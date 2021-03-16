from mimikit.kit.db_dataset import DBDataModule
from pytorch_lightning import LightningModule

from ..db_dataset import DBDataset


class DataSubModule(LightningModule):

    db_class = None

    @property
    def db(self):
        """short-hand to quickly access the db passed to the constructor"""
        return self.datamodule.db

    def __init__(self,
                 db: [DBDataset, str] = None,
                 in_mem_data: bool = True,
                 splits: [list, None] = [.8, .2],
                 keep_open=False,
                 **loaders_kwargs,
                 ):
        super(LightningModule, self).__init__()
        if db is not None:
            db_ = db
            if isinstance(db, str):
                db_ = self.db_class(db_, keep_open=keep_open)
            self.datamodule = DBDataModule(self, db_, in_mem_data, splits, **loaders_kwargs)
            self.hparams.update(db_.attrs)

    def random_train_batch(self):
        return next(iter(self.datamodule.train_dataloader()))

    def random_val_batch(self):
        return next(iter(self.datamodule.val_dataloader(shuffle=True)))