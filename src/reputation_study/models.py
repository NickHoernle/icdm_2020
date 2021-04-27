import torch
import torch.nn as nn
from torch.nn import functional as F
from typing import List
from torch.distributions import Poisson, Dirichlet
import torch.distributions as distrib
import itertools

import numpy as np

from so_study.normalizing_flows import NormalizingFlow


pois_loss = torch.nn.PoissonNLLLoss(reduction="none")

def init_weights(m):
    if type(m) == nn.Linear:
        torch.nn.init.xavier_uniform_(m.weight)
        m.bias.data.fill_(0.01)


def ZeroInflatedPoisson_loss_function(recon_x, x, latent_loss):
    x_shape = x.size()

    recon_x_0_bin = torch.logsigmoid(recon_x[:, 0, :])
    recon_x_0_1_min_bin = torch.logsigmoid(-recon_x[:, 0, :])
    recon_x_0_count = F.softplus(recon_x[:, 1, :])

    poisson_0 = (x == 0).float() * Poisson(recon_x_0_count).log_prob(x)
    # else if x > 0
    poisson_greater0 = (x > 0).float() * Poisson(recon_x_0_count).log_prob(x)

    zero_inf = torch.cat((
        recon_x_0_1_min_bin.view(x_shape[0], x_shape[1], -1),
        poisson_0.view(x_shape[0], x_shape[1], -1)
    ), dim=2)

    log_l = (x == 0).float() * torch.logsumexp(zero_inf, dim=2)
    log_l += (x > 0).float() * (recon_x_0_bin + poisson_greater0)

    return -log_l.sum() + latent_loss

def ZIP_loss(recon_x, x, latent_loss=0, log_cluster_pred=None, test=False):

    count_pred = F.softplus(recon_x[:, :, 1, :])

    log_theta = F.logsigmoid(recon_x[:, :, 0, :])
    log_1_min_theta = F.logsigmoid(-recon_x[:, :, 0, :])

    pois_lp = log_theta + Poisson(count_pred).log_prob(x)
    x0_term = torch.logsumexp(torch.stack((log_1_min_theta, pois_lp), dim=-1), dim=-1)

    # theta + (1-theta)*Pois(0|lambda) if x ==0 else (1-theta)*Pois(x|lambda)
    log_l = torch.where(x == 0, x0_term, pois_lp)
    half_way = log_l.shape[2]//2
    log_l[:, :, (half_way-2, half_way-1, half_way)] = 0

    if type(log_cluster_pred) == type(None):
        log_l = log_l.sum(dim=-1).mean()
        return latent_loss - log_l

    cluster_pred = log_cluster_pred.exp()

    zp, nc = .9, log_cluster_pred.size(1)-1
    log_prior = torch.log(torch.tensor([zp] + [(1-zp)/nc for i in range(nc)]))

    # import pdb
    # pdb.set_trace()
    return -(cluster_pred*(log_l.sum(dim=-1) - log_cluster_pred + log_prior)).sum(dim=-1).mean() + latent_loss
    # return -(cluster_pred*(log_l.sum(dim=-1) - log_cluster_pred)).sum(dim=-1).mean() + latent_loss

def Pois_loss(recon_x, x, latent_loss=0, logit_cluster_pred=None):

    count_pred = F.softplus(recon_x.squeeze(-2))
    log_l = Poisson(count_pred).log_prob(x)

    if type(logit_cluster_pred) == type(None):
        return -log_l.sum() + latent_loss

    cluster_pred = log_cluster_pred.exp()
    log_prior = torch.log(torch.tensor([.5, .5/3, .5/3, .5/3]))

    return -(cluster_pred*(log_l.sum(dim=-1) - log_cluster_pred + log_prior)).sum(dim=-1).mean() + latent_loss


