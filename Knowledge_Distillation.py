"""Baseline train
- Author: Junghoon Kim
- Contact: placidus36@gmail.com
"""

import argparse
import os
from datetime import datetime
from typing import Any, Dict, Tuple, Union

import torch
import torch.nn as nn
import torch.optim as optim
import yaml
import wandb

from src.dataloader import create_dataloader
from src.loss import CustomCriterion, CustomCriterion_KD
from src.model import Model
from src.trainer import TorchTrainer
from src.utils.common import get_label_counts, read_yaml
from src.utils.torch_utils import check_runtime, model_info
from src.utils.setseed import setSeed
from swin.models import build_model
from swin.config import get_config
from collections import OrderedDict

def train(
    model_config: Dict[str, Any],
    data_config: Dict[str, Any],
    log_dir: str,
    fp16: bool,
    device: torch.device,
) -> Tuple[float, float, float]:
    """Train."""
    # save model_config, data_config
    with open(os.path.join(log_dir, "data.yml"), "w") as f:
        yaml.dump(data_config, f, default_flow_style=False)
    with open(os.path.join(log_dir, "model.yml"), "w") as f:
        yaml.dump(model_config, f, default_flow_style=False)

    model_instance = Model(model_config, verbose=True)
    model_path = os.path.join(log_dir, "best.pt")
    print(f"Model save path: {model_path}")

    model_instance.model.to(device)

    # Create dataloader
    train_dl, val_dl, test_dl = create_dataloader(data_config)

    # Create optimizer, scheduler, criterion
    optimizer = torch.optim.SGD(
        model_instance.model.parameters(), lr=data_config["INIT_LR"], momentum=0.9
    )
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer=optimizer,
        max_lr=data_config["INIT_LR"],
        steps_per_epoch=len(train_dl),
        epochs=data_config["EPOCHS"],
        pct_start=0.05,
    )
    criterion = CustomCriterion(
        samples_per_cls=get_label_counts(data_config["DATA_PATH"])
        if data_config["DATASET"] == "TACO"
        else None,
        device=device,
    )
    # Amp loss scaler
    scaler = (
        torch.cuda.amp.GradScaler() if fp16 and device != torch.device("cpu") else None
    )

    # Create trainer
    trainer = TorchTrainer(
        model=model_instance.model,
        criterion=criterion,
        optimizer=optimizer,
        scheduler=scheduler,
        scaler=scaler,
        device=device,
        model_path=model_path,
        verbose=1,
    )
    best_acc, best_f1 = trainer.train(
        train_dataloader=train_dl,
        n_epoch=data_config["EPOCHS"],
        val_dataloader=val_dl if val_dl else test_dl,
    )

    # evaluate model with test set
    model_instance.model.load_state_dict(torch.load(model_path))
    test_loss, test_f1, test_acc = trainer.test(
        model=model_instance.model, test_dataloader=val_dl if val_dl else test_dl
    )
    return test_loss, test_f1, test_acc


