from __future__ import annotations

from inspect import getfullargspec

import torch
from pytorch_lightning import LightningDataModule
from torch.utils.data import Dataset, random_split, DataLoader
from torch.utils.data._utils.collate import default_convert
import pytorch_lightning as pl

from ..h5data import Database


class DBDataset(Database, Dataset):
    """
    extends ``Database`` so that it can also be used in place of a ``Dataset``
    """

    def __init__(self, h5_file: str, keep_open: bool = False):
        """
        "first" init instantiates a ``Database``

        Parameters
        ----------
        h5_file : str
            path to the .h5 file containing the data
        keep_open : bool, optional
            whether to keep the h5 file open or close it after each query.
            Default is ``False``.
        """
        Database.__init__(self, h5_file, keep_open)

    def prepare_dataset(self, model: pl.LightningModule, datamodule: DBDataModule):
        """
        placeholder for implementing what need to be done before serving data.

        If this class was just a ``Dataset``, this would be its constructor.
        This method is best called in ``prepare_data()`` as shown in ``DBDataModule``.
        Getting the model as argument allows to use its hparams or any of its computed properties
        to configure ``self.__len__`` and ``self.__getitem__``.
        Getting the datamodule allows to overwrite some of its ``loader_kwargs``.

        Parameters
        ----------
        model : pl.LightningModule
            The model that will consume this dataset.
        datamodule : DBDataModule
            The datamodule where this db lives.
            It exposes a ``loader_kwargs`` attribute for the train, val and test loaders
        Returns
        -------
        None
        """
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, item):
        raise NotImplementedError

    def split(self, splits):
        """
        Parameters
        ----------
        splits: Sequence of floats or ints possibly containing None.
            The sequence of elements corresponds to the proportion (floats), the number of examples (ints) or the absence of
            train-set, validation-set, test-set, other sets... in that order.

        Returns
        -------
        splits: tuple
            the *-sets
        """
        nones = []
        if any(x is None for x in splits):
            if splits[0] is None:
                raise ValueError("the train-set's split cannot be None")
            nones = [i for i, x in zip(range(len(splits)), splits) if x is None]
            splits = [x for x in splits if x is not None]
        if all(type(x) is float for x in splits):
            splits = [x / sum(splits) for x in splits]
            N = len(self)
            # leave the last one out for now because of rounding
            as_ints = [int(N * x) for x in splits[:-1]]
            # check that the last is not zero
            if N - sum(as_ints) == 0:
                raise ValueError("the last split rounded to zero element. Please provide a greater float or consider "
                                 "passing ints.")
            as_ints += [N - sum(as_ints)]
            splits = as_ints
        sets = list(random_split(self, splits))
        if any(nones):
            sets = [None if i in nones else sets.pop(0) for i in range(len(sets + nones))]
        return tuple(sets)

    @property
    def hparams(self):
        params = dict()
        for feat in self.features:
            this_feat = {feat + "_" + k: v for k, v in getattr(self, feat).attrs.items()}
            params.update(this_feat)
        return params

    # ***************************************************************************************************
    # ************  Convenience functions for converting features to (cuda) tensors  ********************

    def to_tensor(self):
        for feat in self.features:
            as_tensor = self._to_tensor(getattr(self, feat))
            setattr(self, feat, as_tensor)
        return self

    def to(self, device):
        for feat in self.features:
            self._to(getattr(self, feat), device)
        return self

    @staticmethod
    def _to_tensor(obj):
        if isinstance(obj, torch.Tensor):
            return obj
        # converting obj[:] makes sure we get the data out of any db.feature object
        maybe_tensor = default_convert(obj[:])
        if isinstance(maybe_tensor, torch.Tensor):
            return maybe_tensor
        try:
            obj = torch.tensor(obj)
        except Exception as e:
            raise e
        return obj

    @staticmethod
    def _to(obj, device):
        """move any underlying tensor to some device"""
        if getattr(obj, "to", False):
            return obj.to(device)
        raise TypeError("object %s has no `to()` attribute" % str(obj))


class DBDataModule(LightningDataModule):
    """
    boilerplate subclass of ``pytorch_lightning.LightningDataModule`` to handle standard "data-tasks" :
        - give a Database a chance to prepare itself for serving data once the model has been instantiated
        - move small datasets to the RAM of the gpu if desired
            (TODO: Otherwise, more workers are used in the DataLoaders for better performance)
        - split data into train, val and test sets and serve corresponding DataLoaders
    """
    def __init__(self,
                 model=None,
                 db: DBDataset = None,
                 in_mem_data=True,
                 splits=None,
                 **loader_kwargs,
                 ):
        super(DBDataModule, self).__init__()
        self.model = model
        self.db = db
        self.in_mem_data = in_mem_data
        self.splits = splits
        self.loader_kwargs = self._filter_loader_kwargs(loader_kwargs)
        self.train_ds, self.val_ds, self.test_ds = None, None, None

    def prepare_data(self, *args, **kwargs):
        self.db.prepare_dataset(model=self.model, datamodule=self)

    def setup(self, stage=None):
        if stage == "fit":
            if self.in_mem_data and torch.cuda.is_available():
                self.db.to_tensor()
                self.db.to("cuda")
            if not self.splits:
                sets = (self.db, )
            else:
                sets = self.db.split(self.splits)
            for ds, attr in zip(sets, ["train_ds", "val_ds", "test_ds"]):
                setattr(self, attr, ds)

    def train_dataloader(self):
        if not self.has_prepared_data:
            self.prepare_data()
        if not self.has_setup_fit:
            self.setup("fit")
        return DataLoader(self.train_ds, **self.loader_kwargs)

    def val_dataloader(self, shuffle=False):
        has_val = self.splits is not None and len(self.splits) >= 2 and self.splits[1] is not None
        if not has_val:
            return None
        if not self.has_prepared_data:
            self.prepare_data()
        if not self.has_setup_fit:
            self.setup("fit")
        kwargs = self.loader_kwargs.copy()
        kwargs["shuffle"] = shuffle
        return DataLoader(self.val_ds, **kwargs)

    def test_dataloader(self, shuffle=False):
        has_test = self.splits is not None and len(self.splits) >= 3 and self.splits[2] is not None
        if not has_test:
            return None
        if not self.has_prepared_data:
            self.prepare_data()
        if not self.has_setup_test:
            self.setup("test")
        kwargs = self.loader_kwargs.copy()
        kwargs["shuffle"] = shuffle
        return DataLoader(self.test_ds, **kwargs)

    @staticmethod
    def _filter_loader_kwargs(kwargs):
        valids = getfullargspec(DataLoader.__init__).annotations.keys()
        return {k: v for k, v in kwargs.items() if k in valids}
