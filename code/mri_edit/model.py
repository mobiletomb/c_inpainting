import math
import random
from collections import abc

import torch
from torch import nn
from torch.nn import functional as F


def exists(val):
    return val is not None


class EqualLinear(nn.Module):
    def __init__(
            self, in_dim, out_dim, bias=True, bias_init=0, lr_mul=1, activation=None
    ):
        super().__init__()

        self.weight = nn.Parameter(torch.randn(out_dim, in_dim).div_(lr_mul))

        if bias:
            self.bias = nn.Parameter(torch.zeros(out_dim).fill_(bias_init))

        else:
            self.bias = None

        self.activation = activation

        self.scale = (1 / math.sqrt(in_dim)) * lr_mul
        self.lr_mul = lr_mul

    def forward(self, input):
        if self.activation:
            out = F.linear(input, self.weight * self.scale)
            out = fused_leaky_relu(out, self.bias * self.lr_mul)

        else:
            out = F.linear(
                input, self.weight * self.scale, bias=self.bias * self.lr_mul
            )

        return out

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.weight.shape[1]}, {self.weight.shape[0]})'
        )
    
    
def upfirdn2d(inputs, kernel, up=1, down=1, pad=(0, 0)):
    if not isinstance(up, abc.Iterable):
        up = (up, up)

    if not isinstance(down, abc.Iterable):
        down = (down, down)

    if len(pad) == 2:
        pad = (pad[0], pad[1], pad[0], pad[1])

    return upfirdn2d_native(inputs, kernel, *up, *down, *pad)


def upfirdn2d_native(
    inputs, kernel, up_x, up_y, down_x, down_y, pad_x0, pad_x1, pad_y0, pad_y1
):
    _, channel, in_h, in_w = inputs.shape
    inputs = inputs.reshape(-1, in_h, in_w, 1)

    _, in_h, in_w, minor = inputs.shape
    kernel_h, kernel_w = kernel.shape

    out = inputs.view(-1, in_h, 1, in_w, 1, minor)
    out = F.pad(out, [0, 0, 0, up_x - 1, 0, 0, 0, up_y - 1])
    out = out.view(-1, in_h * up_y, in_w * up_x, minor)

    out = F.pad(
        out, [0, 0, max(pad_x0, 0), max(pad_x1, 0), max(pad_y0, 0), max(pad_y1, 0)]
    )
    out = out[
          :,
          max(-pad_y0, 0): out.shape[1] - max(-pad_y1, 0),
          max(-pad_x0, 0): out.shape[2] - max(-pad_x1, 0),
          :,
          ]

    out = out.permute(0, 3, 1, 2)
    out = out.reshape(
        [-1, 1, in_h * up_y + pad_y0 + pad_y1, in_w * up_x + pad_x0 + pad_x1]
    )
    w = torch.flip(kernel, [0, 1]).view(1, 1, kernel_h, kernel_w)
    out = F.conv2d(out, w)
    out = out.reshape(
        -1,
        minor,
        in_h * up_y + pad_y0 + pad_y1 - kernel_h + 1,
        in_w * up_x + pad_x0 + pad_x1 - kernel_w + 1,
    )
    out = out.permute(0, 2, 3, 1)
    out = out[:, ::down_y, ::down_x, :]

    out_h = (in_h * up_y + pad_y0 + pad_y1 - kernel_h + down_y) // down_y
    out_w = (in_w * up_x + pad_x0 + pad_x1 - kernel_w + down_x) // down_x

    return out.view(-1, channel, out_h, out_w)


class FusedLeakyReLU(nn.Module):
    def __init__(self, channel, bias=True, negative_slope=0.2, scale=2 ** 0.5):
        super().__init__()

        if bias:
            self.bias = nn.Parameter(torch.zeros(channel))

        else:
            self.bias = None

        self.negative_slope = negative_slope
        self.scale = scale

    def forward(self, inputs):
        return fused_leaky_relu(inputs, self.bias, self.negative_slope, self.scale)


def fused_leaky_relu(inputs, bias=None, negative_slope=0.2, scale=2 ** 0.5):
    if bias is not None:
        rest_dim = [1] * (inputs.ndim - bias.ndim - 1)
        return (
            F.leaky_relu(
                inputs + bias.view(1, bias.shape[0], *rest_dim), negative_slope=negative_slope
            )
            * scale
        )

    else:
        return F.leaky_relu(inputs, negative_slope=negative_slope) * scale