"""Knowledge Distillation
- Author: Sungjin Park, Sangwon Lee  
- Contact: 8639sung@gmail.com
"""
def train_kd(
    student_model_config: Dict[str, Any],
    teacher_model_config: Dict[str, Any],
    teacher_ckpt: str,
    data_config: Dict[str, Any],
    log_dir: str,
    fp16: bool,
    device: torch.device,
) -> Tuple[float, float, float]:
    """Train."""

    """
    Load student model
    """
    # save student_model_config, data_config
    with open(os.path.join(log_dir, "data.yml"), "w") as f:
        yaml.dump(data_config, f, default_flow_style=False)
    with open(os.path.join(log_dir, "model.yml"), "w") as f:
        yaml.dump(student_model_config, f, default_flow_style=False)

    student_model_instance = Model(student_model_config, verbose=True)
    student_model = student_model_instance.model
    model_path = os.path.join(log_dir, "best.pt")
    print(f"Student_Model save path: {model_path}")
  
    student_model.to(device)

    """
    Load teacher model
    """
    teacher_model = build_model(teacher_model_config)
    
    # load pre-trained teacher_model weight
    checkpoint = teacher_ckpt
    
    print(f"Teacher_Model load: {checkpoint}")
    state_dict = torch.load(checkpoint, map_location=device)
    temp = OrderedDict()
    for n, v in state_dict.items():
        name = n.replace("head.","") 
        temp[name] = v
    teacher_model.load_state_dict(temp, strict=False)

    teacher_model.to(device)

    # Create dataloader
    train_dl, val_dl, test_dl = create_dataloader(data_config)

    # Create optimizer, scheduler, criterion
    optimizer = torch.optim.SGD(
        student_model.parameters(), lr=data_config["INIT_LR"], momentum=0.9
    )
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer=optimizer,
        max_lr=data_config["INIT_LR"],
        steps_per_epoch=len(train_dl),
        epochs=data_config["EPOCHS"],
        pct_start=0.05,
    )
    criterion = CustomCriterion_KD(
        samples_per_cls=get_label_counts(data_config["DATA_PATH"])
        if data_config["DATASET"] == "TACO"
        else None,
        device=device,
    )
    # Amp loss scaler
    scaler = (
        torch.cuda.amp.GradScaler() if fp16 and device != torch.device("cpu") else None
    )

    # Create trainer
    trainer = TorchTrainer(
        model=student_model,
        teacher_model=teacher_model,
        criterion=criterion,
        optimizer=optimizer,
        scheduler=scheduler,
        scaler=scaler,
        device=device,
        model_path=model_path,
        verbose=1,
    )
    
    best_acc, best_f1 = trainer.train_kd(
        train_dataloader=train_dl,
        n_epoch=data_config["EPOCHS"],
        val_dataloader=val_dl if val_dl else test_dl,
    )

    student_model.load_state_dict(torch.load(model_path))
    test_loss, test_f1, test_acc = trainer.test(
        model=student_model, test_dataloader=val_dl if val_dl else test_dl
    )
    return test_loss, test_f1, test_acc

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Distill model.")
    
    # Model
    parser.add_argument(
        "--model",
        default="configs/model/mobilenetv3.yaml",
        type=str,
        help="student model config",
    )

    # Teacher Model for knowledge dilstillation
    parser.add_argument(
        "--teacher_model",
        default="swin/configs/swin_base_patch4_window7_224.yaml",
        type=str,
        help="teacher model config",    
    )    
    parser.add_argument(
        "--teacher_checkpoints",
        default="exp/swin_teacher/best.pt",
        type=str,
        help="teacher model checkpotins",
    )
    
    # Distill Mode Check
    parser.add_argument(
        "--distill_mode",
        default=False,
        type=bool,
        help="True : Distillation Mode Train, False : General Train"
    )

    # Data
    parser.add_argument(
        "--data", default="configs/data/taco.yaml", type=str, help="data config"
    )

    # Seed & Wandb
    parser.add_argument(
        "--seed", default=42, type=int, help="seed"
    )
    parser.add_argument(
        "--run_name", default="exp", type=str, help="run name for wandb"
    )
    args = parser.parse_args()

    # Set
    model_config = read_yaml(cfg=args.model)
    tc_model_config = get_config(args.teacher_model) # yaml 읽는게 student랑 다름
    tc_ckpt = args.teacher_checkpoints
    data_config = read_yaml(cfg=args.data)
    setSeed(args.seed)

    data_config["DATA_PATH"] = os.environ.get("SM_CHANNEL_TRAIN", data_config["DATA_PATH"])

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    log_dir = os.environ.get("SM_MODEL_DIR", os.path.join("exp", 'latest'))

    if os.path.exists(log_dir): 
        modified = datetime.fromtimestamp(os.path.getmtime(log_dir + '/best.pt'))
        new_log_dir = os.path.dirname(log_dir) + '/' + modified.strftime("%Y-%m-%d_%H-%M-%S")
        os.rename(log_dir, new_log_dir)

    os.makedirs(log_dir, exist_ok=True)

    # for wandb
    wandb.init(project='lightweight', entity='cv4', name = args.run_name, save_code = True)
    wandb.run.name = args.run_name
    wandb.run.save()
    wandb.config.update(model_config)
    wandb.config.update(data_config)

    # Distill mode check
    if args.distill_mode == True:
        # valid ckpt
        assert os.path.isfile(tc_ckpt), "No ckpt file found at {}".format(tc_ckpt)
        
        # distillation train
        test_loss, test_f1, test_acc = train_kd(
            student_model_config=model_config,
            teacher_model_config=tc_model_config,
            teacher_ckpt=tc_ckpt,
            data_config=data_config,
            log_dir=log_dir,
            fp16=data_config["FP16"],
            device=device,)
    else:
        # general train
        test_loss, test_f1, test_acc = train(
            model_config=model_config,
            data_config=data_config,
            log_dir=log_dir,
            fp16=data_config["FP16"],
            device=device,
        )
