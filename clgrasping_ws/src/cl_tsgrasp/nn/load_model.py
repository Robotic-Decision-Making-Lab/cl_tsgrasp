#! /home/tim/anaconda3/envs/tsgrasp/bin/python
# shebang is for the Python3 environment with the network dependencies

import sys
sys.path.append("/home/tim/Research/cl_grasping/clgrasping_ws/src/cl_tsgrasp/nn/tsgrasp")
from tsgrasp.net.lit_tsgraspnet import LitTSGraspNet

from hydra.utils import instantiate
from omegaconf import OmegaConf
import torch

cfg_str = """
training:
  gpus: 1
  batch_size: 3
  max_epochs: 100
  optimizer:
    learning_rate: 0.0002
    lr_decay: 0.95

model:
  _target_: tsgrasp.net.lit_tsgraspnet.LitTSGraspNet
  model_cfg:
    backbone_model_name: MinkUNet14A
    D: 4
    backbone_out_dim: 128
    add_s_loss_coeff: 100
    bce_loss_coeff: 1
    width_loss_coeff: 10
    top_confidence_quantile: 1.0
    feature_dimension: 1
    pt_radius: 0.005
    grid_size: 0.005
    conv1_kernel_size: 3
    dilations:
    - 1 1 1 1

ckpt_path: /home/tim/Research/cl_grasping/clgrasping_ws/src/cl_tsgrasp/nn/ckpts/tsgrasp_data_aug/model.ckpt
"""

def load_model():
    cfg = OmegaConf.create(cfg_str)
    pl_model = instantiate(cfg.model, training_cfg=cfg.training)
    pl_model.load_state_dict(torch.load(cfg.ckpt_path)['state_dict'])
    return pl_model