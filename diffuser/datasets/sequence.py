from collections import namedtuple
import numpy as np
import torch
import pdb

from .preprocessing import get_preprocess_fn
from .d4rl import load_environment, sequence_dataset
from .normalization import DatasetNormalizer
from .buffer import ReplayBuffer

import time


RewardBatch = namedtuple('Batch', 'trajectories conditions language')
Batch = namedtuple('Batch', 'trajectories conditions')
ValueBatch = namedtuple('ValueBatch', 'trajectories conditions values')


class SequenceDataset(torch.utils.data.Dataset):

    def __init__(self, env='hopper-medium-replay', horizon=64,
        normalizer='LimitsNormalizer', preprocess_fns=[], max_path_length=1000,
        max_n_episodes=10000, termination_penalty=0, use_padding=True, seed=None, 
        use_npy_inputs=False, use_normed_inputs=True, use_language=True):
        self.preprocess_fn = get_preprocess_fn(preprocess_fns, env)
        self.horizon = horizon
        self.max_path_length = max_path_length
        self.use_padding = use_padding

        use_npy_inputs = True
        use_normed_inputs = False
        self.use_npy_inputs = use_npy_inputs
        self.use_normed_inputs = use_normed_inputs
        self.use_language = use_language

        if self.use_npy_inputs:
            if use_normed_inputs:
                """normed_observations = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/normed_observations.npy')
                normed_actions = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/normed_actions.npy')
                language = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/language.npy')

                path_len_each = 32
                path_lengths_ = np.full(normed_observations.shape[0], path_len_each) # len number of episodes and each entry is the horizon (second element of shape)
                self.indices = self.make_indices(path_lengths_, horizon)
                self.normalizer = DatasetNormalizer(fields, normalizer, path_lengths=path_lengths_)

                self.fields = {}
                self.fields['normed_observations'] = normed_observations
                self.fields['normed_actions'] = normed_actions

                self.observation_dim = normed_observations.shape[-1] # last dim (embedding)
                self.action_dim = normed_actions.shape[-1] # last dim (embedding)
                self.n_episodes = normed_observations.shape[0]"""
                pass
            else:
                """Reading in the .npy files (saved data format)"""
                #t1= time.time()
                observations = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/observations.npy')
                actions = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/actions.npy')
                language = np.load('/iliad/u/manasis/language-diffuser/code/dataset_npy_files/language.npy')
                #print("\n\ntime diff load: ", (time.time() - t1)/60)

                fields = {}
                fields['observations'] = observations
                fields['actions'] = actions
                fields['language'] = language

                path_len_each = 32
                path_lengths_ = np.full(fields['observations'].shape[0], path_len_each) # len number of episodes and each entry is the horizon (second element of shape)
                self.normalizer = DatasetNormalizer(fields, normalizer, path_lengths=path_lengths_)
                #import pdb;pdb.set_trace()
                self.indices = self.make_indices(path_lengths_, horizon)
                
                self.observation_dim = fields['observations'].shape[-1] # last dim (embedding)
                self.action_dim = fields['actions'].shape[-1] # last dim (embedding)
                self.fields = fields
                self.n_episodes = fields['observations'].shape[0] # first dim (num of episodes)
                self.path_lengths = path_lengths_
                #fields['path_lengths'] = 
                self.normalize()
        else:
            self.max_path_length = max_path_length = 1000
            #horizon = 4

            self.env = env = load_environment(env)
            self.env.seed(seed)
            itr = sequence_dataset(env, self.preprocess_fn)

            fields = ReplayBuffer(max_n_episodes, max_path_length, termination_penalty)
            for i, episode in enumerate(itr):
                fields.add_path(episode)
            fields.finalize()

            #import pdb;pdb.set_trace()

            self.normalizer = DatasetNormalizer(fields, normalizer, path_lengths=fields['path_lengths'])
            self.indices = self.make_indices(fields.path_lengths, horizon)

            self.observation_dim = fields.observations.shape[-1]
            self.action_dim = fields.actions.shape[-1]
            self.fields = fields
            self.n_episodes = fields.n_episodes
            self.path_lengths = fields.path_lengths
            self.normalize()

            print(fields)
            # shapes = {key: val.shape for key, val in self.fields.items()}
            # print(f'[ datasets/mujoco ] Dataset fields: {shapes}')

    def normalize(self, keys=['observations', 'actions']):
        '''
            normalize fields that will be predicted by the diffusion model
        '''
        for key in keys:
            array = self.fields[key].reshape(self.n_episodes*self.max_path_length, -1)
            normed = self.normalizer(array, key)
            self.fields[f'normed_{key}'] = normed.reshape(self.n_episodes, self.max_path_length, -1)

    def make_indices(self, path_lengths, horizon):
        '''
            makes indices for sampling from dataset;
            each index maps to a datapoint
        '''
        indices = []
        for i, path_length in enumerate(path_lengths):
            max_start = min(path_length - 1, self.max_path_length - horizon)
            if not self.use_padding:
                max_start = min(max_start, path_length - horizon)
            for start in range(max_start):
                end = start + horizon
                indices.append((i, start, end))
        indices = np.array(indices)
        return indices

    def get_conditions(self, observations):
        '''
            condition on current observation for planning
        '''
        return {0: observations[0]}

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, idx, eps=1e-4):
        path_ind, start, end = self.indices[idx]

        if self.use_npy_inputs:
            observations = self.fields['normed_observations'][path_ind, start:end]
            actions = self.fields['normed_actions'][path_ind, start:end]
        else:
            observations = self.fields.normed_observations[path_ind, start:end]
            actions = self.fields.normed_actions[path_ind, start:end]

        conditions = self.get_conditions(observations)
        trajectories = np.concatenate([actions, observations], axis=-1)

        if self.use_language:
            #import pdb;pdb.set_trace()
            language = self.fields['language'][path_ind, 0]
            batch = RewardBatch(trajectories, conditions, language)
        else:
            batch = Batch(trajectories, conditions)
        return batch


class GoalDataset(SequenceDataset):

    def get_conditions(self, observations):
        '''
            condition on both the current observation and the last observation in the plan
        '''
        return {
            0: observations[0],
            self.horizon - 1: observations[-1],
        }


class ValueDataset(SequenceDataset):
    '''
        adds a value field to the datapoints for training the value function
    '''

    def __init__(self, *args, discount=0.99, normed=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.discount = discount
        self.discounts = self.discount ** np.arange(self.max_path_length)[:,None]
        self.normed = False
        if normed:
            self.vmin, self.vmax = self._get_bounds()
            self.normed = True

    def _get_bounds(self):
        print('[ datasets/sequence ] Getting value dataset bounds...', end=' ', flush=True)
        vmin = np.inf
        vmax = -np.inf
        for i in range(len(self.indices)):
            value = self.__getitem__(i).values.item()
            vmin = min(value, vmin)
            vmax = max(value, vmax)
        print('✓')
        return vmin, vmax

    def normalize_value(self, value):
        ## [0, 1]
        normed = (value - self.vmin) / (self.vmax - self.vmin)
        ## [-1, 1]
        normed = normed * 2 - 1
        return normed

    def __getitem__(self, idx):
        batch = super().__getitem__(idx)
        path_ind, start, end = self.indices[idx]
        rewards = self.fields['rewards'][path_ind, start:]
        discounts = self.discounts[:len(rewards)]
        value = (discounts * rewards).sum()
        if self.normed:
            value = self.normalize_value(value)
        value = np.array([value], dtype=np.float32)
        value_batch = ValueBatch(*batch, value)
        return value_batch
