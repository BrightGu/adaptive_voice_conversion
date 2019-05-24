import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as ag
import numpy as np
from math import ceil
from functools import reduce
from torch.nn.utils import spectral_norm
from utils import cc

class DummyEncoder(object):
    def __init__(self, encoder):
        self.encoder = encoder

    def load(self, target_network):
        self.encoder.load_state_dict(target_network.state_dict())

    def __call__(self, x):
        return self.encoder(x)

def cal_gradpen(netD, real_data, real_cond, fake_data, fake_cond, center=0, alpha=None, device='cuda'):
    if alpha is not None:
        alpha = torch.tensor(alpha, device=device)  # torch.rand(real_data.size(0), 1, device=device)
    else:
        alpha = torch.rand(real_data.size(0), 1, device=device)
    alpha_exp = alpha.unsqueeze(2).expand(real_data.size())
    interpolates = alpha_exp * real_data + ((1 - alpha_exp) * fake_data)
    alpha_exp = alpha.expand(real_cond.size())
    interpolates_cond = alpha_exp * real_cond + ((1 - alpha_exp) * fake_cond) 
    interpolates.requires_grad_(True)
    interpolates_cond.requires_grad_(True)
    disc_interpolates = netD(interpolates, interpolates_cond)
    gradients_x = ag.grad(outputs=disc_interpolates, inputs=interpolates,
                        grad_outputs=torch.ones(disc_interpolates.size()).to(device),
                        create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradients_c = ag.grad(outputs=disc_interpolates, inputs=interpolates_cond,
                        grad_outputs=torch.ones(disc_interpolates.size()).to(device),
                        create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradient_penalty_x = ((gradients_x.norm(2, dim=1) - center) ** 2).mean()
    gradient_penalty_c = ((gradients_c.norm(2, dim=1) - center) ** 2).mean()
    return gradient_penalty_x + gradient_penalty_c

def compute_grad(d_out, x_in, center=0):
    # add activation sigmoid
    #d_out = torch.sigmoid(d_out)
    gradients = ag.grad(
            outputs=d_out, inputs=x_in, grad_outputs=d_out.new_ones(d_out.size()), 
            create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradient_penalty = ((gradients.norm(2, dim=1) - center) ** 2).mean()
    return gradient_penalty

def pad_layer(inp, layer, pad_type='reflect'):
    kernel_size = layer.kernel_size[0]
    if kernel_size % 2 == 0:
        pad = (kernel_size//2, kernel_size//2 - 1)
    else:
        pad = (kernel_size//2, kernel_size//2)
    # padding
    inp = F.pad(inp, 
            pad=pad,
            mode=pad_type)
    out = layer(inp)
    return out

def pad_layer_2d(inp, layer, pad_type='reflect'):
    kernel_size = layer.kernel_size
    if kernel_size[0] % 2 == 0:
        pad_lr = [kernel_size[0]//2, kernel_size[0]//2 - 1]
    else:
        pad_lr = [kernel_size[0]//2, kernel_size[0]//2]
    if kernel_size[1] % 2 == 0:
        pad_ud = [kernel_size[1]//2, kernel_size[1]//2 - 1]
    else:
        pad_ud = [kernel_size[1]//2, kernel_size[1]//2]
    pad = tuple(pad_lr + pad_ud)
    # padding
    inp = F.pad(inp, 
            pad=pad,
            mode=pad_type)
    out = layer(inp)
    return out

def pixel_shuffle_1d(inp, scale_factor=2):
    batch_size, channels, in_width = inp.size()
    channels //= scale_factor
    out_width = in_width * scale_factor
    inp_view = inp.contiguous().view(batch_size, channels, scale_factor, in_width)
    shuffle_out = inp_view.permute(0, 1, 3, 2).contiguous()
    shuffle_out = shuffle_out.view(batch_size, channels, out_width)
    return shuffle_out

def upsample(x, scale_factor=2):
    x_up = F.interpolate(x, scale_factor=scale_factor, mode='nearest')
    return x_up

def flatten(x):
    out = x.contiguous().view(x.size(0), x.size(1) * x.size(2))
    return out

def concat_cond(x, cond):
    # x = [batch_size, x_channels, length]
    # cond = [batch_size, c_channels]
    cond = cond.unsqueeze(dim=2)
    cond = cond.expand(*cond.size()[:-1], x.size(-1))
    out = torch.cat([x, cond], dim=1)
    return out

def append_cond(x, cond):
    # x = [batch_size, x_channels, length]
    # cond = [batch_size, x_channels * 2]
    p = cond.size(1) // 2
    mean, std = cond[:, :p], cond[:, p:]
    out = x * std.unsqueeze(dim=2) + mean.unsqueeze(dim=2)
    return out

def append_cond_2d(x, cond):
    # x = [batch_size, channels, freq, length]
    # cond = [batch_size, channels * 2]
    p = cond.size(1) // 2
    mean, std = cond[:, :p], cond[:, p:]
    out = x * std.unsqueeze(dim=2).unsqueeze(dim=3) + mean.unsqueeze(dim=2).unsqueeze(dim=3)
    return out

def conv_bank(x, module_list, act=None, pad_type='reflect'):
    outs = []
    for layer in module_list:
        out = pad_layer(x, layer, pad_type)
        if act:
            out = act(out)
        outs.append(out)
    out = torch.cat(outs + [x], dim=1)
    return out

def get_act(act):
    if act == 'relu':
        return nn.ReLU()
    elif act == 'lrelu':
        return nn.LeakyReLU()
    else:
        return nn.ReLU()

class MLP(nn.Module):
    def __init__(self, c_in, c_h, n_blocks, act, sn):
        super(MLP, self).__init__()
        self.act = get_act(act)
        self.n_blocks = n_blocks
        f = spectral_norm if sn else lambda x: x
        self.in_dense_layer = f(nn.Linear(c_in, c_h))
        self.first_dense_layers = nn.ModuleList([f(nn.Linear(c_h, c_h)) for _ in range(n_blocks)])
        self.second_dense_layers = nn.ModuleList([f(nn.Linear(c_h, c_h)) for _ in range(n_blocks)])

    def forward(self, x):
        h = self.in_dense_layer(x)
        for l in range(self.n_blocks):
            y = self.first_dense_layers[l](h)
            y = self.act(y)
            y = self.second_dense_layers[l](y)
            y = self.act(y)
            h = h + y
        return h

class Prenet(nn.Module):
    def __init__(self, c_in, c_h, c_out, 
            kernel_size, n_conv_blocks, 
            subsample, act, dropout_rate):
        super(Prenet, self).__init__()
        self.act = get_act(act)
        self.subsample = subsample
        self.n_conv_blocks = n_conv_blocks
        self.in_conv_layer = nn.Conv2d(1, c_h, kernel_size=kernel_size)
        self.first_conv_layers = nn.ModuleList([nn.Conv2d(c_h, c_h, kernel_size=kernel_size) for _ \
                in range(n_conv_blocks)])
        self.second_conv_layers = nn.ModuleList([nn.Conv2d(c_h, c_h, kernel_size=kernel_size, stride=sub) 
            for sub, _ in zip(subsample, range(n_conv_blocks))])
        output_size = c_in
        for l, sub in zip(range(n_conv_blocks), self.subsample):
            output_size = ceil(output_size / sub)
        self.out_conv_layer = nn.Conv1d(c_h * output_size, c_out, kernel_size=1)
        self.dropout_layer = nn.Dropout(p=dropout_rate)
        self.norm_layer = nn.InstanceNorm2d(c_h, affine=False)

    def forward(self, x):
        # reshape x to 4D
        x = x.contiguous().view(x.size(0), 1, x.size(1), x.size(2))
        out = pad_layer_2d(x, self.in_conv_layer)
        out = self.act(out)
        out = self.norm_layer(out)
        for l in range(self.n_conv_blocks):
            y = pad_layer_2d(out, self.first_conv_layers[l])
            y = self.act(y)
            y = self.norm_layer(y)
            y = self.dropout_layer(y)
            y = pad_layer_2d(y, self.second_conv_layers[l])
            y = self.act(y)
            y = self.norm_layer(y)
            y = self.dropout_layer(y)
            if self.subsample[l] > 1:
                out = F.avg_pool2d(out, kernel_size=self.subsample[l], ceil_mode=True)
            out = y + out
        out = out.contiguous().view(out.size(0), out.size(1) * out.size(2), out.size(3))
        out = pad_layer(out, self.out_conv_layer)
        out = self.act(out)
        return out

class Postnet(nn.Module):
    def __init__(self, c_in, c_h, c_out, c_cond,  
            kernel_size, n_conv_blocks, 
            upsample, act, sn):
        super(Postnet, self).__init__()
        self.act = get_act(act)
        self.upsample = upsample
        self.c_h = c_h
        self.n_conv_blocks = n_conv_blocks
        f = spectral_norm if sn else lambda x: x
        total_upsample = reduce(lambda x, y: x*y, upsample)
        self.in_conv_layer = f(nn.Conv1d(c_in, c_h * c_out // total_upsample, kernel_size=1))
        self.first_conv_layers = nn.ModuleList([f(nn.Conv2d(c_h, c_h, kernel_size=kernel_size)) for _ \
                in range(n_conv_blocks)])
        self.second_conv_layers = nn.ModuleList([f(nn.Conv2d(c_h, c_h*up*up, kernel_size=kernel_size)) 
            for up, _ in zip(upsample, range(n_conv_blocks))])
        self.out_conv_layer = f(nn.Conv2d(c_h, 1, kernel_size=1))
        self.conv_affine_layers = nn.ModuleList(
                [f(nn.Linear(c_cond, c_h * 2)) for _ in range(n_conv_blocks*2)])
        self.norm_layer = nn.InstanceNorm2d(c_h, affine=False)
        self.ps = nn.PixelShuffle(max(upsample))

    def forward(self, x, cond):
        out = pad_layer(x, self.in_conv_layer)
        out = out.contiguous().view(out.size(0), self.c_h, out.size(1) // self.c_h, out.size(2))
        for l in range(self.n_conv_blocks):
            y = pad_layer_2d(out, self.first_conv_layers[l])
            y = self.act(y)
            y = self.norm_layer(y)
            y = append_cond_2d(y, self.conv_affine_layers[l*2](cond))
            y = pad_layer_2d(y, self.second_conv_layers[l])
            y = self.act(y)
            if self.upsample[l] > 1:
                y = self.ps(y)
                y = self.norm_layer(y)
                y = append_cond_2d(y, self.conv_affine_layers[l*2+1](cond))
                out = y + upsample(out, scale_factor=(self.upsample[l], self.upsample[l])) 
            else:
                y = self.norm_layer(y)
                y = append_cond(y, self.conv_affine_layers[l*2+1](cond))
                out = y + out
        out = self.out_conv_layer(out)
        out = out.squeeze(dim=1)
        return out

class SpeakerEncoder(nn.Module):
    def __init__(self, c_in, c_h, c_out, kernel_size,
            bank_size, bank_scale, c_bank, 
            n_conv_blocks, n_dense_blocks, 
            subsample, act):
        super(SpeakerEncoder, self).__init__()
        self.c_in = c_in
        self.c_h = c_h
        self.c_out = c_out
        self.kernel_size = kernel_size
        self.n_conv_blocks = n_conv_blocks
        self.n_dense_blocks = n_dense_blocks
        self.subsample = subsample
        self.act = get_act(act)
        self.conv_bank = nn.ModuleList(
                [nn.Conv1d(c_in, c_bank, kernel_size=k) for k in range(bank_scale, bank_size + 1, bank_scale)])
        in_channels = c_bank * (bank_size // bank_scale) + c_in
        self.in_conv_layer = nn.Conv1d(in_channels, c_h, kernel_size=1)
        self.first_conv_layers = nn.ModuleList([nn.Conv1d(c_h, c_h, kernel_size=kernel_size) for _ \
                in range(n_conv_blocks)])
        self.second_conv_layers = nn.ModuleList([nn.Conv1d(c_h, c_h, kernel_size=kernel_size, stride=sub) 
            for sub, _ in zip(subsample, range(n_conv_blocks))])
        self.pooling_layer = nn.AdaptiveAvgPool1d(1)
        self.first_dense_layers = nn.ModuleList([nn.Linear(c_h, c_h) for _ in range(n_dense_blocks)])
        self.second_dense_layers = nn.ModuleList([nn.Linear(c_h, c_h) for _ in range(n_dense_blocks)])
        self.output_layer = nn.Linear(c_h, c_out)

    def conv_blocks(self, inp):
        out = inp
        # convolution blocks
        for l in range(self.n_conv_blocks):
            y = pad_layer(out, self.first_conv_layers[l])
            y = self.act(y)
            y = pad_layer(y, self.second_conv_layers[l])
            y = self.act(y)
            if self.subsample[l] > 1:
                out = F.avg_pool1d(out, kernel_size=self.subsample[l], ceil_mode=True)
            out = y + out
        return out

    def dense_blocks(self, inp):
        out = inp
        # dense layers
        for l in range(self.n_dense_blocks):
            y = self.first_dense_layers[l](out)
            y = self.act(y)
            y = self.second_dense_layers[l](y)
            y = self.act(y)
            out = y + out
        return out

    def forward(self, x):
        out = conv_bank(x, self.conv_bank, act=self.act)
        # dimension reduction layer
        out = pad_layer(out, self.in_conv_layer)
        out = self.act(out)
        # conv blocks
        out = self.conv_blocks(out)
        # avg pooling
        out = self.pooling_layer(out).squeeze(2)
        # dense blocks
        out = self.dense_blocks(out)
        out = self.output_layer(out)
        return out

class ContentEncoder(nn.Module):
    def __init__(self, c_in, c_h, c_out, kernel_size,
            bank_size, bank_scale, c_bank, 
            n_conv_blocks, subsample, 
            act):
        super(ContentEncoder, self).__init__()
        self.n_conv_blocks = n_conv_blocks
        self.subsample = subsample
        self.act = get_act(act)
        self.conv_bank = nn.ModuleList(
                [nn.Conv1d(c_in, c_bank, kernel_size=k) for k in range(bank_scale, bank_size + 1, bank_scale)])
        in_channels = c_bank * (bank_size // bank_scale) + c_in
        self.in_conv_layer = nn.Conv1d(in_channels, c_h, kernel_size=1)
        self.first_conv_layers = nn.ModuleList([nn.Conv1d(c_h, c_h, kernel_size=kernel_size) for _ \
                in range(n_conv_blocks)])
        self.second_conv_layers = nn.ModuleList([nn.Conv1d(c_h, c_h, kernel_size=kernel_size, stride=sub) 
            for sub, _ in zip(subsample, range(n_conv_blocks))])
        self.norm_layer = nn.InstanceNorm1d(c_h, affine=False)
        self.out_conv_layer = nn.Conv1d(c_h, c_out, kernel_size=1)

    def forward(self, x):
        out = conv_bank(x, self.conv_bank, act=None)
        out = self.norm_layer(out)
        out = self.act(out)
        # dimension reduction layer
        out = pad_layer(out, self.in_conv_layer)
        # convolution blocks
        for l in range(self.n_conv_blocks):
            y = self.norm_layer(out)
            y = self.act(y)
            y = pad_layer(y, self.first_conv_layers[l])
            y = self.norm_layer(y)
            y = self.act(y)
            y = pad_layer(y, self.second_conv_layers[l])
            if self.subsample[l] > 1:
                out = F.avg_pool1d(out, kernel_size=self.subsample[l], ceil_mode=True)
            out = y + out
        out = pad_layer(out, self.out_conv_layer)
        return out

class Decoder(nn.Module):
    def __init__(self, 
            c_in, c_cond, c_h, c_out, 
            kernel_size,
            n_conv_blocks, upsample, act, sn):
        super(Decoder, self).__init__()
        self.n_conv_blocks = n_conv_blocks
        self.upsample = upsample
        self.act = get_act(act)
        f = spectral_norm if sn else lambda x: x
        self.in_conv_layer = f(nn.Conv1d(c_in, c_h, kernel_size=1))
        self.first_conv_layers = nn.ModuleList([f(nn.Conv1d(c_h, c_h, kernel_size=kernel_size)) for _ \
                in range(n_conv_blocks)])
        self.second_conv_layers = nn.ModuleList(\
                [f(nn.Conv1d(c_h, c_h, kernel_size=kernel_size)) for _ in range(n_conv_blocks)])
        self.norm_layer = nn.InstanceNorm1d(c_h, affine=False)
        self.conv_affine_layers = nn.ModuleList(
                [f(nn.Linear(c_cond, c_h * 2)) for _ in range(n_conv_blocks*2)])
        self.out_conv_layer = f(nn.Conv1d(c_h, c_out, kernel_size=1))

    def forward(self, x, cond):
        out = self.norm_layer(x)
        out = self.act(out)
        out = pad_layer(out, self.in_conv_layer)
        # convolution blocks
        for l in range(self.n_conv_blocks):
            y = self.norm_layer(out)
            y = append_cond(y, self.conv_affine_layers[l*2](cond))
            y = self.act(y)
            y = pad_layer(y, self.first_conv_layers[l])
            y = self.norm_layer(y)
            y = append_cond(y, self.conv_affine_layers[l*2+1](cond))
            y = self.act(y)
            y = pad_layer(y, self.second_conv_layers[l])
            if self.upsample[l] > 1:
                out = upsample(out, scale_factor=self.upsample[l]) 
                y = upsample(y, scale_factor=self.upsample[l]) 
            out = y + out
        out = pad_layer(out, self.out_conv_layer)
        return out

class AE(nn.Module):
    def __init__(self, config):
        super(AE, self).__init__()
        self.speaker_encoder = SpeakerEncoder(**config['SpeakerEncoder']) 
        self.content_encoder = ContentEncoder(**config['ContentEncoder'])
        self.decoder = Decoder(**config['Decoder'])
        self.dummy_speaker_encoder = DummyEncoder(cc(SpeakerEncoder(**config['SpeakerEncoder'])))
        self.dummy_content_encoder = DummyEncoder(cc(ContentEncoder(**config['ContentEncoder'])))

    def forward(self, x, x_neg):
        emb = self.speaker_encoder(x)
        enc = self.content_encoder(x)
        #noise = enc.new(*enc.size()).normal_(0, 1) 
        dec = self.decoder(enc, emb)
        emb_neg = self.speaker_encoder(x_neg)
        enc_neg = self.content_encoder(x_neg)
        #noise = enc.new(*enc.size()).normal_(0, 1) 
        dec_syn_emb = self.decoder(enc_neg, emb)
        dec_syn_enc = self.decoder(enc, emb_neg)
        # latent reconstruction 
        self.dummy_speaker_encoder.load(self.speaker_encoder)
        emb_rec = self.dummy_speaker_encoder(dec_syn_emb)
        self.dummy_content_encoder.load(self.content_encoder)
        enc_rec = self.dummy_content_encoder(dec_syn_enc)
        return enc, emb, dec, enc_rec, emb_rec

    def inference(self, x, x_cond):
        emb = self.speaker_encoder(x_cond)
        enc = self.content_encoder(x)
        dec = self.decoder(enc, emb)
        return dec

    def get_speaker_embeddings(self, x):
        emb = self.speaker_encoder(x)
        return emb
'''
class SpeakerClassifier(nn.Module):
    def __init__(self, input_size, c_in, output_dim, n_dense_layers, d_h, act):
        super(SpeakerClassifier, self).__init__()
        self.act = get_act(act)
        dense_input_size = input_size * c_in
        self.dense_layers = nn.ModuleList([nn.Linear(dense_input_size, d_h)] + 
                [nn.Linear(d_h, d_h) for _ in range(n_dense_layers - 2)] + 
                [nn.Linear(d_h, output_dim)])

    def forward(self, x):
        out = flatten(x)
        for layer in self.dense_layers[:-1]:
            out = self.act(layer(out))
        out = self.dense_layers[-1](out)
        return out

class SpeakerClassifier(nn.Module):
    def __init__(self, input_size, c_in, output_dim, n_dense_layers, d_h, act):
        super(SpeakerClassifier, self).__init__()
        self.act = get_act(act)
        dense_input_size = input_size * c_in
        self.dense_layers = nn.ModuleList([nn.Linear(dense_input_size, d_h)] + 
                [nn.Linear(d_h, d_h) for _ in range(n_dense_layers - 2)] + 
                [nn.Linear(d_h, output_dim)])

    def forward(self, x):
        #out = flatten(x)
        out = x
        for layer in self.dense_layers[:-1]:
            out = self.act(layer(out))
        out = self.dense_layers[-1](out)
        return out

class Discriminator(nn.Module):
    def __init__(self, input_size, 
            c_in, c_h, c_cond, 
            kernel_size, n_conv_blocks,
            subsample, 
            n_dense_layers, d_h, act, sn, ins_norm, dropout_rate):
        super(Discriminator, self).__init__()
        # input_size is a tuple
        self.n_conv_blocks = n_conv_blocks
        self.n_dense_layers = n_dense_layers
        self.subsample = subsample
        self.act = get_act(act)
        self.ins_norm = ins_norm
        # using spectral_norm if specified, or identity function
        f = spectral_norm if sn else lambda x: x
        self.in_conv_layer = f(nn.Conv2d(c_in, c_h, kernel_size=kernel_size))
        self.first_conv_layers = nn.ModuleList(
                [f(nn.Conv2d(c_h, c_h, kernel_size=kernel_size)) for sub in subsample])
        self.second_conv_layers = nn.ModuleList(
                [f(nn.Conv2d(c_h, c_h, kernel_size=kernel_size, stride=(2, sub))) for sub in subsample])
        self.norm_layer = nn.InstanceNorm2d(c_h)
        # to process all frequency
        dense_input_size = input_size 
        for l, sub in zip(range(n_conv_blocks), self.subsample):
            dense_input_size = (ceil(dense_input_size[0] / 2), ceil(dense_input_size[1] / sub))
        self.out_conv_layer = f(nn.Conv2d(c_h, d_h, \
                kernel_size=(dense_input_size[0], 1), \
                stride=(1, 1)))
        dense_input_size = dense_input_size[1] * d_h
        self.dense_layers = nn.ModuleList([f(nn.Linear(dense_input_size, d_h))] + 
                [f(nn.Linear(d_h, d_h)) for _ in range(n_dense_layers - 2)] + 
                [f(nn.Linear(d_h, 1))])
        self.linear_cond = f(nn.Linear(c_cond, d_h, bias=False))
        self.dropout_layer = nn.Dropout(p=dropout_rate)

    def conv_blocks(self, inp):
        out = self.act(pad_layer_2d(inp, self.in_conv_layer))
        for l in range(self.n_conv_blocks):
            y = self.act(pad_layer_2d(out, self.first_conv_layers[l]))
            y = self.dropout_layer(y)
            y = self.act(pad_layer_2d(y, self.second_conv_layers[l]))
            y = self.dropout_layer(y)
            if self.ins_norm:
                y = self.norm_layer(y)
            out = y + F.avg_pool2d(out, kernel_size=(2, self.subsample[l]), ceil_mode=True)
        out = self.out_conv_layer(out).squeeze(2)
        out = self.act(out)
        out = self.dropout_layer(out)
        out = out.view(out.size(0), out.size(1) * out.size(2))
        return out

    def dense_blocks(self, inp):
        h = inp
        for l in range(self.n_dense_layers - 1):
            h = self.dense_layers[l](h)
            h = self.act(h)
            h = self.dropout_layer(h)
        out = self.dense_layers[-1](h)
        return out, h

    def forward(self, x, cond):
        if x.dim() == 3:
            x = x.unsqueeze(1)
        x_vec = self.conv_blocks(x)
        val, h = self.dense_blocks(x_vec)
        cond_val = torch.sum(h * self.linear_cond(cond), dim=1, keepdim=True)
        val += cond_val
        return val, cond_val
'''