class PixelNorm(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, input):
        return input * torch.rsqrt(torch.mean(input ** 2, dim=1, keepdim=True) + 1e-8)


def make_kernel(k):
    k = torch.tensor(k, dtype=torch.float32)

    if k.ndim == 1:
        k = k[None, :] * k[:, None]

    k /= k.sum()

    return k


class Upsample(nn.Module):
    def __init__(self, kernel, factor=2):
        super().__init__()

        self.factor = factor
        kernel = make_kernel(kernel) * (factor ** 2)
        self.register_buffer('kernel', kernel)

        p = kernel.shape[0] - factor

        pad0 = (p + 1) // 2 + factor - 1
        pad1 = p // 2

        self.pad = (pad0, pad1)

    def forward(self, input):
        out = upfirdn2d(input, self.kernel, up=self.factor, down=1, pad=self.pad)

        return out


class Downsample(nn.Module):
    def __init__(self, kernel, factor=2):
        super().__init__()

        self.factor = factor
        kernel = make_kernel(kernel)
        self.register_buffer('kernel', kernel)

        p = kernel.shape[0] - factor

        pad0 = (p + 1) // 2
        pad1 = p // 2

        self.pad = (pad0, pad1)

    def forward(self, input):
        out = upfirdn2d(input, self.kernel, up=1, down=self.factor, pad=self.pad)

        return out


class Blur(nn.Module):
    def __init__(self, kernel, pad, upsample_factor=1):
        super().__init__()

        kernel = make_kernel(kernel)

        if upsample_factor > 1:
            kernel = kernel * (upsample_factor ** 2)

        self.register_buffer('kernel', kernel)

        self.pad = pad

    def forward(self, input):
        out = upfirdn2d(input, self.kernel, pad=self.pad)

        return out


class EqualConv2d(nn.Module):
    def __init__(
            self, in_channel, out_channel, kernel_size, stride=1, padding=0, bias=True, dilation=1 ## modified
    ):
        super().__init__()

        self.weight = nn.Parameter(
            torch.randn(out_channel, in_channel, kernel_size, kernel_size)
        )
        self.scale = 1 / math.sqrt(in_channel * kernel_size ** 2)

        self.stride = stride
        self.padding = padding
        self.dilation = dilation ## modified

        if bias:
            self.bias = nn.Parameter(torch.zeros(out_channel))

        else:
            self.bias = None

    def forward(self, input):
        out = F.conv2d(
            input,
            self.weight * self.scale,
            bias=self.bias,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,  ## modified
        )

        return out

    def __repr__(self):
        return (
            f"{self.__class__.__name__}({self.weight.shape[1]}, {self.weight.shape[0]},"
            f" {self.weight.shape[2]}, stride={self.stride}, padding={self.padding}, dilation={self.dilation})" ## modified
        )


class EqualLinear(nn.Module):
    def __init__(
            self, in_dim, out_dim, bias=True, bias_init=0, lr_mul=1, activation=None
    ):
        super().__init__()

        self.weight = nn.Parameter(torch.randn(out_dim, in_dim).div_(lr_mul))

        if bias:
            self.bias = nn.Parameter(torch.zeros(out_dim).fill_(bias_init))

        else:
            self.bias = None

        self.activation = activation

        self.scale = (1 / math.sqrt(in_dim)) * lr_mul
        self.lr_mul = lr_mul

    def forward(self, input):
        if self.activation:
            out = F.linear(input, self.weight * self.scale)
            out = fused_leaky_relu(out, self.bias * self.lr_mul)

        else:
            out = F.linear(
                input, self.weight * self.scale, bias=self.bias * self.lr_mul
            )

        return out

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.weight.shape[1]}, {self.weight.shape[0]})'
        )


class ScaledLeakyReLU(nn.Module):
    def __init__(self, negative_slope=0.2):
        super().__init__()

        self.negative_slope = negative_slope

    def forward(self, input):
        out = F.leaky_relu(input, negative_slope=self.negative_slope)

        return out * math.sqrt(2)