class MaskedLinear(nn.Linear):
    """ same as Linear except has a configurable mask on the weights """

    def __init__(self, in_features, out_features, bias=True):
        super().__init__(in_features, out_features, bias)
        self.register_buffer('mask', torch.ones(out_features, in_features))

    def set_mask(self, mask):
        self.mask.data.copy_(torch.from_numpy(mask.astype(np.uint8).T))

    def forward(self, input):
        return F.linear(input, self.mask * self.weight, self.bias)


class MADE(nn.Module):
    def __init__(self,
                 nin: int,
                 hidden_sizes: List[int],
                 latent_dim: int,
                 nout: int,
                 num_masks: int = 1,
                 natural_ordering: bool = False):
        # thanks: https://github.com/karpathy/pytorch-made/blob/master/made.py
        """
        nin: integer; number of inputs
        hidden sizes: a list of integers; number of units in hidden layers
        nout: integer; number of outputs, which usually collectively parameterize some kind of 1D distribution
              note: if nout is e.g. 2x larger than nin (perhaps the mean and std), then the first nin
              will be all the means and the second nin will be stds. i.e. output dimensions depend on the
              same input dimensions in "chunks" and should be carefully decoded downstream appropriately.
              the output of running the tests for this file makes this a bit more clear with examples.
        num_masks: can be used to train ensemble over orderings/connections
        natural_ordering: force natural ordering of dimensions, don't use random permutations
        """
        super().__init__()

        self.nin = nin
        self.nout = nout
        self.hidden_sizes = hidden_sizes
        self.latent_dim = latent_dim
        assert self.nout % self.nin == 0, "nout must be integer multiple of nin"

        # define a simple MLP neural net
        self.net = []
        hs = [nin + self.latent_dim] + hidden_sizes + [nout]
        for h0, h1 in zip(hs, hs[1:]):
            self.net.extend([
                MaskedLinear(h0, h1),
                nn.ReLU(),
            ])
        self.net.pop()  # pop the last ReLU for the output layer
        self.net = nn.Sequential(*self.net)

        # seeds for orders/connectivities of the model ensemble
        self.natural_ordering = natural_ordering
        self.num_masks = num_masks
        self.seed = 0  # for cycling through num_masks orderings

        self.m = {}
        self.update_masks()  # builds the initial self.m connectivity

    def update_masks(self):
        if self.m and self.num_masks == 1: return  # only a single seed, skip for efficiency
        hs = [self.nin] + self.hidden_sizes + [self.nout]
        L = len(self.hidden_sizes)

        # fetch the next seed and construct a random stream
        rng = np.random.RandomState(self.seed)
        self.seed = (self.seed + 1) % self.num_masks

        # sample the order of the inputs and the connectivity of all neurons
        self.m[-1] = np.arange(self.nin) if self.natural_ordering else rng.permutation(self.nin)
        for l in range(L):
            self.m[l] = rng.randint(self.m[l - 1].min(), self.nin - 1, size=self.hidden_sizes[l])

        # construct the mask matrices
        masks = [self.m[l - 1][:, None] <= self.m[l][None, :] for l in range(L)]
        masks.append(self.m[L - 1][:, None] < self.m[-1][None, :])

        # handle the case where nout = nin * k, for integer k > 1
        if self.nout > self.nin:
            k = int(self.nout / self.nin)
            # replicate the mask across the other outputs
            masks[-1] = np.concatenate([masks[-1]] * k, axis=1)

        masks[0] = np.concatenate([masks[0], np.ones((self.latent_dim, hs[1]))], axis=0)

        # set the masks in all MaskedLinear layers
        layers = [l for l in self.net.modules() if isinstance(l, MaskedLinear)]
        for l, m in zip(layers, masks):
            l.set_mask(m)

    def forward(self, x):
        return self.net(x)


