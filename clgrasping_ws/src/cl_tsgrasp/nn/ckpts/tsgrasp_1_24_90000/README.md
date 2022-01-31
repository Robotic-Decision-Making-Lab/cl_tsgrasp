To run this model, first set the tsgrasp commit via

```
git checkout 68dbe96c5f2496a8dc17ffce7d32b7081900cd2e
```

and use
```
cfg_str = """
training:
  gpus: 2
  batch_size: 30
  max_epochs: 100
  optimizer:
    learning_rate: 0.00025
    lr_decay: 0.99
  animate_outputs: false
  make_sc_curve: false
  use_wandb: false
  wandb:
    project: TSGrasp
    experiment: tsgrasp_1_24
    notes: Jan 15 run with all-frame loss, last-cam frame, and only one frame.
  save_animations: false
model:
  _target_: tsgrasp.net.lit_tsgraspnet.LitTSGraspNet
  model_cfg:
    backbone_model_name: MinkUNet14A
    D: 4
    backbone_out_dim: 128
    add_s_loss_coeff: 10
    bce_loss_coeff: 1
    width_loss_coeff: 1
    top_confidence_quantile: 1.0
    feature_dimension: 1
    pt_radius: 0.005
    grid_size: 0.005
    conv1_kernel_size: 3
    dilations:
    - 1 1 1 1
data:
  _target_: tsgrasp.data.lit_acronym_renderer_dm.LitTrajectoryDataset
  data_cfg:
    num_workers: 4
    data_proportion_per_epoch: 1
    dataroot: /home/tim/Research/tsgrasp/data/dataset
    frames_per_traj: 1
    points_per_frame: 90000
    min_pitch: 0.0
    max_pitch: 1.222
    augmentations:
      add_random_jitter: true
      random_jitter_sigma: 0.0001
      add_random_rotations: false
      add_random_rotation_about_z: true
    renderer:
      height: 300
      width: 300
      acronym_repo: /home/tim/Research/acronym
      mesh_dir: ${hydra:runtime.cwd}/data/obj/

ckpt_path: /home/tim/Research/cl_grasping/clgrasping_ws/src/cl_tsgrasp/nn/ckpts/tsgrasp_1_24_90000/model.ckpt
"""
```

artifact playertr/TSGrasp/model-3r7g1t3m:v99