class ModulatedConv2d(nn.Module):
    def __init__(
            self,
            in_channel,
            out_channel,
            kernel_size,
            style_dim,
            demodulate=True,
            dilation=1
    ):
        super().__init__()

        self.eps = 1e-8
        self.kernel_size = kernel_size
        self.in_channel = in_channel
        self.out_channel = out_channel
        self.dilation = dilation 

        fan_in = in_channel * kernel_size ** 2
        self.scale = 1 / math.sqrt(fan_in)
        self.padding = (kernel_size - 1) // 2 * dilation 

        self.weight = nn.Parameter(
            torch.randn(1, out_channel, in_channel, kernel_size, kernel_size)
        )

        self.modulation = EqualLinear(style_dim, in_channel, bias_init=1)

        self.demodulate = demodulate

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.in_channel}, {self.out_channel}, {self.kernel_size}, '
            f'upsample={self.upsample}, downsample={self.downsample})'
        )

    def forward(self, input, style):
        batch, in_channel, height, width = input.shape

        style = self.modulation(style).view(batch, 1, in_channel, 1, 1)
        weight = self.scale * self.weight * style

        if self.demodulate:
            demod = torch.rsqrt(weight.pow(2).sum([2, 3, 4]) + 1e-8)
            weight = weight * demod.view(batch, self.out_channel, 1, 1, 1)

        weight = weight.view(
            batch * self.out_channel, in_channel, self.kernel_size, self.kernel_size
        )

        input = input.view(1, batch * in_channel, height, width)
        out = F.conv2d(input, weight, padding=self.padding, groups=batch, dilation=self.dilation)  ##### modified
        _, _, height, width = out.shape
        out = out.view(batch, self.out_channel, height, width)

        return out


class NoiseInjection(nn.Module):
    def __init__(self):
        super().__init__()

        self.weight = nn.Parameter(torch.zeros(1))

    def forward(self, image, noise=None):
        if noise is None:
            batch, _, height, width = image.shape
            noise = image.new_empty((batch, 1, height, width)).normal_(0., 1.)
        else:  
            batch, _, height, width = image.shape
            _, _, height1, width1 = noise.shape
            if height != height1 or width != width1:
                noise = F.adaptive_avg_pool2d(noise, (height, width))

        return image + self.weight * noise


class ConstantInput(nn.Module):
    def __init__(self, channel, size=4):
        super().__init__()

        self.input = nn.Parameter(torch.randn(1, channel, size, size))

    def forward(self, input):
        batch = input.shape[0]
        out = self.input.repeat(batch, 1, 1, 1)

        return out


class StyledConv(nn.Module):
    def __init__(
            self,
            in_channel,
            out_channel,
            kernel_size,
            style_dim,
            demodulate=True,
            dilation=1,  
    ):
        super().__init__()
        
        self.conv = ModulatedConv2d(
            in_channel,
            out_channel,
            kernel_size,
            style_dim,
            demodulate=demodulate,
            dilation=dilation,  
        )

        self.noise = NoiseInjection()
        self.activate = FusedLeakyReLU(out_channel)

    def forward(self, input, style, noise=None):
        out = self.conv(input, style)
        out = self.noise(out, noise=noise)
        out = self.activate(out)

        return out
    

class StyledConvBlock(nn.Module):
    def __init__(            
            self,
            in_channel,
            out_channel,
            kernel_size,
            style_dim,
            up_sample=True,
            demodulate=True,
            dilation=1,  
        ):
        super().__init__()
        
        self.upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=False) if up_sample else None
        
        self.block_0 = StyledConv(
            in_channel,
            out_channel,
            kernel_size,
            style_dim,
            demodulate=demodulate,
            dilation=dilation,  
        )

        self.block_1 = StyledConv(
            out_channel,
            out_channel,
            kernel_size,
            style_dim,
            demodulate=demodulate,
            dilation=dilation,  
        )
    
    def forward(self, x, style, noise=None):
        if exists(self.upsample):
            x = self.upsample(x)

        out = self.block_0(x, style, noise)
        out = self.block_1(out, style, noise)
        
        return out
        

