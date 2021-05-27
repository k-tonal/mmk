import math
import torch.nn as nn
from dataclasses import dataclass
from typing import Optional

from ..modules import homs as H, ops as Ops

__all__ = [
    'WaveNetLayer',
    'WNNetwork'
]


@dataclass(init=True, repr=False, eq=False, frozen=False, unsafe_hash=True)
class WaveNetLayer(nn.Module):

    layer_i: int
    gate_dim: int = 128
    residuals_dim: Optional[int] = None
    skip_dim: Optional[int] = None
    kernel_size: int = 2
    groups: int = 1
    cin_dim: Optional[int] = None
    gin_dim: Optional[int] = None
    pad_input: int = 1
    accum_outputs: int = -1

    stride: int = 1
    bias: bool = True

    dilation = property(lambda self: self.kernel_size ** self.layer_i)

    shift_diff = property(
        lambda self:
        (self.kernel_size - 1) * self.dilation if self.pad_input == 0 else 0
    )
    input_padding = property(
        lambda self:
        self.pad_input * (self.kernel_size - 1) * self.dilation if self.pad_input else 0
    )
    output_padding = property(
        lambda self:
        - self.accum_outputs * self.shift_diff
    )
    receptive_field = property(
        lambda self:
        self.kernel_size * self.dilation
    )

    @property
    def conv_kwargs(self):
        return dict(kernel_size=self.kernel_size, dilation=self.dilation,
                    stride=self.stride, bias=self.bias, groups=self.groups)

    @property
    def kwargs_1x1(self):
        return dict(kernel_size=1, bias=self.bias, groups=self.groups)

    def gcu_(self):
        return H.GatedUnit(
            H.AddPaths(
                # core
                nn.Conv1d(self.gate_dim, self.residuals_dim, **self.conv_kwargs),
                # conditioning parameters :
                nn.Conv1d(self.cin_dim, self.residuals_dim, **self.kwargs_1x1) if self.cin_dim else None,
                nn.Conv1d(self.gin_dim, self.residuals_dim, **self.kwargs_1x1) if self.gin_dim else None
            ))

    def residuals_(self):
        return nn.Conv1d(self.residuals_dim, self.gate_dim, **self.kwargs_1x1)

    def skips_(self):
        return H.Skips(
            nn.Conv1d(self.residuals_dim, self.skip_dim, **self.kwargs_1x1)
        )

    def accumulator(self):
        return H.AddPaths(nn.Identity(), nn.Identity())

    def __post_init__(self):
        nn.Module.__init__(self)

        with_residuals = self.residuals_dim is not None
        if not with_residuals:
            self.residuals_dim = self.gate_dim

        self.gcu = self.gcu_()

        if with_residuals:
            self.residuals = self.residuals_()
        else:  # keep the signature but just pass through
            self.residuals = nn.Identity()

        if self.skip_dim is not None:
            self.skips = self.skips_()
        else:
            self.skips = lambda y, skp: skp

        if self.accum_outputs:
            self.accum = self.accumulator()
        else:
            self.accum = lambda y, x: y

    def forward(self, inputs):
        x, cin, gin, skips = inputs
        if self.pad_input == 0:
            cause = self.shift_diff if x.size(2) > self.kernel_size else self.kernel_size - 1
            slc = slice(cause, None) if self.accum_outputs <= 0 else slice(None, -cause)
            padder = nn.Identity()
        else:
            slc = slice(None)
            padder = Ops.CausalPad((0, 0, self.input_padding))
        y = self.gcu(padder(x), cin, gin)
        if skips is not None and y.size(-1) != skips.size(-1):
            skips = skips[:, :, slc]
        skips = self.skips(y, skips)
        y = self.accum(self.residuals(y), x[:, :, slc])
        return y, cin, gin, skips

    def output_length(self, input_length):
        if bool(self.pad_input):
            # no matter what, padding input gives the same output shape
            return input_length
        # output is gonna be less than input
        numerator = input_length - self.dilation * (self.kernel_size - 1) - 1
        return math.floor(1 + numerator / self.stride)


# Finally : define the network class
@dataclass(init=True, repr=False, eq=False, frozen=False, unsafe_hash=True)
class WNNetwork(nn.Module):

    n_layers: tuple = (4,)
    q_levels: int = 256
    n_cin_classes: Optional[int] = None
    cin_dim: Optional[int] = None
    n_gin_classes: Optional[int] = None
    gin_dim: Optional[int] = None
    gate_dim: int = 128
    kernel_size: int = 2
    groups: int = 1
    accum_outputs: int = 0
    pad_input: int = 0
    skip_dim: Optional[int] = None
    residuals_dim: Optional[int] = None
    head_dim: Optional[int] = None

    def inpt_(self):
        return H.Paths(
            nn.Sequential(nn.Embedding(self.q_levels, self.gate_dim), Ops.Transpose(1, 2)),
            nn.Sequential(nn.Embedding(self.n_cin_classes, self.cin_dim),
                          Ops.Transpose(1, 2)) if self.cin_dim else None,
            nn.Sequential(nn.Embedding(self.n_gin_classes, self.gin_dim), Ops.Transpose(1, 2)) if self.gin_dim else None
        )

    def layers_(self):
        return nn.Sequential(*[
            WaveNetLayer(i,
                         gate_dim=self.gate_dim,
                         skip_dim=self.skip_dim,
                         residuals_dim=self.residuals_dim,
                         kernel_size=self.kernel_size,
                         cin_dim=self.cin_dim,
                         gin_dim=self.gin_dim,
                         groups=self.groups,
                         pad_input=self.pad_input,
                         accum_outputs=self.accum_outputs,
                         )
            for block in self.n_layers for i in range(block)
        ])

    def outpt_(self):
        return nn.Sequential(
            nn.ReLU(),
            nn.Conv1d(self.gate_dim if self.skip_dim is None else self.skip_dim,
                      self.gate_dim if self.head_dim is None else self.head_dim, kernel_size=1),
            nn.ReLU(),
            nn.Conv1d(self.gate_dim if self.head_dim is None else self.head_dim,
                      self.q_levels, kernel_size=1),
            Ops.Transpose(1, 2)
        )

    def __post_init__(self):
        nn.Module.__init__(self)
        self.inpt = self.inpt_()
        self.layers = self.layers_()
        self.outpt = self.outpt_()

        rf = 0
        for i, layer in enumerate(self.layers):
            if i == (len(self.layers) - 1):
                rf += layer.receptive_field
            elif self.layers[i + 1].layer_i == 0:
                rf += layer.receptive_field - 1
        self.receptive_field = rf

        if self.pad_input == 1:
            self.shift = 1
        elif self.pad_input == -1:
            self.shift = self.receptive_field
        else:
            self.shift = sum(layer.shift_diff for layer in self.layers) + 1

    def forward(self, xi, cin=None, gin=None):
        x, cin, gin = self.inpt(xi, cin, gin)
        y, _, _, skips = self.layers((x, cin, gin, None))
        return self.outpt(skips if skips is not None else y)

    def output_shape(self, input_shape):
        return input_shape[0], self.all_output_lengths(input_shape[1])[-1], input_shape[-1]

    def all_output_lengths(self, input_length):
        out_length = input_length
        lengths = []
        for layer in self.layers:
            out_length = layer.output_length(out_length)
            lengths += [out_length]
        return tuple(lengths)