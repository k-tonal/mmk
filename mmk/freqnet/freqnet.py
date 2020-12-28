import torch.nn as nn
import numpy as np

from .modules import GatedLinearInput, AbsLinearOutput, mean_L1_prop
from .freq_layer import FreqLayer
from .base import FreqNetModel


class FreqNet(FreqNetModel):
    LAYER_KWARGS = ["groups", "strict", "accum_outputs", "concat_outputs",
                    "pad_input", "learn_padding"]

    def __init__(self,
                 loss_fn=mean_L1_prop,
                 model_dim=512,
                 groups=1,
                 n_layers=(int(np.log2(8)),),
                 strict=False,
                 accum_outputs=None,
                 concat_outputs=None,
                 pad_input=None,
                 learn_padding=False,
                 **data_optim_kwargs):
        super(FreqNet, self).__init__(**data_optim_kwargs)
        self._loss_fn = loss_fn
        self.model_dim = model_dim
        self.groups = groups
        self.n_layers = n_layers
        self.strict = strict
        self.accum_outputs = accum_outputs
        self.concat_outputs = concat_outputs
        self.pad_input = pad_input
        self.learn_padding = learn_padding

        # Input Encoder
        self.inpt = GatedLinearInput(self.input_dim, self.model_dim)

        # Auto-regressive Part
        layer_kwargs = {attr: getattr(self, attr) for attr in self.LAYER_KWARGS}
        # for simplicity we keep all the layers in a flat list
        self.layers = nn.ModuleList([
            FreqLayer(layer_index=i, input_dim=model_dim, layer_dim=model_dim, **layer_kwargs)
            for n_layers in self.n_layers for i in range(n_layers)
        ])

        # Output Decoder
        self.outpt = AbsLinearOutput(self.model_dim, self.input_dim)

        self.save_hyperparameters()

    def forward(self, x):
        """
        """
        x = self.inpt(x)
        skips = None
        for layer in self.layers:
            x, skips = layer(x, skips)
        x = self.outpt(skips)
        return x

    def loss_fn(self, predictions, targets):
        return self._loss_fn(predictions, targets)

    def all_rel_shifts(self):
        """sequence of shifts from one layer to the next"""
        return tuple(layer.rel_shift() for layer in self.layers)

    def shift(self):
        """total shift of the network"""
        return sum(self.all_rel_shifts()) + int(not self.strict)

    def all_shifts(self):
        """the accumulated shift at each layer"""
        return tuple(np.cumsum(self.all_rel_shifts()) + int(not self.strict))

    def receptive_field(self):
        block_rf = []
        for i, layer in enumerate(self.layers[:-1]):
            if self.layers[i + 1].layer_index == 0:
                block_rf += [layer.receptive_field() - 1]
        block_rf += [self.layers[-1].receptive_field()]
        return sum(block_rf)

    def output_length(self, input_length):
        return self.all_output_lengths(input_length)[-1]

    def all_output_lengths(self, input_length):
        out_length = input_length
        lengths = []
        for layer in self.layers:
            out_length = layer.output_length(out_length)
            lengths += [out_length]
        return tuple(lengths)

    def targets_shifts_and_lengths(self, input_length):
        return [(self.shift(), self.output_length(input_length))]

    def generation_slices(self):
        # TODO! Add logic for concat/strict parameters
        return slice(-self.receptive_field(), None), slice(-1, None)