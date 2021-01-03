import h5py
import numpy as np
import pandas as pd
from multiprocessing import cpu_count, Pool
from typing import Iterable
import os
import warnings

from .api import Database
from .metadata import Metadata
from .transforms import default_extract_func

warnings.filterwarnings("ignore", message="PySoundFile failed.")
warnings.filterwarnings("ignore", message="PerformanceWarning")
warnings.filterwarnings("ignore", message="Creating an ndarray from ragged nested sequences")


class AudioFileWalker:

    AUDIO_EXTENSIONS = ("wav", "aif", "aiff", "mp3", "m4a", "mp4")

    def __init__(self, roots=None, files=None):
        """
        recursively find audio files from `roots` and/or collect audio_files passed in `files`
        @param roots: a single path (string, os.Path) or an Iterable of paths
                      from which to collect audio files recursively
        @param files: a single path (string, os.Path) or an Iterable of paths
        N.B. : any file whose extension isn't in AudioFileWalker.AUDIO_EXTENSIONS will be ignored,
        regardless whether it was found recursively or passed through the `files` argument.

        AudioFileWalker implements `__iter__`, hence files can be retrieved in different ways.
        For instance so:
        ```
        files = list(AudioFileWalker(roots=some_roots, files=some_files))
        ```
        """
        generators = []

        if roots is not None and isinstance(roots, Iterable):
            if isinstance(roots, str):
                if not os.path.exists(roots):
                    raise FileNotFoundError("%s does not exist." % roots)
                generators += [AudioFileWalker.walk_root(roots)]
            else:
                for r in roots:
                    if not os.path.exists(r):
                        raise FileNotFoundError("%s does not exist." % r)
                generators += [AudioFileWalker.walk_root(root) for root in roots]

        if files is not None and isinstance(files, Iterable):
            if isinstance(files, str):
                if not os.path.exists(files):
                    raise FileNotFoundError("%s does not exist." % files)
                generators += [(f for f in [files] if AudioFileWalker.is_audio_file(files))]
            else:
                for f in files:
                    if not os.path.exists(f):
                        raise FileNotFoundError("%s does not exist." % f)
                generators += [(f for f in files if AudioFileWalker.is_audio_file(f))]

        self.generators = generators

    def __iter__(self):
        for generator in self.generators:
            for file in generator:
                yield file

    @staticmethod
    def walk_root(root):
        for directory, _, files in os.walk(root):
            for audio_file in filter(AudioFileWalker.is_audio_file, files):
                yield os.path.join(directory, audio_file)

    @staticmethod
    def is_audio_file(filename):
        # filter out hidden files (isn't cross-platform, but, it's a start!...)
        if filename.startswith("."):
            return False
        return os.path.splitext(filename)[-1].strip(".") in AudioFileWalker.AUDIO_EXTENSIONS


