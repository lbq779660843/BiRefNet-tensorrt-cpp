import os
import argparse
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable

from config import Config
from loss import saliency_structure_consistency, PixLoss
from utils import generate_smoothed_gt
from dataset import MyData
from models.baseline import BSL
from models.pvtvp import PVTVP
from utils import Logger, AverageMeter, set_seed
from evaluation.valid import valid


# Parameter from command line
parser = argparse.ArgumentParser(description='')
parser.add_argument('--resume',
                    default=None,
                    type=str,
                    help='path to latest checkpoint')
parser.add_argument('--epochs', default=100, type=int)
parser.add_argument('--trainset',
                    default='DIS5K',
                    type=str,
                    help="Options: 'DIS5K'")
parser.add_argument('--ckpt_dir', default=None, help='Temporary folder')

parser.add_argument('--testsets',
                    default='DIS-VD',
                    type=str,
                    help="Options: 'DIS-VD+DIS-TE1+DIS-TE2+DIS-TE3+DIS-TE4'")

args = parser.parse_args()


config = Config()

# Prepare dataset
training_set = 'DIS-TR'
data_loader_train = torch.utils.data.DataLoader(
    dataset=MyData(data_root=os.path.join(config.data_root_dir, config.dataset, training_set), image_size=config.size, is_train=True),
    batch_size=config.batch_size, shuffle=True, num_workers=config.num_workers, pin_memory=True
)
print(len(data_loader_train), "batches of train dataloader {} have been created.".format(training_set))

test_loaders = {}
for testset in args.testsets.split('+'):
    data_loader_test = torch.utils.data.DataLoader(
        dataset=MyData(data_root=os.path.join(config.data_root_dir, config.dataset, testset), image_size=config.size, is_train=False),
        batch_size=config.batch_size_valid, shuffle=False, num_workers=config.num_workers, pin_memory=True
    )
    print(len(data_loader_test), "batches of valid dataloader {} have been created.".format(testset))
    test_loaders[testset] = data_loader_test

if config.rand_seed:
    set_seed(config.rand_seed)

# make dir for ckpt
os.makedirs(args.ckpt_dir, exist_ok=True)

# Init log file
logger = Logger(os.path.join(args.ckpt_dir, "log.txt"))
logger_loss_file = os.path.join(args.ckpt_dir, "log_loss.txt")
logger_loss_idx = 1

# Init model
device = torch.device(config.device)

if config.model == 'BSL':
    model = BSL().to(device)
elif config.model == 'PVTVP':
    model = PVTVP().to(device)

# Setting optimizer
if config.optimizer == 'AdamW':
    optimizer = optim.AdamW(params=model.parameters(), lr=config.lr, weight_decay=1e-2)
elif config.optimizer == 'Adam':
    optimizer = optim.Adam(params=model.parameters(), lr=config.lr, weight_decay=0)
lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
    optimizer,
    milestones=[lde if lde > 0 else args.epochs + lde + 1 for lde in config.lr_decay_epochs],
    gamma=0.1
)

if config.lambda_adv_g:
    # AIL
    from loss import Discriminator
    disc = Discriminator(channels=3, img_size=config.size).to(device)
    Tensor = torch.cuda.FloatTensor if (True if torch.cuda.is_available() else False) else torch.FloatTensor
    adv_criterion = nn.BCELoss()
    if config.optimizer == 'AdamW':
        optimizer_d = optim.AdamW(params=model.parameters(), lr=config.lr, weight_decay=1e-2)
    elif config.optimizer == 'Adam':
        optimizer_d = optim.Adam(params=model.parameters(), lr=config.lr, weight_decay=0)
    lr_scheduler_d = torch.optim.lr_scheduler.MultiStepLR(
        optimizer_d,
        milestones=[lde if lde > 0 else args.epochs + lde + 1 for lde in config.lr_decay_epochs],
        gamma=0.1
    )

# Freeze the backbone...
if config.freeze_bb:
    for key, value in model.named_parameters():
        if 'bb.' in key:
            value.requires_grad = False


# log model and optimizer params
logger.info("Model details:")
logger.info(model)
logger.info("Optimizer details:")
logger.info(optimizer)
logger.info("Scheduler details:")
logger.info(lr_scheduler)
logger.info("Other hyperparameters:")
logger.info(args)
print('batch size:', config.batch_size)



# Setting Loss
pix_loss = PixLoss()