class Baseline(nn.Module):
    def __init__(self, **kwargs):
        super().__init__()

        self.latent_dim = kwargs.get('latent_dim', 20)
        self.date_of_threshold_cross = kwargs.get('date_of_threshold_cross', 20)
        self.threshold_amount = kwargs.get('threshold_amount', 1000)
        self.input_lim = kwargs.get('input_lim', 20)
        self.output_len = kwargs.get('output_len', 51)
        self.in_channels = kwargs.get('in_channels', 1)
        self.block = kwargs.get('block', False)

        self.name = "Model0"

        self.encoder = nn.Sequential(
            nn.Linear(self.input_lim*self.in_channels, 100),
            nn.ReLU(True),
            nn.BatchNorm1d(100),
            nn.Linear(100, 100),
            nn.ReLU(True),
            nn.BatchNorm1d(100),
            nn.Linear(100, self.latent_dim * 2)
        )

        hidden_size = 5

        # self.decoder = MADE(51, [200, 200], self.latent_dim, 51 * 2, natural_ordering=True)
        # self.decoder = nn.Sequential(
        #     nn.Linear(self.latent_dim, 100),
        #     nn.ReLU(True),
        #     nn.BatchNorm1d(100),
        #     nn.Linear(100, 100),
        #     nn.ReLU(True),
        #     nn.BatchNorm1d(100),
        #     nn.Linear(100, 7*2)
        # )

        self.decoder = nn.GRU(input_size=1, hidden_size=self.latent_dim, num_layers=self.in_channels, batch_first=True, dropout=0)
        self.fc_decoder = nn.Sequential(nn.ReLU(True), nn.Linear(self.latent_dim, 2))

        self.apply(init_weights)

    @property
    def t_cross(self):
        return self.date_of_threshold_cross

    def latent_loss(self, x, z_params):
        n_batch = x.size(0)

        # Retrieve mean and var
        mu, log_var = z_params

        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)

        z = mu + eps*std
        kl_div = -0.5 * torch.mean((1 + log_var - mu.pow(2) - log_var.exp()).sum(dim=1))

        # Compute KL divergence
        return z, kl_div

    def encode(self, x):
        bs = x.shape[0]
        encoded = self.encoder(x[:, :, :self.input_lim].view(bs, -1))
        return encoded[:, :self.latent_dim], encoded[:, self.latent_dim:]

    def decode(self, x, z):
        hidden_layer = z.unsqueeze(0).repeat(self.in_channels, 1, 1)
        predictions = []
        for i in range(0, x.size(-1) - 1):
            # print(x[:, :, i].unsqueeze(-1).shape)
            preds, hidden_layer_ = self.decoder(x[:, :, i].unsqueeze(-1), hidden_layer)
            # print(preds.shape, hidden_layer_.shape)
            predictions.append(self.fc_decoder(preds.squeeze(1)))

        if self.in_channels == 1:
            return torch.stack(predictions, dim=-1).unsqueeze(1)
        # pred = self.decoder(z)
        # out = torch.stack(pred.split(7, dim=-1), dim=1).repeat(1, 1, (self.output_len+8)//7)[:, :, :self.output_len]
        # import pdb
        # pdb.set_trace()
        # return out.unsqueeze(1)

    def forward(self, x):
        zparams = self.encode(x)
        z, latent_loss = self.latent_loss(x, zparams)
        predictions = self.decode(x, z)
        return predictions, (latent_loss, z)


