"""
Pytorch Lightning Module for training prototype segmentation model on Cityscapes and SUN datasets
"""
import os
from collections import Counter, defaultdict
import time
from typing import Dict, Optional

import gin
import torch
import torch.nn.functional as F
from pytorch_lightning import LightningModule
import numpy as np

from deeplab_pytorch.libs.utils import PolynomialLR
from torch.optim.lr_scheduler import ReduceLROnPlateau
from segmentation.utils import get_params
from helpers import list_of_distances
from model import PPNet
from segmentation.dataset import resize_label
from settings import log
from train_and_test import warm_only, joint, last_only


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def reset_metrics() -> Dict:
    return {
        'n_correct': 0,
        'n_batches': 0,
        'n_patches': 0,
        'cross_entropy': 0,
        'kld_loss': 0,
        # 'cls_act_loss': 0,
        # 'cluster_cost': 0,
        # 'separation': 0,
        # 'proto_dist_cost': 0,
        'loss': 0,
    }


# noinspection PyAbstractClass
@gin.configurable(denylist=['model_dir', 'ppnet', 'training_phase', 'max_steps', 'prototype_rebalancing'])
class PatchClassificationModule(LightningModule):
    def __init__(
            self,
            model_dir: str,
            ppnet: PPNet,
            training_phase: int,
            max_steps: Optional[int] = None,
            prototype_rebalancing: Optional[int] = None,
            poly_lr_power: float = gin.REQUIRED,
            loss_weight_crs_ent: float = gin.REQUIRED,
            loss_weight_clst: float = gin.REQUIRED,
            loss_weight_sep: float = gin.REQUIRED,
            loss_weight_proto_dist: float = 0.0,
            loss_weight_l1: float = gin.REQUIRED,
            loss_weight_kld: float = 0.0,
            loss_weight_cls_act: float = 0.0,
            joint_optimizer_lr_features: float = gin.REQUIRED,
            joint_optimizer_lr_add_on_layers: float = gin.REQUIRED,
            joint_optimizer_lr_prototype_vectors: float = gin.REQUIRED,
            joint_optimizer_weight_decay: float = gin.REQUIRED,
            warm_optimizer_lr_add_on_layers: float = gin.REQUIRED,
            warm_optimizer_lr_prototype_vectors: float = gin.REQUIRED,
            warm_optimizer_weight_decay: float = gin.REQUIRED,
            last_layer_optimizer_lr: float = gin.REQUIRED,
            prototype_rebalancing_threshold: float = gin.REQUIRED,
            prototype_initialization_method: str = gin.REQUIRED,
            prototype_rebalance_every: int = gin.REQUIRED,
            ignore_void_class: bool = False,
            randomize_all_below_threshold: bool = False,
            iter_size: int = 1,
            reduce_lr_on_plateau: bool = False,
    ):
        super().__init__()
        self.model_dir = model_dir
        self.prototypes_dir = os.path.join(model_dir, 'prototypes')
        self.checkpoints_dir = os.path.join(model_dir, 'checkpoints')
        self.ppnet = ppnet
        self.training_phase = training_phase
        self.max_steps = max_steps
        self.poly_lr_power = poly_lr_power
        self.loss_weight_crs_ent = loss_weight_crs_ent
        self.loss_weight_clst = loss_weight_clst
        self.loss_weight_sep = loss_weight_sep
        self.loss_weight_proto_dist = loss_weight_proto_dist
        self.loss_weight_l1 = loss_weight_l1
        self.joint_optimizer_lr_features = joint_optimizer_lr_features
        self.joint_optimizer_lr_add_on_layers = joint_optimizer_lr_add_on_layers
        self.joint_optimizer_lr_prototype_vectors = joint_optimizer_lr_prototype_vectors
        self.joint_optimizer_weight_decay = joint_optimizer_weight_decay
        self.warm_optimizer_lr_add_on_layers = warm_optimizer_lr_add_on_layers
        self.warm_optimizer_lr_prototype_vectors = warm_optimizer_lr_prototype_vectors
        self.warm_optimizer_weight_decay = warm_optimizer_weight_decay
        self.last_layer_optimizer_lr = last_layer_optimizer_lr
        self.prototype_rebalancing = prototype_rebalancing
        self.prototype_rebalance_every = prototype_rebalance_every
        self.prototype_rebalancing_threshold = prototype_rebalancing_threshold
        self.prototype_initialization_method = prototype_initialization_method
        self.ignore_void_class = ignore_void_class
        self.randomize_all_below_threshold = randomize_all_below_threshold
        self.iter_size = iter_size
        self.loss_weight_kld = loss_weight_kld
        self.loss_weight_cls_act = loss_weight_cls_act
        self.reduce_lr_on_plateau = reduce_lr_on_plateau

        os.makedirs(self.prototypes_dir, exist_ok=True)
        os.makedirs(self.checkpoints_dir, exist_ok=True)

        # initialize variables for computing metrics
        self.metrics = {}
        for split_key in ['train', 'val', 'test', 'train_last_layer']:
            self.metrics[split_key] = reset_metrics()

        # initialize configure_optimizers()
        self.optimizer_defaults = None
        self.start_step = None

        # we use optimizers manually
        self.automatic_optimization = False
        self.best_acc = 0.0

        if self.training_phase == 0:
            warm_only(model=self.ppnet, log=log)
            log(f'WARM-UP TRAINING START. ({self.max_steps} steps)')
        elif self.training_phase == 1:
            joint(model=self.ppnet, log=log)
            log(f'JOINT TRAINING START. (max {self.max_steps} steps)')
        else:
            last_only(model=self.ppnet, log=log)
            log('LAST LAYER TRAINING START.')

        # helper collections for prototype re-balancing
        self.cls_prototypes = []
        self.proto2cls = {}
        for cls_num in range(self.ppnet.prototype_class_identity.shape[1]):
            cls_identity = self.ppnet.prototype_class_identity[:, cls_num]
            cls_prototypes = (cls_identity == 1).nonzero().flatten().cpu().detach().numpy()
            self.cls_prototypes.append(cls_prototypes)
            for proto_num in cls_prototypes:
                self.proto2cls[proto_num] = cls_num

        self.ppnet.prototype_class_identity = self.ppnet.prototype_class_identity.cuda()
        self.rebalance_epoch_counter = 0
        self.rebalancing_stats = {
            'proto_class_patches_total': Counter(),
            'patches_nearest_prototypes': Counter()
        }
        self.lr_scheduler = None
        self.iter_steps = 0
        self.batch_metrics = defaultdict(list)
        self.sanity_check_val = True

    def forward(self, x):
        return self.ppnet(x)

    def _step(self, split_key: str, batch):
        batch_start = time.time()
        optimizer = self.optimizers()
        if split_key == 'train' and self.iter_steps == 0:
            optimizer.zero_grad()

        if self.start_step is None:
            self.start_step = self.trainer.global_step

        if hasattr(self.ppnet.features, 'base'):
            self.ppnet.features.base.freeze_bn()
        prototype_class_identity = self.ppnet.prototype_class_identity.to(self.device)

        metrics = self.metrics[split_key]

        image, mcs_target = batch

        image = image.to(self.device).to(torch.float32)
        mcs_target = mcs_target.cpu().detach().numpy().astype(np.float32)

        mcs_model_outputs = self.ppnet.forward(image, return_activations=False)
        if not isinstance(mcs_model_outputs, list):
            mcs_model_outputs = [mcs_model_outputs]

        mcs_loss, mcs_cross_entropy, mcs_kld_loss, mcs_cls_act_loss = 0.0, 0.0, 0.0, 0.0
        for output, patch_activations in mcs_model_outputs:

            target = []
            for sample_target in mcs_target:
                target.append(resize_label(sample_target, size=(output.shape[2], output.shape[1])).to(self.device))
            target = torch.stack(target, dim=0)

            # we flatten target/output - classification is done per patch
            output = output.reshape(-1, output.shape[-1])
            target_img = target.reshape(target.shape[0], -1) # (batch_size, img_size)
            target = target.flatten()

            if patch_activations is not None:
                patch_activations = patch_activations.permute(0, 2, 3, 1)
                patch_activations_img = patch_activations.reshape(patch_activations.shape[0], -1, patch_activations.shape[-1]) # (batch_size, img_size, num_proto)
                patch_activations = patch_activations.reshape(-1, patch_activations.shape[-1])

            if self.ignore_void_class:
                # do not predict label for void class (0)
                target_not_void = (target != 0).nonzero().squeeze()
                target = target[target_not_void] - 1
                output = output[target_not_void]
                if patch_activations is not None:
                    patch_activations = patch_activations[target_not_void]

            # TODO: This is temporary for gumbel softmax
            # if split_key == 'train':
                # # add noise to outputs using gumbel softmax
                # output = F.gumbel_softmax(
                    # output,
                    # tau=1.0,
                    # hard=False
                # )
                # # NLL Loss must be used if output already is after softmax
                # cross_entropy = torch.nn.functional.nll_loss(
                    # torch.log(output),
                    # target.long()
                # )
            # else:
                # cross_entropy = torch.nn.functional.cross_entropy(
                    # output,
                    # target.long()
                # )
            cross_entropy = torch.nn.functional.cross_entropy(
                output,
                target.long()
            )

            # calculate KLD over class pixels between prototypes from same class
            kld_loss = []
            cls_act_loss = []
            eps = 1e-9
            if self.loss_weight_kld > 0:
                for img_i in range(len(target_img)):
                    for cls_i in torch.unique(target_img[img_i]).cpu().detach().numpy():
                        if cls_i < 0 or cls_i >= self.ppnet.prototype_class_identity.shape[1]:
                            continue
                        cls_protos = torch.nonzero(self.ppnet.prototype_class_identity[:, cls_i]). \
                            flatten().cpu().detach().numpy()
                        if len(cls_protos) < 2:
                            continue

                        cls_mask = (target_img[img_i] == cls_i)

                        log_cls_activations = [torch.masked_select(patch_activations_img[img_i, :, i], cls_mask) for i in cls_protos]
                        # non_cls_activations = [torch.masked_select(patch_activations_img[img_i, :, i], ~cls_mask) for i in cls_protos]

                        log_cls_activations = [torch.nn.functional.log_softmax(act, dim=0) for act in log_cls_activations]

                        # randomize 10 * 9 pairs of prototypes
                        cls_protos_i = np.arange(len(cls_protos))
                        if len(cls_protos_i) > 10:
                            cls_protos_i = np.random.choice(cls_protos_i, size=10, replace=False)

                        for i in cls_protos_i:
                            # cls_class_act_loss = (torch.mean(cls_activations[i]) /
                                                  # (torch.mean(non_cls_activations[i]) + eps)) ** 2
                            # cls_act_loss.append(cls_class_act_loss)

                            if len(cls_protos) < 2 or len(log_cls_activations[0]) < 2:
                                # no distribution over given class
                                continue

                            log_p1_scores = log_cls_activations[i]

                            other_protos_i = np.arange(i+1, len(cls_protos))
                            if len(other_protos_i) > 9:
                                other_protos_i = np.random.choice(other_protos_i, size=9, replace=False)

                            for j in other_protos_i:
                                log_p2_scores = log_cls_activations[j]

                                # add kld1 and kld2 to make 'symmetrical kld'
                                kld1 = torch.nn.functional.kl_div(log_p1_scores, log_p2_scores,
                                                                  log_target=True, reduction='sum')
                                kld2 = torch.nn.functional.kl_div(log_p2_scores, log_p1_scores,
                                                                  log_target=True, reduction='sum')
                                kld = (kld1 + kld2) / 2.0
                                kld_loss.append(kld)

            if len(kld_loss) > 0:
                kld_loss = torch.stack(kld_loss)
                # to make 'loss' (lower == better) take exponent of the negative (maximum value is 1.0, for KLD == 0.0)
                kld_loss = torch.exp(-kld_loss)
                kld_loss = torch.mean(kld_loss)
            else:
                kld_loss = 0.0

            # cls_act_loss = torch.mean(torch.stack(cls_act_loss))

            # TODO: temporarily commented out to save time and RAM
            # max_dist = (self.ppnet.prototype_shape[1]
            # * self.ppnet.prototype_shape[2]
            # * self.ppnet.prototype_shape[3])

            # calculate cluster cost
            # prototypes_of_correct_class = torch.t(torch.index_select(
            # prototype_class_identity,
            # dim=-1,
            # index=target.long()
            # )).to(self.device)

            # inverted_distances, _ = torch.max((max_dist - patch_distances) * prototypes_of_correct_class, dim=1)
            # cluster_cost = torch.mean(max_dist - inverted_distances)

            # calculate separation cost
            # prototypes_of_wrong_class = 1 - prototypes_of_correct_class
            # inverted_distances_to_nontarget_prototypes, _ = \
            # torch.max((max_dist - patch_distances) * prototypes_of_wrong_class, dim=1)

            # separation = torch.mean(max_dist - inverted_distances_to_nontarget_prototypes)

            output_class = torch.argmax(output, dim=-1)
            is_correct = output_class == target

            if self.loss_weight_l1 > 0:
                if hasattr(self.ppnet, 'nearest_proto_only') and self.ppnet.nearest_proto_only:
                    l1_mask = 1 - torch.eye(self.ppnet.num_classes, device=self.device)
                else:
                    l1_mask = 1 - torch.t(prototype_class_identity)

                l1 = (self.ppnet.last_layer.weight * l1_mask).norm(p=1)
            else:
                l1 = 0.0

            # calculate 'prototype distance cost' - we want to punish near prototypes within same class
            # prototype_class_identity.shape = (num_prototypes, self.num_classes)
            # prototype_vectors.shape = (num_prototypes, hidden)

            # prototype_vectors = torch.reshape(self.ppnet.prototype_vectors, (self.ppnet.num_prototypes, -1))

            # proto_dist_cost = []

            # for cls_i in range(self.ppnet.num_classes):
            # cls_prototypes = prototype_vectors[self.cls_prototypes[cls_i]]
            # if len(cls_prototypes) <= 1:
            # continue
            # pair_distances = torch.cdist(cls_prototypes, cls_prototypes)

            # mask duplicate pairs and distances of a prototype to itself
            # mask_value = 10e6
            # pair_distances = pair_distances + torch.triu(torch.full_like(pair_distances, fill_value=mask_value))
            # pair_distances = pair_distances.flatten()
            # pair_distances = pair_distances[pair_distances < mask_value]

            # pairwise_loss = torch.max(torch.exp(-pair_distances))
            # proto_dist_cost.append(pairwise_loss)

            # proto_dist_cost = torch.mean(torch.stack(proto_dist_cost))
            
            if self.loss_weight_kld > 0:
                loss = (self.loss_weight_crs_ent * cross_entropy +
                        # self.loss_weight_clst * cluster_cost +
                        # self.loss_weight_sep * separation +
                        # self.loss_weight_proto_dist * proto_dist_cost +
                        self.loss_weight_kld * kld_loss +
                        # self.loss_weight_cls_act * cls_act_loss +
                        self.loss_weight_l1 * l1)
            else:
                loss = (self.loss_weight_crs_ent * cross_entropy +
                        # self.loss_weight_clst * cluster_cost +
                        # self.loss_weight_sep * separation +
                        # self.loss_weight_proto_dist * proto_dist_cost +
                        # self.loss_weight_cls_act * cls_act_loss +
                        self.loss_weight_l1 * l1)

            mcs_loss += loss / len(mcs_model_outputs)
            mcs_cross_entropy += cross_entropy / len(mcs_model_outputs)
            mcs_kld_loss += kld_loss / len(mcs_model_outputs)
            # mcs_cls_act_loss += cls_act_loss / len(mcs_model_outputs)
            metrics['n_correct'] += torch.sum(is_correct)
            metrics['n_patches'] += output.shape[0]

        self.batch_metrics['loss'].append(mcs_loss.item())
        self.batch_metrics['cross_entropy'].append(mcs_cross_entropy.item())
        self.batch_metrics['kld_loss'].append(mcs_kld_loss if isinstance(mcs_kld_loss, float) else mcs_kld_loss.item())
        # self.batch_metrics['cls_act_loss'].append(mcs_cls_act_loss.item())
        self.iter_steps += 1

        if split_key == 'train':
            self.manual_backward(mcs_loss / self.iter_size)

            if self.iter_steps == self.iter_size:
                self.iter_steps = 0
                optimizer.step()

            lr = get_lr(optimizer)
            self.log('lr', lr, on_step=True)

            if self.lr_scheduler is not None and not self.reduce_lr_on_plateau:
                self.lr_scheduler.step()

            if self.prototype_rebalancing is not None:
                with torch.no_grad():
                    output_class_oh = F.one_hot(output_class, num_classes=self.ppnet.num_classes)
                    output_class_mask = torch.matmul(output_class_oh.float(), self.ppnet.prototype_class_identity.T)
                    # [n_pixels x n_protos]
                    pred_cls_patch_distances = patch_activations + (1 - output_class_mask) * 10e6
                    nearest_patch_prototypes = torch.argmin(pred_cls_patch_distances, dim=1).flatten()

                    for cls_num in range(self.ppnet.prototype_class_identity.shape[1]):
                        is_pred_cls = output_class == cls_num
                        total_cls_pixels = torch.sum(is_pred_cls).item()

                        if total_cls_pixels > 0:
                            for proto_num in self.cls_prototypes[cls_num]:
                                self.rebalancing_stats['patches_nearest_prototypes'][proto_num] += torch.sum(
                                    (nearest_patch_prototypes == proto_num) & is_pred_cls
                                ).item()
                                self.rebalancing_stats['proto_class_patches_total'][proto_num] += total_cls_pixels
        elif self.iter_steps == self.iter_size:
            self.iter_steps = 0

        if self.iter_steps == 0:
            for key, values in self.batch_metrics.items():
                mean_value = float(np.mean(self.batch_metrics[key]))
                metrics[key] += mean_value
                if key == 'loss':
                    self.log('train_loss_step', mean_value, on_step=True, prog_bar=True)
                # print(key, mean_value)
            # print()
            metrics['n_batches'] += 1

            self.batch_metrics = defaultdict(list)
        # print(time.time() - batch_start)

    def rebalance_prototypes(self):
        total_cls_patches = self.rebalancing_stats['proto_class_patches_total']
        prototypes_n_nearest = self.rebalancing_stats['patches_nearest_prototypes']

        cls_proto_saturation = np.full(len(self.cls_prototypes), dtype=float, fill_value=2.0)
        proto_nums, frac_top_proto = [], []
        for i in range(self.ppnet.num_prototypes):
            if total_cls_patches[i] > 0:
                proto_nums.append(i)
                proto_frac = prototypes_n_nearest[i] / total_cls_patches[i]
                frac_top_proto.append(proto_frac)

                cls_num = self.proto2cls[i]
                if proto_frac < cls_proto_saturation[cls_num]:
                    cls_proto_saturation[cls_num] = proto_frac

        proto_nums = np.asarray(proto_nums)
        frac_top_proto = np.asarray(frac_top_proto)

        # save statistics about prototype balance for debugging purposes
        # this can be removed/commented out if it takes too much disk space
        os.makedirs(f'{self.checkpoints_dir}/prototype_rebalancing', exist_ok=True)
        np_total_cls_patches = np.asarray([self.rebalancing_stats['proto_class_patches_total'][i]
                                           for i in range(self.ppnet.num_prototypes)], dtype=np.uint64)
        np_prototypes_n_nearest = np.asarray([self.rebalancing_stats['patches_nearest_prototypes'][i]
                                              for i in range(self.ppnet.num_prototypes)], dtype=np.uint64)
        np.savez(f'{self.checkpoints_dir}/prototype_rebalancing/{self.training_phase}_{self.trainer.global_step}',
                 total_patches=np_total_cls_patches, nearest_patches=np_prototypes_n_nearest,
                 class_saturation=cls_proto_saturation)

        cls_proto_saturation = np.asarray(cls_proto_saturation)
        top_classes_by_proto_saturation = np.argsort(-cls_proto_saturation)

        randomized_prototypes = []

        # up to "NUM_CLASSES" prototypes are moved to different classes
        cls_i = 0
        any_moved = False
        for proto_ind in np.argsort(frac_top_proto):
            proto_num = proto_nums[proto_ind]

            if frac_top_proto[proto_ind] >= self.prototype_rebalancing_threshold:
                break

            while cls_i < self.ppnet.num_classes and cls_proto_saturation[top_classes_by_proto_saturation[cls_i]] > 1.1:
                cls_i += 1

            if cls_i >= self.ppnet.num_classes:
                saturated_class = None
            else:
                saturated_class = top_classes_by_proto_saturation[cls_i]
                if (saturated_class == self.proto2cls[proto_num] or
                        cls_proto_saturation[saturated_class] < self.prototype_rebalancing_threshold):
                    saturated_class = None

            if saturated_class is None:
                if self.randomize_all_below_threshold:
                    torch.nn.init.uniform_(self.ppnet.prototype_vectors[proto_num])
                    randomized_prototypes.append(proto_ind)
                    continue
                else:
                    break

            log(f'Moving prototype {proto_num} ({(frac_top_proto[proto_ind] * 100):.4f}%) '
                f'from class {self.proto2cls[proto_num]} to class {saturated_class} '
                f'(saturation: {cls_proto_saturation[saturated_class]:.4f}%)')
            any_moved = True

            if self.prototype_initialization_method == 'random':
                torch.nn.init.uniform_(self.ppnet.prototype_vectors[proto_num])
            elif self.prototype_initialization_method == 'mean':
                cls_proto_mean = torch.zeros((self.ppnet.prototype_vectors.shape[1], 1, 1),
                                             dtype=torch.float, device=self.ppnet.prototype_vectors.device)
                for cls_proto_num in self.cls_prototypes[saturated_class]:
                    cls_proto_mean = cls_proto_mean + self.ppnet.prototype_vectors[cls_proto_num]
                cls_proto_mean = cls_proto_mean / len(self.cls_prototypes[saturated_class])
                self.ppnet.prototype_vectors.data[proto_num] = cls_proto_mean
            else:
                raise NotImplementedError(f'Not implemented: {self.prototype_initialization_method}')

            self.ppnet.prototype_class_identity[proto_num] = 0.0
            self.ppnet.prototype_class_identity[proto_num, saturated_class] = 1.0

            cls_i += 1

        if len(randomized_prototypes) > 0:
            log(f'Randomized {len(randomized_prototypes)} prototypes below threshold: {randomized_prototypes}')

        if any_moved or self.rebalance_epoch_counter == 0:
            # log new class identity
            np_identity = self.ppnet.prototype_class_identity.cpu().detach().numpy()
            os.makedirs(f'{self.checkpoints_dir}/prototype_identity', exist_ok=True)
            np.save(f'{self.checkpoints_dir}/prototype_identity/{self.training_phase}_{self.trainer.global_step}',
                    np_identity)

            # re-initialize helper collections for prototype re-balancing
            self.cls_prototypes = []
            self.proto2cls = {}
            for cls_num in range(self.ppnet.prototype_class_identity.shape[1]):
                cls_identity = self.ppnet.prototype_class_identity[:, cls_num]
                cls_prototypes = (cls_identity == 1).nonzero().flatten().cpu().detach().numpy()
                self.cls_prototypes.append(cls_prototypes)
                for proto_num in cls_prototypes:
                    self.proto2cls[proto_num] = cls_num

            # re-initialize last layer
            self.ppnet.set_last_layer_incorrect_connection(incorrect_strength=-0.5)

    def training_step(self, batch, batch_idx):
        return self._step('train', batch)

    def validation_step(self, batch, batch_idx):
        return self._step('val', batch)

    def test_step(self, batch, batch_idx):
        return self._step('test', batch)

    def on_train_epoch_start(self):
        # reset metrics
        for split_key in self.metrics.keys():
            self.metrics[split_key] = reset_metrics()

        # Freeze the pre-trained batch norm
        if hasattr(self.ppnet.features, 'base'):
            self.ppnet.features.base.freeze_bn()

    def on_validation_epoch_end(self):
        val_acc = (self.metrics['val']['n_correct'] / self.metrics['val']['n_patches']).item()
        if self.metrics['train']['n_patches'] > 0:
            train_acc = (self.metrics['train']['n_correct'] / self.metrics['train']['n_patches']).item()
        else:
            train_acc = ''
        print(train_acc, val_acc)

        self.log('training_stage', float(self.training_phase))

        if self.training_phase == 0:
            stage_key = 'warmup'
        elif self.training_phase == 1:
            stage_key = 'nopush'
        else:
            stage_key = 'push'

        torch.save(obj=self.ppnet, f=os.path.join(self.checkpoints_dir, f'{stage_key}_last.pth'))
        if not self.sanity_check_val and val_acc > self.best_acc:
            log(f'Saving best model, accuracy: ' + str(val_acc))
            self.best_acc = val_acc
            torch.save(obj=self.ppnet, f=os.path.join(self.checkpoints_dir, f'{stage_key}_best.pth'))

        if self.prototype_rebalancing is not None and self.trainer.global_step >= self.prototype_rebalancing:
            if self.rebalance_epoch_counter % self.prototype_rebalance_every == 0:
                self.rebalance_prototypes()
                self.rebalancing_stats = {
                    'proto_class_patches_total': Counter(),
                    'patches_nearest_prototypes': Counter()
                }
            self.rebalance_epoch_counter += 1

        if self.sanity_check_val:
            self.sanity_check_val = False
        else:
            if self.lr_scheduler is not None and self.reduce_lr_on_plateau:
                self.lr_scheduler.step(val_acc)

    def _epoch_end(self, split_key: str):
        metrics = self.metrics[split_key]
        if len(self.batch_metrics) > 0:
            for key, values in self.batch_metrics.items():
                mean_value = float(np.mean(self.batch_metrics[key]))
                metrics[key] += mean_value
            metrics['n_batches'] += 1

        n_batches = metrics['n_batches']

        self.batch_metrics = defaultdict(list)

        # for key in ['loss', 'cross_entropy', 'cluster_cost', 'separation', 'proto_dist_cost']:
        for key in ['loss', 'cross_entropy', 'kld_loss']: #, 'cls_act_loss']:
            self.log(f'{split_key}/{key}', metrics[key] / n_batches)

        self.log(f'{split_key}/accuracy', metrics['n_correct'] / metrics['n_patches'])
        if hasattr(self.ppnet, 'last_layer'):
            self.log('l1', self.ppnet.last_layer.weight.norm(p=1).item())
        if hasattr(self.ppnet, 'nearest_proto_only') and self.ppnet.nearest_proto_only:
            self.log('gumbel_tau', self.ppnet.gumbel_tau)

    def training_epoch_end(self, step_outputs):
        return self._epoch_end('train')

    def validation_epoch_end(self, step_outputs):
        p = self.ppnet.prototype_vectors.view(self.ppnet.prototype_vectors.shape[0], -1).cpu()
        with torch.no_grad():
            p_avg_pair_dist = torch.mean(list_of_distances(p, p))
        self.log('p dist pair', p_avg_pair_dist.item())

        return self._epoch_end('val')

    def test_epoch_end(self, step_outputs):
        return self._epoch_end('test')

    def configure_optimizers(self):
        if self.training_phase == 0:  # warmup
            aspp_params = [
                self.ppnet.features.base.aspp.c0.weight,
                self.ppnet.features.base.aspp.c0.bias,
                self.ppnet.features.base.aspp.c1.weight,
                self.ppnet.features.base.aspp.c1.bias,
                self.ppnet.features.base.aspp.c2.weight,
                self.ppnet.features.base.aspp.c2.bias,
                self.ppnet.features.base.aspp.c3.weight,
                self.ppnet.features.base.aspp.c3.bias
            ]
            optimizer_specs = \
                [
                    {
                        'params': list(self.ppnet.add_on_layers.parameters()) + aspp_params,
                        'lr': self.warm_optimizer_lr_add_on_layers,
                        'weight_decay': self.warm_optimizer_weight_decay
                    },
                    {
                        'params': self.ppnet.prototype_vectors,
                        'lr': self.warm_optimizer_lr_prototype_vectors
                    }
                ]
        elif self.training_phase == 1:  # joint
            if hasattr(self.ppnet.features, 'base'):
                optimizer_specs = \
                    [
                        {
                            "params": get_params(self.ppnet.features, key="1x"),
                            'lr': self.joint_optimizer_lr_features,
                            'weight_decay': self.joint_optimizer_weight_decay
                        },
                        {
                            "params": get_params(self.ppnet.features, key="10x"),
                            'lr': 10 * self.joint_optimizer_lr_features,
                            'weight_decay': self.joint_optimizer_weight_decay
                        },
                        {
                            "params": get_params(self.ppnet.features, key="20x"),
                            'lr': 10 * self.joint_optimizer_lr_features,
                            'weight_decay': self.joint_optimizer_weight_decay
                        },
                        {
                            'params': self.ppnet.add_on_layers.parameters(),
                            'lr': self.joint_optimizer_lr_add_on_layers,
                            'weight_decay': self.joint_optimizer_weight_decay
                        },
                        {
                            'params': self.ppnet.prototype_vectors,
                            'lr': self.joint_optimizer_lr_prototype_vectors
                        }
                    ]
            else:
                if self.ppnet.no_prototypes:
                    optimizer_specs = \
                        [
                            {
                                "params": self.ppnet.features.parameters(),
                                'lr': self.joint_optimizer_lr_features,
                                'weight_decay': self.joint_optimizer_weight_decay
                            }
                        ]
                else:
                    optimizer_specs = \
                        [
                            {
                                "params": self.ppnet.features.parameters(),
                                'lr': self.joint_optimizer_lr_features,
                                'weight_decay': self.joint_optimizer_weight_decay
                            },
                            {
                                'params': self.ppnet.add_on_layers.parameters(),
                                'lr': self.joint_optimizer_lr_add_on_layers,
                                'weight_decay': self.joint_optimizer_weight_decay
                            },
                            {
                                'params': self.ppnet.prototype_vectors,
                                'lr': self.joint_optimizer_lr_prototype_vectors
                            }
                        ]
        else:  # last layer
            optimizer_specs = [
                {
                    'params': self.ppnet.last_layer.parameters(),
                    'lr': self.last_layer_optimizer_lr
                }
            ]

        optimizer = torch.optim.Adam(optimizer_specs)

        if self.training_phase == 1:
            if self.reduce_lr_on_plateau:
                self.lr_scheduler = ReduceLROnPlateau(
                    optimizer=optimizer,
                    mode='max',
                    verbose=True,
                    factor=0.1,
                    patience=4,
                    min_lr=1e-8
                )
            else:
                self.lr_scheduler = PolynomialLR(
                    optimizer=optimizer,
                    step_size=1,
                    iter_max=self.max_steps // self.iter_size,
                    power=self.poly_lr_power
                )

        return optimizer
