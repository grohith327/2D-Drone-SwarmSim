import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


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


def atari_initializer(module):
    """ Parameter initializer for Atari models

    Initializes Linear, Conv2d, and LSTM weights.
    """
    classname = module.__class__.__name__

    if classname == "Linear":
        module.weight.data = ortho_weights(
            module.weight.data.size(), scale=np.sqrt(2.0)
        )
        module.bias.data.zero_()

    elif classname == "Conv2d":
        module.weight.data = ortho_weights(
            module.weight.data.size(), scale=np.sqrt(2.0)
        )
        module.bias.data.zero_()

    elif classname == "LSTM":
        for name, param in module.named_parameters():
            if "weight_ih" in name:
                param.data = ortho_weights(param.data.size(), scale=1.0)
            if "weight_hh" in name:
                param.data = ortho_weights(param.data.size(), scale=1.0)
            if "bias" in name:
                param.data.zero_()


class AtariCNN(nn.Module):
    def __init__(self, num_actions):
        """ Basic convolutional actor-critic network for Atari 2600 games

        Equivalent to the network in the original DQN paper.

        Args:
            num_actions (int): the number of available discrete actions
        """
        super().__init__()

        self.conv = nn.Sequential(
            nn.Conv2d(4, 32, 8, stride=4),
            nn.ReLU(),
            nn.Conv2d(32, 64, 4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 64, 3, stride=1),
            nn.ReLU(),
        )

        self.fc = nn.Sequential(nn.Linear(64 * 7 * 7, 512), nn.ReLU())

        self.pi = nn.Linear(512, num_actions)
        self.v = nn.Linear(512, 1)

        self.num_actions = num_actions

        # parameter initialization
        self.apply(atari_initializer)
        self.pi.weight.data = ortho_weights(self.pi.weight.size(), scale=0.01)
        self.v.weight.data = ortho_weights(self.v.weight.size())

    def forward(self, conv_in):
        """ Module forward pass

        Args:
            conv_in (Variable): convolutional input, shaped [N x 4 x 84 x 84]

        Returns:
            pi (Variable): action probability logits, shaped [N x self.num_actions]
            v (Variable): value predictions, shaped [N x 1]
        """
        N = conv_in.size()[0]

        conv_out = self.conv(conv_in).view(N, 64 * 7 * 7)

        fc_out = self.fc(conv_out)

        pi_out = self.pi(fc_out)
        v_out = self.v(fc_out)

        return pi_out, v_out


class MlpPolicy(nn.Module):
    def __init__(self, state_size, n_drones, action_size):
        super(MlpPolicy, self).__init__()
        self.state_input = nn.Linear(state_size, 128)
        self.drone_input = nn.Linear(n_drones * 2, 128)
        self.dense1 = nn.Linear(256, 128)
        self.dense2 = nn.Linear(128, 64)
        self.dense3 = nn.Linear(64, 32)
        self.pi = nn.Linear(32, action_size)
        self.v = nn.Linear(32, 1)

        self._init_weights()

    def _init_weights(self):
        layers = [
            self.state_input,
            self.drone_input,
            self.dense1,
            self.dense2,
            self.dense3,
            self.pi,
            self.v,
        ]
        for layer in layers:
            layer.weight.data = ortho_weights(layer.weight.size())

    def forward(self, state, drone_pos):
        mu = state.mean()
        std = state.std()
        state = (state - mu) / std
        state_embed = F.tanh(self.state_input(state))
        drone_embed = F.tanh(self.drone_input(drone_pos))
        out = torch.cat([state_embed, drone_embed], dim=-1)
        out = F.normalize(out)
        out = F.tanh(self.dense1(out))
        out = F.tanh(self.dense2(out))
        out = F.tanh(self.dense3(out))
        pi_out = self.pi(out)
        v_out = self.v(out)
        return pi_out, v_out


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
        self.pi = nn.Linear(32, action_size)
        self.v = nn.Linear(32, 1)

        self.batch_norm = nn.BatchNorm2d(32)

        self._init_weights()

    def _init_weights(self):
        layers = [
            self.drone_input,
            self.dense1,
            self.dense2,
            self.dense3,
            self.pi,
            self.v,
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
        state = F.tanh(self.conv1(state))
        state = F.tanh(self.conv2(state))
        state = self.batch_norm(state)
        state = F.tanh(self.conv3(state))
        state = F.tanh(self.conv4(state))
        ## Concat
        state = state.view(-1, 128)
        drone = F.tanh(self.drone_input(drone))
        out = torch.cat([state, drone], dim=-1)
        out = F.normalize(out)
        ## Predict policies and values
        out = F.tanh(self.dense1(out))
        out = F.tanh(self.dense2(out))
        out = F.tanh(self.dense3(out))
        pi_out = self.pi(out)
        v_out = self.v(out)
        return pi_out, v_out