class AddBeta(Baseline):
    def __init__(self, **kwargs):
        super(AddBeta, self).__init__(**kwargs)

        self.k_binomial = nn.Parameter(.1*torch.randn((self.in_channels, self.output_len), requires_grad=True).float())
        self.k_activity = nn.Parameter(.1*torch.randn((self.in_channels, self.output_len), requires_grad=True).float())

        # self.decoder = MADE(51, [200, 200], 0, 51 * 2, natural_ordering=True)
        self.name = "Model1"

        self.softplus = nn.Softplus()
        self.apply(init_weights)

    @property
    def k_bin_pos(self):
        offset = torch.ones_like(self.k_binomial)

        offset[:, 0::2] *= .5 # even (positive effect)
        offset[:, 1::2] *= .2 # odd (negative effect)

        # before badge
        offset[:, 0::4] = self.apply_window_fn_b(offset[:, 0::4])
        offset[:, 1::4] = self.apply_window_fn_b(offset[:, 1::4])

        # after badge
        offset[:, 2::4] = self.apply_window_fn_a(offset[:, 2::4])
        offset[:, 3::4] = self.apply_window_fn_a(offset[:, 3::4])

        return self.softplus(self.k_binomial) + offset

    @property
    def k_act_pos(self):
        offset = torch.ones_like(self.k_activity)

        offset[:, 0::2] *= .5 # even (positive effect)
        offset[:, 1::2] *= .2 # odd (negative effect)

        # before badge
        offset[:, 0::4] = self.apply_window_fn_b(offset[:, 0::4])
        offset[:, 1::4] = self.apply_window_fn_b(offset[:, 1::4])

        # after badge
        offset[:, 2::4] = self.apply_window_fn_a(offset[:, 2::4])
        offset[:, 3::4] = self.apply_window_fn_a(offset[:, 3::4])

        return self.softplus(self.k_activity) + offset

    def apply_window_fn_b(self, weights):
        mu = self.date_of_threshold_cross-1
        l = self.date_of_threshold_cross/5
        ones = torch.arange(weights.size(-1)).float()
        wndw = (-(ones-mu).pow(2)/(2*l**2)).exp()
        return weights*wndw

    def apply_window_fn(self, weights):
        # mu = self.date_of_threshold_cross-1
        # l = self.date_of_threshold_cross/4
        # ones = torch.arange(weights.size(-1)).float()
        # wndw = (-(ones-mu).pow(2)/(2*l**2)).exp()
        return weights#*wndw

    def apply_window_fn_a(self, weights):
        mu = 0
        l = self.date_of_threshold_cross/5
        ones = torch.arange(weights.size(-1)).float()
        wndw = (-(ones-mu).pow(2)/(2*l**2)).exp()
        return weights*wndw

    # def apply_window_fn(self, weights):
    #     mu = 0
    #     l = 20
    #     ones = torch.arange(weights.size(-1)).float()
    #     wndw = (-(ones-mu).pow(2)/(2*l**2)).exp()
    #     return weights*wndw

    def get_weights(self, x):

        # effect_weight = 1/(1+torch.abs((self.output_len/2 - torch.arange(len(self.k_binomial)))))
        effect_weight = torch.ones(self.output_len)
        effect_weight[self.date_of_threshold_cross:] *= -1

        bin_weights = (effect_weight*self.k_bin_pos).unsqueeze(0).repeat(len(x), 1, 1)
        act_weights = (effect_weight*self.k_act_pos).unsqueeze(0).repeat(len(x), 1, 1)

        return bin_weights, act_weights

    def forward(self, x):
        zparams = self.encode(x)
        z, latent_loss = self.latent_loss(x, zparams)
        predictions = self.decode(x, z)
        bin_weights, act_weights = self.get_weights(x)
        weights = torch.stack((bin_weights, act_weights), dim=-2)
        predict = predictions + weights
        return predict, (latent_loss, z)

    # def decode(self, x, z):
    #     activity = super().decode(x, z)
    #     bin_weights, act_weights = self.get_weights(x)
    #
    #     activity[:, :, 0, :] += bin_weights
    #     activity[:, :, 0, :] += act_weights
    #
    #     return activity