def sizeof_fmt(num, suffix='b'):
    """
    straight from https://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size
    """
    for unit in ['', 'k', 'M', 'G', 'T', 'P', 'E', 'Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def _empty_info(features_names):
    tuples = [("directory", ""), ("name", ""),
              *[t for feat in features_names for t in [(feat, "dtype"), (feat, "shape"), (feat, "size")]
                if feat != "metadata"]
              ]
    idx = pd.MultiIndex.from_tuples(tuples)
    return pd.DataFrame([], columns=idx)


def split_path(path):
    prefix, file_name = os.path.split(path)
    return prefix, file_name


# Core function

def file_to_db(abs_path, extract_func=default_extract_func, mode="w"):
    """
    if mode == "r+" this will either:
        - raise an Exception if the feature already exists
        - concatenate data along the "feature_axis", assuming that each feature correspond to the same file
          or file collections.
          If you want to concatenate dbs along the "file_axis" consider using `concatenate_dbs(..)`
    @param abs_path:
    @param extract_func:
    @param mode:
    @return:
    """
    print("making db for %s" % abs_path)
    tmp_db = os.path.splitext(abs_path)[0] + ".h5"
    rv = extract_func(abs_path)
    if "metadata" not in rv:
        raise ValueError("Expected `extract_func` to return a ('metadata', Metadata) item. Found none")
    info = _empty_info(rv.keys())
    info.loc[0, [("directory", ""), ("name", "")]] = split_path(abs_path)
    f = h5py.File(tmp_db, mode)
    for name, (attrs, data) in rv.items():
        if issubclass(type(data), np.ndarray):
            ds = f.create_dataset(name=name, shape=data.shape, data=data)
            ds.attrs.update(attrs)
            info.at[0, [(name, "dtype"), (name, "shape"), (name, "size")]] = tuple([ds.dtype, ds.shape, sizeof_fmt(data.nbytes)])
        elif issubclass(type(data), pd.DataFrame):
            f.close()
            pd.DataFrame(data).to_hdf(tmp_db, name, "r+")
            f = h5py.File(tmp_db, "r+")
    f.flush()
    f.close()
    if "info" in f.keys():
        prior = pd.read_hdf(tmp_db, "info", "r")
        info = pd.concat((prior, info.iloc[:, 2:]), axis=1)
    info.to_hdf(tmp_db, "info", "r+")
    return tmp_db


# Multiprocessing routine

def make_db_for_each_file(file_walker,
                          extract_func=default_extract_func,
                          n_cores=cpu_count()):
    args = [(file, extract_func) for file in file_walker]
    with Pool(n_cores) as p:
        tmp_dbs = p.starmap(file_to_db, args)
    return tmp_dbs


# Aggregating sub-tasks

def collect_infos(tmp_dbs):
    infos = []
    for db in tmp_dbs:
        infos += [Database(db).info]
    return pd.concat(infos, ignore_index=True)


def collect_metadatas(tmp_dbs):
    metadatas = []
    offset = 0
    for db in tmp_dbs:
        meta = Database(db).metadata
        meta.loc[:, ("start", "stop")] = meta.loc[:, ("start", "stop")].values + offset
        meta.loc[:, "name"] = os.path.splitext(db)[0]
        metadatas += [meta]
        offset = meta.last_stop
    return pd.DataFrame(pd.concat(metadatas, ignore_index=True))


def zip_prev_next(iterable):
    return zip(iterable[:-1], iterable[1:])


def ds_definitions_from_infos(infos):
    tb = infos.iloc[:, 2:].T
    paths = [os.path.join(*parts) for parts in infos.iloc[:, :2].values]
    # change the paths' extensions
    paths = [os.path.splitext(path)[0] + ".h5" for path in paths]
    features = set(tb.index.get_level_values(0))
    ds_definitions = {}
    for f in features:
        dtype = tb.loc[(f, "dtype"), :].unique().item()
        shapes = tb.loc[(f, "shape"), :].values
        dims = shapes[0][1:]
        assert all(shp[1:] == dims for shp in
                   shapes[1:]), "all features should have the same dimensions but for the first axis"
        layout = Metadata.from_duration([s[0] for s in shapes])
        ds_shape = (layout.last_stop, *dims)
        layout.index = paths
        ds_definitions[f] = {"shape": ds_shape, "dtype": dtype, "layout": layout}
    return ds_definitions


def create_datasets_from_defs(target, defs, mode="w"):
    f = h5py.File(target, mode)
    for name, params in defs.items():
        f.create_dataset(name, shape=params["shape"], dtype=params["dtype"],
                         chunks=True, maxshape=(None, *params["shape"][1:]))
        layout = params["layout"]
        layout.reset_index(drop=False, inplace=True)
        layout = layout.rename(columns={"index": "name"})
        f.flush()
        f.close()
        pd.DataFrame(layout).to_hdf(target, "layouts/" + name, "r+", format="table")
        f = h5py.File(target, "r+")
    f.flush()
    f.close()
    return


def make_integration_args(target):
    args = []
    with h5py.File(target, "r") as f:
        for feature in f["layouts"].keys():
            df = Metadata(pd.read_hdf(target, "layouts/" + feature))
            args += [(target, source, feature, indices) for source, indices in
                     zip(df.name, df.slices(time_axis=0))]
    return args


def integrate(target, source, key, indices):
    with h5py.File(source, "r") as src:
        data = src[key][()]
        attrs = {k: v for k, v in src[key].attrs.items()}
    with h5py.File(target, "r+") as trgt:
        trgt[key][indices] = data
        trgt[key].attrs.update(attrs)
    return


# Aggregating function and main client

def aggregate_dbs(target, dbs, mode="w", remove_sources=False):
    infos = collect_infos(dbs)
    metadata = collect_metadatas(dbs)
    definitions = ds_definitions_from_infos(infos)
    create_datasets_from_defs(target, definitions, mode)
    args = make_integration_args(target)
    for arg in args: integrate(*arg)
    if remove_sources:
        for src in dbs: os.remove(src)
    infos = infos.astype(object)
    infos.to_hdf(target, "info", "r+")
    metadata.to_hdf(target, "metadata", "r+")


def make_root_db(db_name, roots='./', files=None, extract_func=default_extract_func,
                 n_cores=cpu_count(), remove_sources=True):
    walker = AudioFileWalker(roots, files)
    dbs = make_db_for_each_file(walker, extract_func, n_cores)
    aggregate_dbs(db_name, dbs, "w", remove_sources)
    return Database(db_name)