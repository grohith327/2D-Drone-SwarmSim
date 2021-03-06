import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import math


def ortho_weights(shape, scale=1.0):
    """ PyTorch port of ortho_init from baselines.a2c.utils """
    shape = tuple(shape)

    if len(shape) == 2:
        flat_shape = shape[1], shape[0]
    elif len(shape) == 4:
        flat_shape = (np.prod(shape[1:]), shape[0])
    else:
        raise NotImplementedError

    a = np.random.normal(0.0, 1.0, flat_shape)
    u, _, v = np.linalg.svd(a, full_matrices=False)
    q = u if u.shape == flat_shape else v
    q = q.transpose().copy().reshape(shape)

    if len(shape) == 2:
        return torch.from_numpy((scale * q).astype(np.float32))
    if len(shape) == 4:
        return torch.from_numpy(
            (scale * q[:, : shape[1], : shape[2]]).astype(np.float32)
        )


class Policy(nn.Module):
    def __init__(self, obs_size, n_drones, action_size, policy_type="MLP"):
        super(Policy, self).__init__()
        if policy_type == "MLP":
            self.actor = MlpPolicy(obs_size, n_drones, action_size)
            self.critic = MlpPolicy(obs_size, n_drones, 1)
        elif policy_type == "Attn":
            self.actor = ConvAttn(int(math.sqrt(obs_size)), n_drones, action_size)
            self.critic = ConvAttn(int(math.sqrt(obs_size)), n_drones, 1)
        else:
            self.actor = CNNPolicy(n_drones, action_size)
            self.critic = CNNPolicy(n_drones, 1)

    def forward(self, state, drone_pos):
        pi = self.actor(state, drone_pos)
        v = self.critic(state, drone_pos)
        return pi, v


class attn_module(nn.Module):
    def __init__(self, in_features, out_features, n_drones):
        super().__init__()
        self.drone_input = nn.Linear(n_drones * 2, out_features)
        self.q = nn.Linear(in_features, out_features)
        self.k = nn.Linear(in_features, out_features)
        self.v = nn.Linear(in_features, out_features)

    def forward(self, x, drone_pos):
        query = self.q(x)
        key1 = self.k(x)
        key2 = self.drone_input(drone_pos)
        key = key1 + key2
        value = self.v(x)

        scores = torch.matmul(query, key.T)
        attn_mask = F.softmax(scores, dim=-1)
        out = attn_mask * value
        return out


class MlpPolicy(nn.Module):
    def __init__(self, obs_size, n_drones, action_size):
        super(MlpPolicy, self).__init__()
        self.gru = nn.GRU(obs_size, obs_size)

        self.attn1 = attn_module(obs_size, obs_size, n_drones)
        self.state_input = nn.Linear(obs_size, 256)
        self.dense1 = nn.Linear(256, 128)
        self.attn2 = attn_module(128, 128, n_drones)
        self.dense2 = nn.Linear(128, 64)
        self.dense3 = nn.Linear(64, 32)
        self.attn3 = attn_module(32, 32, n_drones)
        self.dense4 = nn.Linear(32, action_size)

        self._init_weights()

    def _init_weights(self):
        layers = [
            self.state_input,
            self.dense1,
            self.dense2,
            self.dense3,
            self.dense4,
        ]
        for layer in layers:
            layer.weight.data = ortho_weights(layer.weight.size())

    def forward(self, state, drone_pos):
        mu = state.mean()
        std = state.std()
        state = (state - mu) / std
        state, _ = self.gru(state.view(len(state), 1, -1))
        state = state.squeeze(1)
        state = self.attn1(state, drone_pos)
        out = F.relu(self.state_input(state))
        out = F.normalize(out)
        out = F.relu(self.dense1(out))
        out = self.attn2(out, drone_pos)
        out = F.relu(self.dense2(out))
        out = F.relu(self.dense3(out))
        out = self.attn3(out, drone_pos)
        out = self.dense4(out)
        return out


class CNNPolicy(nn.Module):

    """
    Policy for 5x5 grid
    """

    def __init__(self, n_drones, action_size):
        super(CNNPolicy, self).__init__()
        self.conv1 = nn.Conv2d(1, 16, 2)
        self.drone_input = nn.Linear(n_drones * 2, 128)
        self.conv2 = nn.Conv2d(16, 32, 2)
        self.conv3 = nn.Conv2d(32, 64, 2)
        self.conv4 = nn.Conv2d(64, 128, 2)
        self.dense1 = nn.Linear(256, 128)
        self.dense2 = nn.Linear(128, 64)
        self.dense3 = nn.Linear(64, 32)
        self.dense4 = nn.Linear(32, action_size)

        self.batch_norm = nn.BatchNorm2d(32)

        self._init_weights()

    def _init_weights(self):
        layers = [
            self.drone_input,
            self.dense1,
            self.dense2,
            self.dense3,
            self.dense4,
        ]
        for layer in layers:
            layer.weight.data = ortho_weights(layer.weight.size())

    def forward(self, state, drone):
        ## Normalize
        mu = state.mean()
        std = state.std()
        state = (state - mu) / std
        state = state.unsqueeze(1)
        ## Conv operations
        state = F.relu(self.conv1(state))
        state = F.relu(self.conv2(state))
        state = self.batch_norm(state)
        state = F.relu(self.conv3(state))
        state = F.relu(self.conv4(state))
        ## Concat
        state = state.view(-1, 128)
        drone = F.relu(self.drone_input(drone))
        out = torch.cat([state, drone], dim=-1)
        out = F.normalize(out)
        ## Predict policies and values
        out = F.relu(self.dense1(out))
        out = F.relu(self.dense2(out))
        out = F.relu(self.dense3(out))
        out = self.dense4(out)
        return out


class ConvAttn(nn.Module):
    def __init__(self, obs_size, n_drones, action_space):
        super().__init__()
        self.weights1 = nn.Parameter(torch.randn(obs_size, obs_size))
        self.conv1 = nn.Conv2d(1, 4, obs_size, padding=2)
        self.weights2 = nn.Parameter(torch.randn(obs_size, obs_size))
        self.conv2 = nn.Conv2d(4, 8, obs_size, padding=1)
        self.weights3 = nn.Parameter(torch.randn(obs_size // 2 + 1, obs_size // 2 + 1))
        self.conv3 = nn.Conv2d(8, 16, obs_size // 2 + 1, padding=0)
        self.drone_input = nn.Linear(n_drones * 2, 16)
        self.dense = nn.Linear(32, action_space)

    def forward(self, state, drone):
        state = state.unsqueeze(1)
        self.weights1.data = (
            self.weights1.view(-1).softmax(-1).view(*self.weights1.size())
        )
        state = torch.matmul(state, self.weights1)
        state = F.gelu(self.conv1(state))
        self.weights2.data = (
            self.weights2.view(-1).softmax(-1).view(*self.weights2.size())
        )
        state = torch.matmul(state, self.weights2)
        state = F.gelu(self.conv2(state))
        self.weights3.data = (
            self.weights3.view(-1).softmax(-1).view(*self.weights3.size())
        )
        state = torch.matmul(state, self.weights3)
        state = F.gelu(self.conv3(state))
        state = state.squeeze(-1).squeeze(-1)
        drone = self.drone_input(drone)
        out = torch.cat((state, drone), dim=-1)
        out = self.dense(out)
        return out