class ToMRI(nn.Module):
    def __init__(self, in_channel, style_dim, cls, upsample=True, blur_kernel=[1, 3, 3, 1], dilation=1):  ##### modified
        super().__init__()

        if upsample:
            self.upsample = Upsample(blur_kernel)

        self.conv = ModulatedConv2d(in_channel, cls, 1, style_dim, demodulate=False)
        self.bias = nn.Parameter(torch.zeros(1, cls, 1, 1))
        
        self.dilation = dilation ##### modified
        if dilation > 1: ##### modified
            blur_weight = torch.randn(1, 1, 3, 3) * 0  + 1
            blur_weight[:,:,0,1] = 2
            blur_weight[:,:,1,0] = 2
            blur_weight[:,:,1,2] = 2
            blur_weight[:,:,2,1] = 2
            blur_weight[:,:,1,1] = 4
            blur_weight = blur_weight / 16.0 
            self.register_buffer("blur_weight", blur_weight)

    def forward(self, input, style, skip=None):
        out = self.conv(input, style)
        out = out + self.bias

        if skip is not None:
            if self.dilation == 1:
                skip = self.upsample(skip)
            else:  
                batch, in_channel, _, _ = skip.shape
                skip = F.conv2d(skip, self.blur_weight.repeat(in_channel,1,1,1), 
                                padding=self.dilation//2, groups=in_channel, dilation=self.dilation//2)

            out = out + skip

        return out


class Generator(nn.Module):
    def __init__(
            self,
            style_dim,
            blur_kernel=[1, 3, 3, 1],
            dilation=1,
            out_c=1
    ):
        super().__init__()

        self.style_dim = style_dim
        self.out_c = out_c
        
        self.dilation = dilation
            
        self.latent_style_0 = nn.Sequential()
        
        for i in range(4):
            self.latent_style_0.append(EqualLinear(512, 512))
            
        self.latent_style_1 = nn.Sequential()
        
        for i in range(4):
            self.latent_style_1.append(EqualLinear(512, 512))

        self.channels = {
            8: 512,
            16: 256,
            32: 128,
            64: 64,
            128: 32,
            256: 16,
        }

        # [512, 8, 8]
        self.stage_5 = StyledConvBlock(self.channels[8], 
                                self.channels[16], 
                                3, 
                                style_dim, 
                                dilation=self.dilation)
        
        # [256, 16, 16]
        self.stage_4 = StyledConvBlock(self.channels[8], 
                                self.channels[32], 
                                3, 
                                style_dim, 
                                dilation=self.dilation)
        
        self.conv = nn.ModuleList()
        # [128, 32, 32]
        self.conv.append(BasicUpBlock(self.channels[16],
                                        self.channels[64],
                                        2))
        
        # [64, 64, 64]
        self.conv.append(BasicUpBlock(self.channels[32],
                                        self.channels[128],
                                        2))
        
        # [32, 128, 128]
        self.conv.append(BasicUpBlock(self.channels[64],
                                        self.channels[256],
                                        2))
        
        # [16, 256, 256]
        self.conv.append(BasicUpBlock(self.channels[128],
                                self.channels[256],
                                2))
        
        self.out_layer = nn.Conv2d(self.channels[256], self.out_c, 1, 1)
        
    def get_w(self, x):      
        styles_0 = self.latent_style_0(x)
        styles_1 = self.latent_style_1(x)
        return [styles_0, styles_1]
    
    def forward(self, x, skip_features, w):
        styles = self.get_w(w)

        x = self.stage_5(x, styles[0])
        x = self.stage_4(torch.cat([x, skip_features[0]], dim=1), styles[1])
        
        for i in range(3):
            x = torch.cat([x, skip_features[i + 1]], dim=1)
            x = self.conv[i](x)

        x = self.out_layer(x)
        return x

class BasicUpBlock(nn.Module):
    def __init__(self, in_c, out_c, depth) -> None:
        super().__init__()
        
        self.in_c = in_c
        self.out_c = out_c
        self.depth = depth
        
        modules = []
        modules += [nn.ConvTranspose2d(self.in_c, self.out_c, kernel_size=4, stride=2, padding=1),
                    nn.LeakyReLU()]
        for i in range(self.depth - 1):
            modules += [nn.Conv2d(self.out_c, self.out_c, kernel_size=3, stride=1, padding=1),
                        nn.LeakyReLU()]
        
        self.conv = nn.Sequential(*modules)
    def forward(self, x):
        return self.conv(x)
    
    
class BasicDownBlock(nn.Module):
    def __init__(self, in_c, out_c, depth) -> None:
        super().__init__()
        
        self.in_c = in_c
        self.out_c = out_c
        self.depth = depth
        
        modules = []
        modules += [nn.Conv2d(self.in_c, self.out_c, kernel_size=3, stride=2, padding=1),
                    nn.LeakyReLU()]
        for i in range(self.depth - 1):
            modules += [nn.Conv2d(self.out_c, self.out_c, kernel_size=3, stride=1, padding=1),
                        nn.LeakyReLU()]
        
        self.conv = nn.Sequential(*modules)
    def forward(self, x):
        return self.conv(x)
        
        
class Encoder(nn.Module):
    def __init__(self, in_c, filters, depth=2) -> None:
        super().__init__()
        
        self.filters = filters
        self.depth = depth
        
        self.encoder = nn.ModuleList()
        self.encoder.append(nn.Conv2d(in_c, filters, 3, 1, 1))
        
        for i in range(5):
            self.encoder.append(BasicDownBlock(filters * pow(2, i), filters * pow(2, i + 1), self.depth))

    def forward(self, x):
        skip_features = []
        for i in self.encoder:
            x = i(x)
            skip_features.append(x)
        skip_features=skip_features[0:-1][::-1]
        return x, skip_features


class Editor(nn.Module):
    def __init__(self, in_c=1, filters=16, style_dim=512, out_c=1) -> None:
        super().__init__() 
        
        self.encoder = Encoder(in_c, filters)
        self.decoder = Generator(style_dim, out_c=out_c)
        
        self.c_encoder = Encoder(3, filters)
        self.pool = nn.AdaptiveAvgPool2d(1)
        
        self.patch_size = [2, 2]
        self.mask_ratio = 0.90
        
    def get_masked_region(self, x, y):
        """
            返回被编辑区域以及共同区域的二值掩码图, 1 表示被遮挡区域, 0 表示背景
        """
        resec = torch.logical_xor(x, y)
        intersec = torch.logical_or(x, y)
        
        resec = torch.sum(resec, dim=1, keepdim=True) 
        resec = torch.where(resec > 0., 1., 0.)
        
        intersec = torch.sum(intersec, dim=1, keepdim=True) 
        intersec = torch.where(intersec > 0., 1., 0.)
        
        return resec, intersec
    
    def random_masking(self, x):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - self.mask_ratio))
        
        noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]
        
        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return mask
    
    def masking(self, x, y):
        resec, intersec = self.get_masked_region(x, y)
        
        patch = self.patchify(intersec)
        random_mask = self.random_masking(patch)
        random_mask = random_mask.unsqueeze(-1).repeat(1, 1, 4)
        random_mask = self.unpatchify(random_mask, 256)
        
        intersec = intersec * random_mask
        
        return torch.logical_not(resec) * torch.logical_not(intersec)
    
    def patchify(self, imgs):
        """
        imgs: (N, 3, H, W)
        x: (N, L, patch_size**2 *3)
        """
        p = self.patch_size[0]
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], 1, h, p, w, p))
        x = torch.einsum('nchpwq->nhwpqc', x)
        x = x.reshape(shape=(imgs.shape[0], h * w, p**2 * 1))
        return x
    
    def unpatchify(self, imgs, H):
        p = self.patch_size[0]
        h = w = H // p
        x = imgs.reshape((imgs.shape[0], h, w, p, p, 1))
        x = torch.einsum('nhwpqc->nchpwq', x)
        x = x.reshape((imgs.shape[0], 1, h * p, w * p))
        return x
        
    def get_w(self, x):
        w, _ = self.c_encoder(x)
        w = self.pool(w).squeeze(-1).squeeze(-1)
        return w
        
    def forward(self, x, gli_0, gli_1):
        mask = self.masking(gli_0, gli_1)

        x = x * mask
        w = self.get_w(gli_1)

        x, skip_features = self.encoder(x)
        x = self.decoder(x, w=w, skip_features=skip_features)
        return x, torch.logical_not(mask)
    
    