def main():
    # Optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            model.load_state_dict(torch.load(args.resume))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    for epoch in range(model.epoch, args.epochs+1):
        train_loss = train(epoch)
        model.epoch = epoch
        # Save checkpoint
        if epoch >= args.epochs - config.val_last and (args.epochs - epoch) % config.save_step == 0:
            torch.save(model.state_dict(), os.path.join(args.ckpt_dir, 'ep{}.pth'.format(epoch)))
            num_image_testset_all = {'DIS-VD': 470, 'DIS-TE1': 500, 'DIS-TE2': 500, 'DIS-TE3': 500, 'DIS-TE4': 500}
            num_image_testset = {}
            for testset in args.testsets.split('+'):
                if 'DIS-TE' in testset:
                    num_image_testset[testset] = num_image_testset_all[testset]
            weighted_score = {}
            for testset, data_loader_test in test_loaders.items():
                performance_dict = valid(
                    model,
                    data_loader_test,
                    pred_dir='.',
                    method=args.ckpt_dir.split('/')[-1] if args.ckpt_dir.split('/')[-1].strip('.').strip('/') else 'tmp_val',
                    testset=testset,
                    only_S_MAE=config.only_S_MAE
                )
                print('Test set: {}:'.format(testset))
                print('Fmax: {:.4f}, Smeasure: {:.4f}, MAE: {:.4f}'.format(
                    performance_dict['f_max'], performance_dict['sm'], performance_dict['mae'])
                )
            # Compute weighted scores of all testsets.
            for k_metric, v in performance_dict.items():
                if v == -1:
                    continue
                if not weighted_score.get(k_metric):
                    weighted_score[k_metric] = v * (num_image_testset[testset] / sum(list(num_image_testset.values())))
                else:
                    weighted_score[k_metric] += v * (num_image_testset[testset] / sum(list(num_image_testset.values())))
            print('>>>>>>>>>>>>>>weighted_score:<<<<<<<<<<<<<<\n')
            for k, v in weighted_score.items():
                print(k, '\t', '{:.4f}'.format(v))
            print('--' * 5)
        lr_scheduler.step()
        if config.lambda_adv_g:
            lr_scheduler_d.step()


def train(epoch):
    loss_log = AverageMeter()
    global logger_loss_idx
    model.train()

    for batch_idx, batch in enumerate(data_loader_train):
        inputs = batch[0].to(torch.device(config.device))
        gts = batch[1].squeeze(0).to(torch.device(config.device))
        class_labels = batch[2].to(torch.device(config.device))

        if config.auxiliary_classification:
            scaled_preds, class_preds = model(inputs)
            loss_cls = F.cross_entropy(class_preds, class_labels)
        else:
            scaled_preds = model(inputs)
            loss_cls = 0.

        # Loss
        loss_pix = pix_loss(scaled_preds, gts)
        # Tricks
        if config.label_smoothing:
            loss_pix = 0.5 * (loss_pix + pix_loss(scaled_preds, generate_smoothed_gt(gts)))
        # since there may be several losses for sal, the lambdas for them (lambdas_pix) are inside the loss.py
        loss = loss_pix * 1.0 + loss_cls * 1.0

        if config.lambda_adv_g:
            # gen
            valid = Variable(Tensor(scaled_preds[-1].shape[0], 1).fill_(1.0), requires_grad=False)
            adv_loss_g = adv_criterion(disc(scaled_preds[-1] * inputs), valid)
            loss += adv_loss_g * config.lambda_adv_g
        loss_log.update(loss, inputs.size(0))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if config.lambda_adv_g and batch_idx % 2 == 0:
            # disc
            fake = Variable(Tensor(scaled_preds[-1].shape[0], 1).fill_(0.0), requires_grad=False)
            optimizer_d.zero_grad()
            adv_loss_real = adv_criterion(disc(gts * inputs), valid)
            adv_loss_fake = adv_criterion(disc(scaled_preds[-1].detach() * inputs.detach()), fake)
            adv_loss_d = (adv_loss_real + adv_loss_fake) / 2 * config.lambda_adv_d
            adv_loss_d.backward()
            optimizer_d.step()

        # Logger
        if batch_idx % 20 == 0:
            # NOTE: Top2Down; [0] is the grobal slamap and [5] is the final output
            info_progress = 'Epoch[{0}/{1}] Iter[{2}/{3}]'.format(epoch, args.epochs, batch_idx, len(data_loader_train))
            info_loss = 'Training Loss: loss_pix: {:.3f}'.format(loss_pix)
            if config.lambda_adv_g:
                info_loss += ', loss_adv: {:.3f}, loss_adv_disc: {:.3f}'.format(adv_loss_g * config.lambda_adv_g, adv_loss_d * config.lambda_adv_d)
            logger.info(''.join((info_progress, info_loss)))
    info_loss = '@==Final== Epoch[{0}/{1}]  Training Loss: {loss.avg:.3f}  '.format(epoch, args.epochs, loss=loss_log)
    logger.info(info_loss)

    return loss_log.avg


if __name__ == '__main__':
    main()