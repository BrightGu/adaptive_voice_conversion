import torch
import numpy as np
import sys
import os 
import torch.nn as nn
import torch.nn.functional as F
import yaml
import pickle
import glob
from model import AE
from data_utils import get_data_loader
from data_utils import PickleDataset, SequenceDataset
from utils import *
from functools import reduce
import json
from collections import defaultdict
from torch.utils.data import Dataset
from torch.utils.data import TensorDataset
from torch.utils.data import DataLoader
from argparse import ArgumentParser, Namespace
from sklearn.manifold import TSNE
from sklearn.decomposition import PCA
import matplotlib as mpl
mpl.use('Agg')
from matplotlib import pyplot as plt
from scipy.io.wavfile import write
import random
from preprocess.tacotron.utils import melspectrogram2wav
import librosa 

class Evaluater(object):
    def __init__(self, config, args):
        # config store the value of hyperparameters, turn to attr by AttrDict
        self.config = config
        print(config)
        # args store other information
        self.args = args
        print(self.args)

        # get dataloader
        self.load_data()

        # init the model with config
        self.build_model()

        # load model
        self.load_model()
        # read speaker info
        self.speaker2gender = self.read_speaker_gender(self.args.speaker_info_path)
        # sampled n speakers for evaluation
        self.sample_n_speakers(self.args.n_speakers)
        with open(os.path.join(self.args.data_dir, 'attr.pkl'), 'rb') as f:
            self.attr = pickle.load(f)
        #self.path_dict = self.read_vctk_pathes()

    def load_model(self):
        print(f'Load model from {self.args.load_model_path}')
        self.model.load_state_dict(torch.load(f'{self.args.load_model_path}.ckpt'))
        return

    def read_vctk_pathes(self):
        vctk_dir = self.args.vctk_dir
        path_dict = defaultdict(lambda : [])
        for speaker_dir in sorted(glob.glob(os.path.join(vctk_dir, '*'))):
            for path in sorted(glob.glob(os.path.join(speaker_dir, '*'))):
                speaker = path.strip().split('/')[-1][:4]
                path_dict[speaker].append(path)
        return path_dict

    def generate_mos_samples(self, src_speaker, tar_speaker, output_dir, n_samples):
        src_utts = random.choices(self.path_dict[src_speaker], k=n_samples)
        tar_utts = random.choices(self.path_dict[tar_speaker], k=n_samples)
        for src_utt, tar_utt in zip(src_utts, tar_utts):
            # down-sampling
            wav_data = self.trimmed_and_downsample(src_utt)
            src_utt_id = src_utt.split('/')[-1]
            tar_utt_id = tar_utt.split('/')[-1]
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id}_ori.wav'))
            src_mel = self.pkl_data[src_utt_id]
            tar_mel = self.pkl_data[tar_utt_id]
            print(src_mel.shape, tar_mel.shape)
            wav_data = melspectrogram2wav(self.denormalize(src_mel))
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id}_resyn.wav'))
            wav_data, _ = self.inference_one_utterance(torch.from_numpy(src_mel).cuda(), torch.from_numpy(src_mel).cuda())
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id}_rec.wav'))
            wav_data, _ = self.inference_one_utterance(torch.from_numpy(src_mel).cuda(), torch.from_numpy(tar_mel).cuda())
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id}_con.wav'))
        return

    def plot_conversion_samples_gv(self, src_speaker, tar_speaker, output_dir, n_samples):
        src_utts = random.choices(self.path_dict[src_speaker], k=n_samples)
        tar_utts = random.choices(self.path_dict[tar_speaker], k=n_samples)
        comp_tar_utts = random.choices(self.path_dict[tar_speaker], k=n_samples)
        src, conv, tar = [], [], []
        for src_utt, tar_utt, comp_tar_utt in zip(src_utts, tar_utts, comp_tar_utts):
            src_utt_id = src_utt.split('/')[-1]
            tar_utt_id = tar_utt.split('/')[-1]
            comp_tar_utt_id = comp_tar_utt.split('/')[-1]
            src_mel = self.pkl_data[src_utt_id]
            tar_mel = self.pkl_data[tar_utt_id]
            comp_tar_mel = self.pkl_data[comp_tar_utt_id]
            _, dec = self.inference_one_utterance(torch.from_numpy(src_mel).cuda(), torch.from_numpy(tar_mel).cuda())
            conv.append(dec)
            comp_tar_mel = self.denormalize(comp_tar_mel)
            tar.append(comp_tar_mel)
            src_mel = self.denormalize(src_mel)
            src.append(src_mel)
        conv = np.concatenate(conv)
        gv_conv = conv.var(axis=0)
        tar = np.concatenate(tar)
        gv_tar = tar.var(axis=0)
        src = np.concatenate(src)
        gv_src = src.var(axis=0)
        plt.plot(gv_conv, color='y', label='converted')
        plt.plot(gv_tar, color='g', label='target speaker')
        #plt.plot(gv_src, color='tab:purple', label='source speaker')
        plt.ylabel('gv', fontsize=20)
        #plt.grid(True)
        plt.xlabel('freq index', fontsize=20)
        plt.legend(loc='upper right', prop={'size': 11})
        plt.savefig(os.path.join(output_dir, f'{src_speaker}_{tar_speaker}.png'))
        plt.clf()
        plt.cla()
        plt.close()
        return

    def generate_conversion_samples(self, src_speaker, tar_speaker, output_dir, n_samples):
        src_utts = random.choices(self.path_dict[src_speaker], k=n_samples)
        src_comp_utts = random.choices(self.path_dict[src_speaker], k=n_samples)
        src_comp2_utts = random.choices(self.path_dict[src_speaker], k=n_samples)
        tar_utts = random.choices(self.path_dict[tar_speaker], k=n_samples)
        tar_comp_utts = random.choices(self.path_dict[tar_speaker], k=n_samples)
        for src_utt, src_comp_utt, src_comp2_utt, tar_utt, tar_comp_utt in zip(src_utts, src_comp_utts, src_comp2_utts, tar_utts, tar_comp_utts):
            src_utt_id = src_utt.split('/')[-1]
            tar_utt_id = tar_utt.split('/')[-1]
            src_comp_utt_id = src_comp_utt.split('/')[-1]
            src_comp2_utt_id = src_comp2_utt.split('/')[-1]
            tar_comp_utt_id = tar_comp_utt.split('/')[-1]
            src_mel = self.pkl_data[src_utt_id]
            tar_mel = self.pkl_data[tar_utt_id]
            src_comp_mel = self.pkl_data[src_comp_utt_id]
            src_comp2_mel = self.pkl_data[src_comp2_utt_id]
            tar_comp_mel = self.pkl_data[tar_comp_utt_id]
            #print(src_mel.shape, tar_mel.shape)
            wav_data = melspectrogram2wav(self.denormalize(src_comp_mel))
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id[:8]}_{tar_utt_id[:8]}_comp_src.wav'))
            wav_data = melspectrogram2wav(self.denormalize(tar_comp_mel))
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id[:8]}_{tar_utt_id[:8]}_comp_tar.wav'))
            wav_data = melspectrogram2wav(self.denormalize(src_mel))
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id[:8]}_{tar_utt_id[:8]}_src.wav'))
            wav_data = melspectrogram2wav(self.denormalize(tar_mel))
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id[:8]}_{tar_utt_id[:8]}_tar.wav'))
            wav_data, _ = self.inference_one_utterance(torch.from_numpy(src_mel).cuda(), torch.from_numpy(tar_mel).cuda())
            self.write_wav_to_file(wav_data, output_path=os.path.join(output_dir, f'{src_utt_id[:8]}_{tar_utt_id[:8]}_con.wav'))
        return

    def load_data(self):
        data_dir = self.args.data_dir
        # load pkl data and sampled segments
        with open(os.path.join(data_dir, f'{self.args.val_set}.pkl'), 'rb') as f:
            self.pkl_data = pickle.load(f)
        with open(os.path.join(data_dir, self.args.val_index_file), 'r') as f:
            self.indexes = json.load(f)
        return

    def build_model(self): 
        # create model, discriminator, optimizers
        self.model = cc(AE(self.config))
        print(self.model)
        self.model.eval()
        return

    def sample_n_speakers(self, n_speakers):
        # only apply on VCTK corpus
        self.speakers = sorted(list(set([key.split('_')[0] for key in self.pkl_data])))
        # first n speakers are sampled
        self.sampled_speakers = self.speakers[:n_speakers]
        self.speaker_index = {speaker:i for i, speaker in enumerate(self.sampled_speakers)}
        return

    def read_speaker_gender(self, speaker_path):
        speaker2gender = {}
        with open(speaker_path, 'r') as f:
            for i, line in enumerate(f):
                if i == 0:
                    continue
                sid, gender, _ = line.strip().split('\t', maxsplit=2)
                speaker2gender[sid] = gender
        return speaker2gender

    def plot_spectrograms(self, data, pic_path):
        # data = [T, F]
        data = data.T
        print(data.shape)
        plt.pcolor(data, cmap=plt.cm.Blues)
        plt.xlabel('time', fontsize=20)
        plt.ylabel('Frequency', fontsize=20)
        plt.savefig(pic_path)
        plt.clf()
        plt.cla()
        plt.close()
        return

    def plot_speaker_embeddings(self, output_path):
        # hack code
        small_pkl_data = {key: val for key, val in self.pkl_data.items() \
                if key.split('_')[0] in self.sampled_speakers and val.shape[0] > 128}
        speakers = [key.split('_')[0] for key in small_pkl_data.keys()]
        dataset = SequenceDataset(small_pkl_data)
        dataloader = DataLoader(dataset, batch_size=1, shuffle=False, num_workers=0)
        all_embs = []
        # run the model 
        for data in dataloader:
            data = cc(data)
            embs = self.model.get_speaker_embeddings(data)
            all_embs = all_embs + embs.detach().cpu().numpy().tolist()
        all_embs = np.array(all_embs)
        norms = np.sqrt(np.sum(all_embs ** 2, axis=1, keepdims=True))
        print(norms.mean())
        all_embs = all_embs / norms 
        print(all_embs.shape)
        # TSNE
        embs_2d = TSNE(n_components=2, init='pca', perplexity=50).fit_transform(all_embs)
        x_min, x_max = embs_2d.min(0), embs_2d.max(0)
        embs_norm = (embs_2d - x_min) / (x_max - x_min)
        # plot to figure
        female_cluster = [i for i, speaker in enumerate(speakers) if self.speaker2gender[speaker] == 'F']
        male_cluster = [i for i, speaker in enumerate(speakers) if self.speaker2gender[speaker] == 'M']
        colors = np.array([self.speaker_index[speaker] for speaker in speakers])
        plt.scatter(embs_norm[female_cluster, 0], embs_norm[female_cluster, 1], 
                c=colors[female_cluster], marker='x') 
        plt.scatter(embs_norm[male_cluster, 0], embs_norm[male_cluster, 1], 
                c=colors[male_cluster], marker='o') 
        plt.savefig(output_path)
        plt.clf()
        plt.cla()
        plt.close()
        return

    def plot_segment_embeddings(self, output_path):
        # filter the samples by speakers sampled
        # hack code 
        small_indexes = [index for index in self.indexes if index[3].split('_')[0] in self.sampled_speakers]
        random.shuffle(small_indexes)
        small_indexes = small_indexes[:self.args.max_samples]
        # generate the tensor and dataloader for evaluation
        tensor = [self.pkl_data[key][t:t + self.config['data_loader']['segment_size']] for _, _, _, key, t in small_indexes]
        speakers = [key.split('_')[0] for _, _, _, key, _  in small_indexes]
        # add the dimension for channel
        tensor = self.seg_make_frames(torch.from_numpy(np.array(tensor)))
        dataset = TensorDataset(tensor)
        dataloader = DataLoader(dataset, batch_size=20, shuffle=False, num_workers=0)
        all_embs = []
        # run the model 
        for data in dataloader:
            data = cc(data[0])
            embs = self.model.get_speaker_embeddings(data)
            all_embs = all_embs + embs.detach().cpu().numpy().tolist()
        all_embs = np.array(all_embs)
        norms = np.sqrt(np.sum(all_embs ** 2, axis=1, keepdims=True))
        print(norms.mean())
        all_embs = all_embs / norms 
        print(all_embs.shape)
        # TSNE
        embs_2d = TSNE(n_components=2, init='pca', perplexity=50).fit_transform(all_embs)
        x_min, x_max = embs_2d.min(0), embs_2d.max(0)
        embs_norm = (embs_2d - x_min) / (x_max - x_min)
        # plot to figure
        female_cluster = [i for i, speaker in enumerate(speakers) if self.speaker2gender[speaker] == 'F']
        male_cluster = [i for i, speaker in enumerate(speakers) if self.speaker2gender[speaker] == 'M']
        colors = np.array([self.speaker_index[speaker] for speaker in speakers])
        plt.scatter(embs_norm[female_cluster, 0], embs_norm[female_cluster, 1], 
                c=colors[female_cluster], marker='x') 
        plt.scatter(embs_norm[male_cluster, 0], embs_norm[male_cluster, 1], 
                c=colors[male_cluster], marker='o') 
        plt.savefig(output_path)
        plt.clf()
        plt.cla()
        plt.close()
        return

    def utt_make_frames(self, x):
        frame_size = self.config['data_loader']['frame_size']
        remains = x.size(0) % frame_size 
        if remains != 0:
            x = F.pad(x, (0, remains))
        out = x.view(1, x.size(0) // frame_size, frame_size * x.size(1)).transpose(1, 2)
        return out

    def seg_make_frames(self, xs):
        # xs = [batch_size, segment_size, channels]
        # ys = [batch_size, frame_size, segment_size // frame_size]
        frame_size = self.config['data_loader']['frame_size']
        ys = xs.view(xs.size(0), xs.size(1) // frame_size, frame_size * xs.size(2)).transpose(1, 2)
        return ys

    def inference_one_utterance(self, x, x_cond):
        x = self.utt_make_frames(x)
        x_cond = self.utt_make_frames(x_cond)
        dec = self.model.inference(x, x_cond)
        dec = dec.transpose(1, 2).squeeze(0)
        dec = dec.detach().cpu().numpy()
        print(x.mean(), dec.mean())
        dec = self.denormalize(dec)
        wav_data = melspectrogram2wav(dec)
        return wav_data, dec

    def denormalize(self, x):
        m, s = self.attr['mean'], self.attr['std']
        ret = x * s + m
        return ret

    def write_wav_to_file(self, wav_data, output_path):
        write(output_path, rate=24000, data=wav_data)
        return

    def trimmed_and_downsample(self, fpath):
        y, sr = librosa.load(fpath, sr=24000)
        y, _ = librosa.effects.trim(y, top_db=15)
        return y 

    def infer_default(self):
        # using the first sample from in_test
        content_utt, _, cond_utt, _ = self.indexes[6]
        #content_utt = 'p262_027.wav'
        #cond_utt = 'p256_150.wav'
        print(content_utt, cond_utt)
        content = torch.from_numpy(self.pkl_data[content_utt]).cuda()
        cond = torch.from_numpy(self.pkl_data[cond_utt]).cuda()
        self.plot_spectrograms(self.denormalize(content.cpu().numpy()), f'{args.output_path}.src.png')
        self.write_wav_to_file(melspectrogram2wav(self.denormalize(content.cpu().numpy())), 
                f'{args.output_path}.src.wav')
        self.plot_spectrograms(self.denormalize(cond.cpu().numpy()), f'{args.output_path}.tar.png')
        self.write_wav_to_file(melspectrogram2wav(self.denormalize(cond.cpu().numpy())), 
                f'{args.output_path}.tar.wav')
        wav_data, dec = self.inference_one_utterance(content, cond)
        self.plot_spectrograms(dec, f'{args.output_path}.src2tar.png')
        self.write_wav_to_file(wav_data, f'{args.output_path}.src2tar.wav')
        wav_data, dec = self.inference_one_utterance(cond, content)
        self.plot_spectrograms(dec, f'{args.output_path}.tar2src.png')
        self.write_wav_to_file(wav_data, f'{args.output_path}.tar2src.wav')
        # reconstruction
        wav_data, dec = self.inference_one_utterance(content, content)
        self.plot_spectrograms(dec, f'{args.output_path}.rec_src.png')
        self.write_wav_to_file(wav_data, f'{args.output_path}.rec_src.wav')
        wav_data, dec = self.inference_one_utterance(cond, cond)
        self.plot_spectrograms(dec, f'{args.output_path}.rec_tar.png')
        self.write_wav_to_file(wav_data, f'{args.output_path}.rec_tar.wav')
        return


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('-data_dir', '-d', 
            default='/storage/feature/voice_conversion/trimmed_vctk_spectrograms/sr_24000_hop_300/')
    parser.add_argument('-val_set', default='in_test')
    parser.add_argument('-val_index_file', default='in_test_samples_128.json')
    parser.add_argument('-load_model_path', default='/storage/model/adaptive_vc/model')
    parser.add_argument('--plot_speakers', action='store_true')
    parser.add_argument('-speakers_output_path', default='tmp_result/speaker.png')
    parser.add_argument('--plot_segments', action='store_true')
    parser.add_argument('-segments_output_path', default='tmp_result/segment.png')
    parser.add_argument('-spec_output_path', default='spec')
    parser.add_argument('-n_speakers', default=20, type=int)
    parser.add_argument('-speaker_info_path', default='/dataset/LibriTTS/speakers.tsv')
    parser.add_argument('-max_samples', default=3000, type=int)
    parser.add_argument('--infer_default', action='store_true')
    parser.add_argument('--gen_mos', action='store_true')
    parser.add_argument('--plot_gv', action='store_true')
    parser.add_argument('--gen_conv', action='store_true')
    parser.add_argument('-src_speaker')
    parser.add_argument('-tar_speaker')
    parser.add_argument('-output_dir')
    parser.add_argument('-n_samples', type=int)
    parser.add_argument('-output_path', default='tmp_result/test')
    parser.add_argument('-vctk_dir', default='/storage/datasets/VCTK/VCTK-Corpus/wav48')

    args = parser.parse_args()
    # load config file 
    with open(f'{args.load_model_path}.config.yaml') as f:
        config = yaml.load(f)
    evaluator = Evaluater(config=config, args=args)
    if args.plot_speakers:
        evaluator.plot_speaker_embeddings(args.speakers_output_path)

    if args.plot_segments:
        evaluator.plot_segment_embeddings(args.segments_output_path)

    if args.infer_default:
        evaluator.infer_default()

    if args.gen_mos:
        evaluator.generate_mos_samples(args.src_speaker, args.tar_speaker, output_dir=args.output_dir, n_samples=args.n_samples)
    if args.gen_conv:
        evaluator.generate_conversion_samples(args.src_speaker, args.tar_speaker, output_dir=args.output_dir, n_samples=args.n_samples)
    if args.plot_gv:
        evaluator.plot_conversion_samples_gv(args.src_speaker, args.tar_speaker, output_dir=args.output_dir, n_samples=args.n_samples)