class AddClassPred(AddBeta):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.weights = kwargs.get("weights", [(0, 0, 0, 0), (1, 0, 1, 0)])
        self.num_clusters = len(self.weights)

        self.k_binomial = nn.Parameter(.1*torch.randn((self.in_channels, self.num_clusters*4, self.output_len//2), requires_grad=True).float())
        self.k_activity = nn.Parameter(.1*torch.randn((self.in_channels, self.num_clusters*4, self.output_len//2), requires_grad=True).float())

        self.name = "Model2"

        self.encoder = nn.Sequential(
            nn.Linear(self.input_lim * self.in_channels, 50),
            nn.ReLU(True),
            nn.BatchNorm1d(50),
            nn.Linear(50, 50),
            nn.ReLU(True),
            nn.BatchNorm1d(50),
            nn.Linear(50, self.latent_dim * 2)
        )

        self.cluster_pred = nn.Sequential(
            nn.Linear((self.output_len+1) * self.in_channels, 50),
            nn.ReLU(True),
            nn.BatchNorm1d(50),
            nn.Linear(50, 50),
            nn.ReLU(True),
            nn.BatchNorm1d(50),
            nn.Linear(50, self.num_clusters)
        )

        self.apply(init_weights)

    def encode(self, x):
        bs = x.shape[0]
        encoded = self.encoder(x[:, :, :self.input_lim].view(bs, -1))
        cluster_pred = self.cluster_pred(x.view(bs, -1))
        return encoded[:, :self.latent_dim], \
               encoded[:, self.latent_dim:], \
               cluster_pred

    def get_weight_options(self):

        binary_pred, count_pred = [], []

        for (bin_b, bin_a, cnt_b, cnt_a) in self.weights:

            bin_weight = torch.zeros(self.output_len)
            act_weight = torch.zeros(self.output_len)

            # before
            bin_weight[:self.output_len // 2] = bin_b * (self.k_bin_pos[0, 0] if bin_b > 0 else self.k_bin_pos[0, 1])
            act_weight[:self.output_len // 2] = cnt_b * (self.k_act_pos[0, 0] if cnt_b > 0 else self.k_act_pos[0, 1])

            # after
            bin_weight[self.output_len // 2:] = bin_a * (self.k_bin_pos[0, 2] if bin_a > 0 else self.k_bin_pos[0, 3])
            act_weight[self.output_len // 2:] = cnt_a * (self.k_act_pos[0, 2] if cnt_a > 0 else self.k_act_pos[0, 3])

            if self.block:
                bin_weight[self.output_len // 2 - 2: self.output_len // 2 + 1] = bin_weight[self.output_len// 2 - 3]
                act_weight[self.output_len // 2 - 2: self.output_len // 2 + 1] = act_weight[self.output_len// 2 - 3]

            binary_pred.append(bin_weight)
            count_pred.append(act_weight)

        return self.apply_window_fn(torch.stack(binary_pred, dim=0)), self.apply_window_fn(torch.stack(count_pred, dim=0))

    def get_weights(self, cp):

        bin_weights, act_weights = self.get_weight_options()
        user_w = torch.ones_like(cp)

        bin_weights = bin_weights.unsqueeze(0)*user_w.unsqueeze(-1)
        act_weights = act_weights.unsqueeze(0)*user_w.unsqueeze(-1)

        return torch.stack((bin_weights, act_weights), dim=2)

    def forward(self, x, **kwargs):
        mu, lv, cluster_pred = self.encode(x)

        z, latent_loss = self.latent_loss(x, (mu, lv))
        predictions = self.decode(x, z, **kwargs)

        weights = self.get_weights(cluster_pred)
        # my_weight = torch.sigmoid(z[:, -1]).unsqueeze(1).unsqueeze(1).unsqueeze(1)

        log_cp = F.log_softmax(cluster_pred, dim=1)
        predictions = predictions.repeat(1, self.num_clusters, 1, 1)
        predict = predictions + weights

        return (predict, log_cp), (latent_loss, z)



class MultipleClassPred(AddClassPred):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.name = "Model3"

    def get_weight_options(self):

        binary_pred, count_pred = [], []

        for (i, bin_b, bin_a, cnt_b, cnt_a) in self.weights:
            bin_weight = torch.zeros(self.output_len)
            act_weight = torch.zeros(self.output_len)

            # before
            bin_weight[:self.output_len // 2] = bin_b * (self.k_bin_pos[0, 4*i] if bin_b > 0 else self.k_bin_pos[0, 4*i+1])
            act_weight[:self.output_len // 2] = cnt_b * (self.k_act_pos[0, 4*i] if cnt_b > 0 else self.k_act_pos[0, 4*i+1])

            # after
            bin_weight[self.output_len // 2:] = bin_a * (self.k_bin_pos[0, 4*i+2] if bin_a > 0 else self.k_bin_pos[0, 4*i+3])
            act_weight[self.output_len // 2:] = cnt_a * (self.k_act_pos[0, 4*i+2] if cnt_a > 0 else self.k_act_pos[0, 4*i+3])

            if self.block:
                bin_weight[self.output_len // 2 - 2: self.output_len // 2 + 1] = bin_weight[self.output_len// 2 - 3]
                act_weight[self.output_len // 2 - 2: self.output_len // 2 + 1] = act_weight[self.output_len// 2 - 3]

            binary_pred.append(bin_weight)
            count_pred.append(act_weight)

        return self.apply_window_fn(torch.stack(binary_pred, dim=0)), self.apply_window_fn(torch.stack(count_pred, dim=0))


# class MultipleClassPred(AddClassPred):
#     def __init__(self, **kwargs):
#         kwargs["weights"] = [(0, 0), (0, 1), (1, 0), (1, -1)]
#         super().__init__(**kwargs)
#
#     def get_weight_options(self):
#         bin_weights, act_weights = [], []
#
#         for i, (b, a) in enumerate(self.weights):
#             bin_weight = torch.zeros(self.output_len)
#             act_weight = torch.zeros(self.output_len)
#
#             bin_weight[:self.output_len // 2] = b*(self.k_bin_pos[0, 2*i])
#             bin_weight[self.output_len // 2:] = a*(self.k_bin_pos[0, 2*i+1])
#
#             act_weight[:self.output_len // 2] = b * (self.k_act_pos[0, 2*i])
#             act_weight[self.output_len // 2:] = a * (self.k_act_pos[0, 2*i+1])
#
#             bin_weights.append(bin_weight)
#             act_weights.append(act_weight)
#
#         return torch.stack(bin_weights, dim=0), torch.stack(act_weights, dim=0)


class AddReputation(MultipleClassPred):
    def forward(self, x, reputation):
        mu, lv, cluster_pred = self.encode(x)
        z, latent_loss = self.latent_loss(x, (mu, lv))
        predictions = self.decode(x, z, reputation)
        return (predictions, cluster_pred), (latent_loss, z)

    def decode(self, x, z, reputation):
        activities = Baseline.decode(self, x, z)

        bin_weights, act_weights = self.get_weights(x)

        weights = torch.stack((bin_weights, act_weights), dim=2)

        activities = activities.repeat(1, self.num_clusters, 1, 1)
        activities += weights


class SingleActivityFeed(MultipleClassPred):

    def __init__(self, **kwargs):
        kwargs["weights"] = [(0, 0), (1, -1)]
        super().__init__(**kwargs)
        self.fc_decoder = nn.Sequential(nn.ReLU(True), nn.Linear(self.latent_dim, 1))

    def get_weight_options(self):
        act_weights = []
        for (b, a) in self.weights:

            act_weight = torch.zeros(self.in_channels, self.output_len)
            act_weight[:, :self.output_len // 2] = b * (self.k_act_pos[:, 1] if b < 0 else self.k_act_pos[:, 0])
            act_weight[:, self.output_len // 2:] = a * (self.k_act_pos[:, 3] if a < 0 else self.k_act_pos[:, 2])
            act_weights.append(act_weight)

        return torch.stack(act_weights, dim=1), self.weights

    def get_weights(self, x):

        act_weights, weight_params = self.get_weight_options()

        num_users = x.size(0)

        act_weights = act_weights.repeat(num_users, 1, 1)

        return act_weights

    def decode(self, x, z):
        activities = Baseline.decode(self, x, z)

        act_weights = self.get_weights(x).unsqueeze(-2)

        activities = activities.repeat(1, self.num_clusters, 1, 1)
        activities += act_weights

        return activities
