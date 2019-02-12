import torch
import numpy as np
import sys
import os 
import torch.nn as nn
import torch.nn.functional as F
import yaml
import pickle
from model import AE, LatentDiscriminator, EmbeddingDiscriminator
from data_utils import get_data_loader
from data_utils import PickleDataset
from utils import *
from functools import reduce
from collections import defaultdict


class Solver(object):
    def __init__(self, config, args):
        # config store the value of hyperparameters, turn to attr by AttrDict
        self.config = config
        print(config)

        # args store other information
        self.args = args
        print(self.args)

        # logger to use tensorboard
        self.logger = Logger(self.args.logdir)

        # get dataloader
        self.get_data_loaders()

        # init the model with config
        self.build_model()
        self.save_config()

        if args.load_model:
            self.load_model(args.load_opt, args.load_dis)

    def save_model(self, iteration, stage):
        # save model and discriminator and their optimizer
        torch.save(self.model.state_dict(), f'{self.args.store_model_path}-{iteration}.{stage}.ckpt')
        torch.save(self.gen_opt.state_dict(), f'{self.args.store_model_path}-{iteration}.{stage}.opt')
        torch.save(self.discr.state_dict(), f'{self.args.store_model_path}-{iteration}.{stage}.discr')
        torch.save(self.dis_opt.state_dict(), f'{self.args.store_model_path}-{iteration}.{stage}.discr.opt')

    def save_config(self):
        with open(f'{self.args.store_model_path}.config.yaml', 'w') as f:
            yaml.dump(vars(self.config))
        with open(f'{self.args.store_model_path}.args.yaml', 'w') as f:
            yaml.dump(vars(self.args))
        return

    def load_model(self, load_opt, load_dis):
        print(f'Load model from {self.args.load_model_path}, load_opt={load_opt}, load_dis={load_dis}')
        self.model.load_state_dict(torch.load(f'{self.args.load_model_path}.ckpt'))
        if load_dis:
            self.discr.load_state_dict(torch.load(f'{self.args.load_model_path}.discr'))
        if load_opt:
            self.gen_opt.load_state_dict(torch.load(f'{self.args.load_model_path}.opt'))
        if load_dis and load_opt:
            self.dis_opt.load_state_dict(torch.load(f'{self.args.load_model_path}.discr.opt'))
        return

    def get_data_loaders(self):
        data_dir = self.args.data_dir

        self.train_dataset = PickleDataset(os.path.join(data_dir, f'{self.args.train_set}.pkl'), 
                os.path.join(data_dir, self.args.train_index_file), 
                segment_size=self.config.segment_size)

        self.val_dataset = PickleDataset(os.path.join(data_dir, f'{self.args.val_set}.pkl'), 
                os.path.join(data_dir, self.args.val_index_file), 
                segment_size=self.config.segment_size)

        self.train_loader = get_data_loader(self.train_dataset, 
                batch_size=self.config.batch_size, 
                shuffle=self.config.shuffle, 
                num_workers=4, drop_last=False)

        self.val_loader = get_data_loader(self.val_dataset, 
                batch_size=self.config.batch_size, 
                shuffle=self.config.shuffle, 
                num_workers=4, drop_last=False)

        self.train_iter = infinite_iter(self.train_loader)
        return

    def build_model(self): 
        # create model, discriminator, optimizers
        self.model = cc(AE(c_in=self.config.c_in,
                c_h=self.config.c_h,
                c_latent=self.config.c_latent,
                c_cond=self.config.c_cond,
                c_out=self.config.c_in,
                kernel_size=self.config.kernel_size,
                bank_size=self.config.bank_size,
                bank_scale=self.config.bank_scale,
                s_enc_n_conv_blocks=self.config.s_enc_n_conv_blocks,
                s_enc_n_dense_blocks=self.config.s_enc_n_dense_blocks,
                d_enc_n_conv_blocks=self.config.d_enc_n_conv_blocks,
                d_enc_n_dense_blocks=self.config.d_enc_n_dense_blocks,
                s_subsample=self.config.s_subsample,
                d_subsample=self.config.d_subsample,
                dec_n_conv_blocks=self.config.dec_n_conv_blocks,
                dec_n_dense_blocks=self.config.dec_n_dense_blocks,
                upsample=self.config.upsample,
                act=self.config.act,
                dropout_rate=self.config.dropout_rate))
        print(self.model)

        discr_input_size = self.config.segment_size / reduce(lambda x, y: x*y, self.config.d_subsample)

        self.discr = cc(LatentDiscriminator(input_size=discr_input_size,
                c_in=self.config.c_latent, 
                c_h=self.config.dis_c_h, 
                kernel_size=self.config.dis_kernel_size,
                n_conv_layers=self.config.dis_n_conv_layers,
                n_dense_layers=self.config.dis_n_dense_layers,
                d_h=self.config.dis_d_h, 
                act=self.config.act, 
                dropout_rate=self.config.dis_dropout_rate))
        print(self.discr)

        self.emb_discr = cc(EmbeddingDiscriminator(input_size=self.config.c_cond,
            d_h=self.config.emb_dis_d_h,
            act=self.config.act,
            n_dense_layers=self.config.emb_dis_n_dense_layers,
            dropout_rate=self.config.emb_dis_dropout_rate))
        print(self.emb_discr)

        self.gen_opt = torch.optim.Adam(self.model.parameters(), 
                lr=self.config.gen_lr, betas=(self.config.beta1, self.config.beta2), 
                amsgrad=self.config.amsgrad)  
        self.dis_opt = torch.optim.Adam(self.discr.parameters(), 
                lr=self.config.dis_lr, betas=(self.config.beta1, self.config.beta2), 
                amsgrad=self.config.amsgrad) 
        self.emb_dis_opt = torch.optim.Adam(self.emb_discr.parameters(),
                lr=self.config.emb_dis_lr, betas=(self.config.beta1, self.config.beta2), 
                amsgrad=self.config.amsgrad)
        print(self.gen_opt)
        print(self.dis_opt)
        print(self.emb_dis_opt)
        self.noise_adder = NoiseAdder(0, self.config.gaussian_std)
        return

    def ae_pretrain_step(self, data):
        x, x_pos, x_neg = [cc(tensor) for tensor in data]
        if self.config.add_gaussian:
            enc, emb_pos, dec = self.model(self.noise_adder(x), 
                    self.noise_adder(x_pos), 
                    self.noise_adder(x_neg), 
                    mode='ae')
        else:
            enc, emb_pos, dec = self.model(x, 
                    x_pos, 
                    x_neg,
                    mode='ae')

        loss_rec = torch.mean(torch.abs(x - dec))

        meta = {'loss_rec': loss_rec.item()}

        self.gen_opt.zero_grad()
        loss_rec.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=self.config.grad_norm)
        self.gen_opt.step()
        return meta

    def ae_latent_step(self, data, lambda_sim, lambda_dis):
        x, x_pos, x_neg = [cc(tensor) for tensor in data]
        if self.config.add_gaussian:
            enc, enc_pos, enc_neg, emb, emb_pos, emb_neg, dec = self.model(self.noise_adder(x), 
                    self.noise_adder(x_pos), 
                    self.noise_adder(x_neg),
                    mode='latent_ae')
        else:
            enc, enc_pos, enc_neg, emb, emb_pos, emb_neg, dec = self.model(x, 
                    x_pos, 
                    x_neg, 
                    mode='latent_ae')

        loss_rec = torch.mean(torch.abs(x - dec))

        criterion = nn.BCEWithLogitsLoss()

        emb_pos_val, emb_neg_val = self.emb_discr(emb, emb_pos, emb_neg)
        ones_label = emb_pos_val.new_ones(*emb_pos_val.size())
        zeros_label = emb_neg_val.new_zeros(*emb_neg_val.size())
        loss_emb_pos = criterion(emb_pos_val, ones_label)
        loss_emb_neg = criterion(emb_neg_val, zeros_label)
        loss_sim = (loss_emb_pos + loss_emb_neg) / 2 

        pos_val, neg_val = self.discr(enc, enc_pos, enc_neg)
        halfs_label = neg_val.new_ones(*neg_val.size()) * 0.5
        loss_pos = criterion(pos_val, halfs_label)
        loss_neg = criterion(neg_val, halfs_label)

        loss_dis = (loss_pos + loss_neg) / 2

        loss = loss_rec + lambda_sim * loss_sim + lambda_dis * loss_dis

        meta = {'loss_rec': loss_rec.item(),
                'loss_emb_pos': loss_emb_pos.item(),
                'loss_emb_neg': loss_emb_neg.item(),
                'loss_sim': loss_sim.item(),
                'loss_pos': loss_pos.item(),
                'loss_neg': loss_neg.item(),
                'loss_dis': loss_dis.item(),
                'loss': loss.item()}

        self.gen_opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=self.config.grad_norm)
        self.gen_opt.step()

        return meta

    def dis_emb_latent_step(self, data, lambda_sim):
        x, x_pos, x_neg = [cc(tensor) for tensor in data]

        with torch.no_grad():
            if self.config.add_gaussian:
                emb, emb_pos, emb_neg = self.model(self.noise_adder(x), 
                        self.noise_adder(x_pos), 
                        self.noise_adder(x_neg), 
                        mode='latent_emb_dis')
            else:
                emb, emb_pos, emb_neg = self.model(x, 
                        x_pos, 
                        x_neg, 
                        mode='latent_emb_dis')

        # input for the discriminator
        pos_val, neg_val = self.emb_discr(emb, emb_pos, emb_neg)

        ones_label = pos_val.new_ones(*pos_val.size())
        zeros_label = neg_val.new_zeros(*neg_val.size())

        criterion = nn.BCEWithLogitsLoss()

        loss_pos = criterion(pos_val, ones_label)
        loss_neg = criterion(neg_val, zeros_label)

        loss_sim = (loss_pos + loss_neg) / 2
        loss = lambda_sim * loss_sim

        self.emb_dis_opt.zero_grad()
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(self.emb_discr.parameters(), max_norm=self.config.grad_norm)
        self.emb_dis_opt.step()

        pos_probs = torch.sigmoid(pos_val)
        neg_probs = torch.sigmoid(neg_val)

        acc_pos = torch.mean((pos_probs >= 0.5).float())
        acc_neg = torch.mean((neg_probs < 0.5).float())
        acc = (acc_pos + acc_neg) / 2

        meta = {'loss_sim': loss_sim.item(),
                'loss_pos': loss_pos.item(),
                'loss_neg': loss_neg.item(),
                'pos_prob': torch.mean(pos_probs).item(),
                'neg_prob': torch.mean(neg_probs).item(),
                'acc_pos': acc_pos.item(),
                'acc_neg': acc_neg.item(),
                'acc': acc.item()}

        return meta

    def dis_latent_step(self, data, lambda_dis):
        x, x_pos, x_neg = [cc(tensor) for tensor in data]

        with torch.no_grad():
            if self.config.add_gaussian:
                enc, enc_pos, enc_neg = self.model(self.noise_adder(x), 
                        self.noise_adder(x_pos), 
                        self.noise_adder(x_neg), 
                        mode='latent_dis')
            else:
                enc, enc_pos, enc_neg = self.model(x, 
                        x_pos, 
                        x_neg, 
                        mode='latent_dis')

        # input for the discriminator
        pos_val, neg_val = self.discr(enc, enc_pos, enc_neg)

        ones_label = pos_val.new_ones(*pos_val.size())
        zeros_label = neg_val.new_zeros(*neg_val.size())

        criterion = nn.BCEWithLogitsLoss()

        loss_pos = criterion(pos_val, ones_label)
        loss_neg = criterion(neg_val, zeros_label)

        loss_dis = (loss_pos + loss_neg) / 2
        loss = lambda_dis * loss_dis

        self.dis_opt.zero_grad()
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(self.discr.parameters(), max_norm=self.config.grad_norm)
        self.dis_opt.step()

        pos_probs = torch.sigmoid(pos_val)
        neg_probs = torch.sigmoid(neg_val)

        acc_pos = torch.mean((pos_probs >= 0.5).float())
        acc_neg = torch.mean((neg_probs < 0.5).float())
        acc = (acc_pos + acc_neg) / 2

        meta = {'loss_dis': loss_dis.item(),
                'loss_pos': loss_pos.item(),
                'loss_neg': loss_neg.item(),
                'pos_prob': torch.mean(pos_probs).item(),
                'neg_prob': torch.mean(neg_probs).item(),
                'acc_pos': acc_pos.item(),
                'acc_neg': acc_neg.item(),
                'acc': acc.item()}

        return meta

    def ae_pretrain(self, n_iterations):
        for iteration in range(n_iterations):
            data = next(self.train_iter)
            meta = self.ae_pretrain_step(data)

            # add to logger
            self.logger.scalars_summary(f'{self.args.tag}/ae_pretrain', meta, iteration)
            loss_rec = meta['loss_rec']
            print(f'AE:[{iteration + 1}/{n_iterations}], loss_rec={loss_rec:.2f}', end='\r')

            if (iteration + 1) % self.args.summary_steps == 0 or iteration + 1 == n_iterations:
                self.save_model(iteration=iteration, stage='ae')
                print()
        return

    def dis_latent_pretrain(self, n_iterations):
        for iteration in range(n_iterations):
            data = next(self.train_iter)
            meta = self.dis_latent_step(data, lambda_dis=1.0)
            self.logger.scalars_summary(f'{self.args.tag}/dis_pretrain', meta, iteration)

            loss_pos = meta['loss_pos']
            loss_neg = meta['loss_neg']
            acc = meta['acc']

            print(f'D:[{iteration + 1}/{n_iterations}], loss_pos={loss_pos:.2f}, loss_neg={loss_neg:.2f}, '
                    f'acc={acc:.2f}', end='\r')

            if (iteration + 1) % self.args.summary_steps == 0 or iteration + 1 == n_iterations:
                self.save_model(iteration=iteration, stage='dis')
                print()
        return

    def emb_dis_pretrain(self, n_iterations):
        for iteration in range(n_iterations):
            data = next(self.train_iter)
            meta = self.dis_emb_latent_step(data, lambda_sim=1.0)
            self.logger.scalars_summary(f'{self.args.tag}/emb_dis_pretrain', meta, iteration)

            loss_pos = meta['loss_pos']
            loss_neg = meta['loss_neg']
            acc = meta['acc']

            print(f'embD:[{iteration + 1}/{n_iterations}], loss_pos={loss_pos:.2f}, loss_neg={loss_neg:.2f}, '
                    f'acc={acc:.2f}', end='\r')

            if (iteration + 1) % self.args.summary_steps == 0 or iteration + 1 == n_iterations:
                self.save_model(iteration=iteration, stage='emb_dis')
                print()
        return

    def train(self, n_iterations):
        for iteration in range(n_iterations):
            # calculate linear increasing lambda_dis
            if iteration >= self.config.sched_iters:
                lambda_dis = self.config.lambda_dis
            else:
                lambda_dis = self.config.lambda_dis * (iteration + 1) / self.config.sched_iters
            # AE step
            for ae_step in range(self.config.ae_steps):
                data = next(self.train_iter)
                gen_meta = self.ae_latent_step(data, lambda_sim=self.config.lambda_sim, lambda_dis=lambda_dis)
                self.logger.scalars_summary(f'{self.args.tag}/ae_train', gen_meta, 
                        iteration * self.config.ae_steps + ae_step)

            # D step
            for dis_step in range(self.config.dis_steps):
                data = next(self.train_iter)
                dis_meta = self.dis_latent_step(data, lambda_dis=1.0)
                self.logger.scalars_summary(f'{self.args.tag}/dis_train', dis_meta, 
                        iteration * self.config.dis_steps + dis_step)

            # emb_D step
            for dis_step in range(self.config.emb_dis_steps):
                data = next(self.train_iter)
                emb_dis_meta = self.dis_emb_latent_step(data, lambda_sim=1.0)
                self.logger.scalars_summary(f'{self.args.tag}/emb_dis_train', emb_dis_meta, 
                        iteration * self.config.emb_dis_steps + dis_step)

            loss_rec = gen_meta['loss_rec']
            loss_sim = gen_meta['loss_sim']
            loss_dis = gen_meta['loss_dis']
            acc = dis_meta['acc']

            print(f'[{iteration + 1}/{n_iterations}], loss_rec={loss_rec:.2f}, loss_sim={loss_sim:.2f}, '
                    f'loss_dis={loss_dis:.2f}, acc={acc:.2f}, lambda_dis={lambda_dis:.1e}', 
                    end='\r')

            if (iteration + 1) % self.args.summary_steps == 0 or iteration + 1 == n_iterations:
                print()
                self.save_model(iteration=iteration, stage='main')
