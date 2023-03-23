#!/bin/bash
# Run script
method="$1"
epochs=120
val_last=20
step=10

# Train
CUDA_VISIBLE_DEVICES=$2 python train.py --ckpt_dir ckpt/${method} --epochs ${epochs} \
    --testsets DIS-VD+DIS-TE1+DIS-TE2+DIS-TE3+DIS-TE4


nvidia-smi | head -1
hostname