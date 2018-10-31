import os
import sys

from tensorboardX import SummaryWriter

sys.path.extend([os.path.dirname(os.getcwd())])
from myutils.myDataLoader import ISICdata
from myutils.myENet import Enet
from myutils.myNetworks import UNet, SegNet
from myutils.myLoss import CrossEntropyLoss2d
import warnings
from tqdm import tqdm
from myutils.myUtils import *

import csv
import argparse

warnings.filterwarnings('ignore')
writer = SummaryWriter()

# torch.set_num_threads(3)  # set by deafault to 1
root = "datasets/ISIC2018"

class_number = 2
lr = 1e-4
weigth_decay = 1e-6
use_cuda = True
device = torch.device("cuda" if use_cuda and torch.cuda.is_available() else "cpu")
number_workers = 4
labeled_batch_size = 4
unlabeled_batch_size = 4
val_batch_size = 1

max_epoch_pre = 100
max_epoch_baseline = 100
max_epoch_ensemble = 100
train_print_frequncy = 10
val_print_frequncy = 10

Equalize = False
# data for semi-supervised training
labeled_data = ISICdata(root=root, model='labeled', mode='semi', transform=True,
                        dataAugment=False, equalize=Equalize)
unlabeled_data = ISICdata(root=root, model='unlabeled', mode='semi', transform=True,
                          dataAugment=False, equalize=Equalize)
test_data = ISICdata(root=root, model='test', mode='semi', transform=True,
                     dataAugment=False, equalize=Equalize)

labeled_loader_params = {'batch_size': labeled_batch_size,
                         'shuffle': True,
                         'num_workers': number_workers,
                         'pin_memory': True}

unlabeled_loader_params = {'batch_size': unlabeled_batch_size,
                           'shuffle': True,
                           'num_workers': number_workers,
                           'pin_memory': True}

labeled_data = DataLoader(labeled_data, **labeled_loader_params)
unlabeled_data = DataLoader(unlabeled_data, **unlabeled_loader_params)
test_data = DataLoader(test_data, **unlabeled_loader_params)

## networks and optimisers
nets = [Enet(class_number),
        #UNet(class_number),
        SegNet(class_number)]

# for i, net_i in enumerate(nets):
#     nets[i] = net_i.to(device)
nets = map_(lambda x: x.to(device), nets)

map_location = lambda storage, loc: storage

optimizers = [torch.optim.Adam(nets[0].parameters(), lr=lr, weight_decay=weigth_decay),
              torch.optim.Adam(nets[1].parameters(), lr=lr, weight_decay=weigth_decay)]
              #torch.optim.Adam(nets[2].parameters(), lr=lr, weight_decay=weigth_decay)]

## loss
class_weigth = [1 * 0.1, 3.53]
class_weigth = torch.Tensor(class_weigth)
criterion = CrossEntropyLoss2d(class_weigth).to(device) if (
        torch.cuda.is_available() and use_cuda) else CrossEntropyLoss2d(
    class_weigth)
ensemble_criterion = JensenShannonDivergence(reduce=True, size_average=False)

historical_score_dict = {'enet': 0,
                         'unet': 0,
                         'segnet': 0,
                         'mv': 0,
                         'jsd': 0}


def pre_train():
    """
    This function performs the training with the unlabeled images.
    """
    map_(lambda x: x.train(), nets)

    global historical_score_dict

    nets_path = ['checkpoint/best_ENet_pre-trained.pth',
                 #'checkpoint/best_UNet_pre-trained.pth',
                 'checkpoint/best_SegNet_pre-trained.pth']
    dice_meters = [AverageValueMeter(), AverageValueMeter()]#, AverageValueMeter()]

    for epoch in range(max_epoch_pre):
        map_(lambda x: x.reset(), dice_meters)

        if epoch % 5 == 0:
            learning_rate_decay(optimizers, 0.95)

        for i, (img, mask, _) in tqdm(enumerate(labeled_data)):
            img, mask = img.to(device), mask.to(device)

            for idx, net_i in enumerate(nets):
                optimizers[idx].zero_grad()
                pred = nets[idx](img)
                loss = criterion(pred, mask.squeeze(1))
                loss.backward()
                optimizers[idx].step()
                dice_score = dice_loss(pred2segmentation(pred), mask.squeeze(1))
                dice_meters[idx].add(dice_score)

        # print(
        #     'traing epoch {0:1d}/{1:d} pre-training: enet_dice_score: {2:.3f}, unet_dice_score: {3:.3f}, segnet_dice_score: {4:.3f}'.format(
        #         epoch + 1, max_epoch_pre, dice_meters[0].value()[0],
        #         dice_meters[1].value()[0], dice_meters[2].value()[0]))
        # without Unet
        print(
            'traing epoch {0:1d}/{1:d} pre-training: enet_dice_score: {2:.3f}, segnet_dice_score: {3:.3f}'.format(
                epoch + 1, max_epoch_pre, dice_meters[0].value()[0], dice_meters[1].value()[0]))

        score_meters, ensemble_score = test(nets, test_data, device=device)

        #visualize(nets, dataset, number_of_images)

        # print(
        #     'val epoch {0:d}/{1:d} pre-training: enet_dice_score: {2:.3f}, unet_dice_score: {3:.3f}, segnet_dice_score: {4:.3f}, with majorty voting: {5:.3f}'.format(
        #         epoch + 1,
        #         max_epoch_pre,
        #         score_meters[0].value()[0],
        #         score_meters[1].value()[0],
        #         score_meters[2].value()[0],
        #         ensemble_score.value()[0]))

        # without Unet
        print(
            'val epoch {0:d}/{1:d} pre-training: enet_dice_score: {2:.3f}, segnet_dice_score: {3:.3f}, with majorty voting: {4:.3f}'.format(
                epoch + 1,
                max_epoch_pre,
                score_meters[0].value()[0],
                score_meters[1].value()[0],
                ensemble_score.value()[0]))
        historical_score_dict = save_models(nets, nets_path, score_meters, epoch, historical_score_dict)

    # train_baseline(nets, nets_path, labeled_data, unlabeled_data)
    # train_ensemble(nets, nets_path, labeled_data, unlabeled_data )


def train_baseline(nets_, nets_path_, labeled_loader_, unlabeled_loader_, cvs_writer):
    """
    This function performs the training of the pre-trained models with the labeled and unlabeled data.
    """
    #  loading pre-trained models
    map_(lambda x, y: [x.load_state_dict(torch.load(y, map_location=lambda storage, loc: storage)), x.train()], nets_, nets_path_)
    global historical_score_dict
    nets_path = ['checkpoint/best_ENet_baseline.pth',
                 #'checkpoint/best_UNet_baseline.pth',
                 'checkpoint/best_SegNet_baseline.pth']
    dice_meters = [AverageValueMeter(), AverageValueMeter()]#, AverageValueMeter()]

    # testing initialization
    score_meters, ensemble_score = test(nets_, test_data, device=device)

    print(
        'val before starting baseline training: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}, with majorty_voting={4:.3f}'.format(
            0,
            max_epoch_pre,
            score_meters[0].value()[0],
            score_meters[1].value()[0],
            ensemble_score.value()[0]))
    cvs_writer.writerow({'Epoch': 0,
                         'ENet_Score': score_meters[0].value()[0],
                         'SegNet_Score': score_meters[1].value()[0],
                         'MV_Score': ensemble_score.value()[0]})

    # testing the Jizong's hypothesis about learning rate decay
    # (set lr = 1e-4 or lr = 1e-5 during all training)
    LEARNING_RATE = 1e-4
    for opti_i in optimizers:
        for param_group in opti_i.param_groups:
            param_group['lr'] = LEARNING_RATE = 1e-4

    for epoch in range(max_epoch_baseline):
        print('epoch = {0:4d}/{1:4d} training baseline'.format(epoch, max_epoch_baseline))

        # if epoch % 5 == 0:
        #     learning_rate_decay(optimizers, 0.95)
        
        # train with labeled data
        for _ in tqdm(range(max(len(labeled_loader_), len(unlabeled_loader_)))):

            imgs, masks, _ = image_batch_generator(labeled_loader_, device=device)
            _, llost_list, dice_score = batch_labeled_loss_(imgs, masks, nets_, criterion)

            # train with unlabeled data
            imgs, _, _ = image_batch_generator(unlabeled_loader_, device=device)
            pseudolabel, predictions = get_mv_based_labels(imgs, nets_)
            ulost_list = cotraining(predictions, pseudolabel, nets_, criterion, device=device)
            total_loss = [x + y for x, y in zip(llost_list, ulost_list)]

            for idx in range(len(optimizers)):
                optimizers[idx].zero_grad()
                total_loss[idx].backward()
                optimizers[idx].step()
                dice_meters[idx].add(dice_score[idx])

        # print(
        #     'train epoch {0:1d}/{1:d} baseline: enet_dice_score={2:.3f}, unet_dice_score={3:.3f}, segnet_dice_score={4:.3f}'.format(
        #         epoch + 1, max_epoch_pre, dice_meters[0].value()[0],
        #         dice_meters[1].value()[0], dice_meters[2].value()[0]))

        # without Unet
        print(
            'train epoch {0:1d}/{1:d} baseline: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}'.format(
                epoch + 1, max_epoch_pre, dice_meters[0].value()[0], dice_meters[1].value()[0]))

        score_meters, ensemble_score = test(nets_, test_data, device=device)

        # print(
        #     'val epoch {0:d}/{1:d} baseline: enet_dice_score={2:.3f}, unet_dice_score={3:.3f}, segnet_dice_score={4:.3f}, with majorty_voting={5:.3f}'.format(
        #         epoch + 1,
        #         max_epoch_pre,
        #         score_meters[0].value()[0],
        #         score_meters[1].value()[0],
        #         score_meters[2].value()[0],
        #         ensemble_score.value()[0]))

        # without Unet
        print(
            'val epoch {0:d}/{1:d} baseline: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}, with majorty_voting={4:.3f}'.format(
                epoch + 1,
                max_epoch_pre,
                score_meters[0].value()[0],
                score_meters[1].value()[0],
                ensemble_score.value()[0]))
        cvs_writer.writerow({'Epoch': epoch + 1,
                             'ENet_Score': score_meters[0].value()[0],
                             'SegNet_Score': score_meters[1].value()[0],
                             'MV_Score': ensemble_score.value()[0]})

        historical_score_dict = save_models(nets_, nets_path, score_meters, epoch, historical_score_dict)
        if ensemble_score.value()[0] > historical_score_dict['mv']:
            historical_score_dict['mv'] = ensemble_score.value()[0]


def train_ensemble(nets_, nets_path_, labeled_loader_, unlabeled_loader_, cvs_writer):
    """
    train_ensemble function performs the ensemble training with the unlabeled subset.
    """

    global historical_score_dict
    map_(lambda x, y: [x.load_state_dict(torch.load(y, map_location=lambda storage, loc: storage)), x.train()], nets_, nets_path_)

    nets_path = ['checkpoint/best_ENet_ensemble.pth',
                 # 'checkpoint/best_UNet_ensemble.pth',
                 'checkpoint/best_SegNet_ensemble.pth']

    dice_meters = [AverageValueMeter(), AverageValueMeter()]#, AverageValueMeter()]

    # testing initialization
    score_meters, ensemble_score = test(nets_, test_data, device=device)

    print(
        'val before starting ensemble training: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}, with majorty_voting={4:.3f}'.format(
            0,
            max_epoch_pre,
            score_meters[0].value()[0],
            score_meters[1].value()[0],
            ensemble_score.value()[0]))
    cvs_writer.writerow({'Epoch': 0,
                         'ENet_Score': score_meters[0].value()[0],
                         'SegNet_Score': score_meters[1].value()[0],
                         'MV_Score': ensemble_score.value()[0]})

    # testing the Jizong's hypothesis about learning rate decay
    # (set lr = 1e-4 or lr = 1e-5 during all training)
    LEARNING_RATE = 1e-4
    for opti_i in optimizers:
        for param_group in opti_i.param_groups:
            param_group['lr'] = LEARNING_RATE = 1e-4

    for epoch in range(max_epoch_ensemble):
        print('epoch = {0:4d}/{1:4d}'.format(epoch, max_epoch_ensemble))
        for idx, _ in enumerate(nets_):
            dice_meters[idx].reset()

        # if epoch % 5 == 0:
        #     learning_rate_decay(optimizers, 0.95)

        for _ in tqdm(range(max(len(labeled_loader_), len(unlabeled_loader_)))):
            # === train with labeled data ===
            imgs, masks, _ = image_batch_generator(labeled_loader_, device=device)
            _, llost_list, dice_score = batch_labeled_loss_(imgs, masks, nets_, criterion)

            # train with unlabeled data
            imgs, _, _ = image_batch_generator(unlabeled_loader_, device=device)
            pseudolabel, predictions = get_mv_based_labels(imgs, nets_)
            jsdLoss = get_loss(predictions)
            # jsdLoss.requires_grad = False

            total_loss = [x + jsdLoss for x in llost_list]
            for idx, optim in enumerate(optimizers):
                optim.zero_grad()
                total_loss[idx].backward(retain_graph=True)
                optim.step()
                dice_meters[idx].add(dice_score[idx])

        print(
            'train epoch {0:1d}/{1:d} baseline: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}'.format(
                epoch + 1, max_epoch_pre, dice_meters[0].value()[0], dice_meters[1].value()[0]))

        score_meters, ensemble_score = test(nets_, test_data, device=device)

        print(
            'val epoch {0:d}/{1:d} baseline: enet_dice_score={2:.3f}, segnet_dice_score={3:.3f}, with majorty_voting={4:.3f}'.format(
                epoch + 1,
                max_epoch_pre,
                score_meters[0].value()[0],
                score_meters[1].value()[0],
                ensemble_score.value()[0]))

        cvs_writer.writerow({'Epoch': epoch + 1,
                             'ENet_Score': score_meters[0].value()[0],
                             'SegNet_Score': score_meters[1].value()[0],
                             'MV_Score': ensemble_score.value()[0]})

        historical_score_dict = save_models(nets_, nets_path, score_meters, epoch, historical_score_dict)
        if ensemble_score.value()[0] > historical_score_dict['jsd']:
            historical_score_dict['jsd'] = ensemble_score.value()[0]


if __name__ == "__main__":
    PRE_TRAINING = False
    BASELINE = False
    ENSEMBLE = False

    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-training", type=str2bool, nargs='?', const=False, default=PRE_TRAINING,
                        help="Whether to pre-train the models.")
    parser.add_argument("--baseline", type=str2bool, nargs='?', const=False, default=BASELINE,
                        help="Whether to train the baseline models.")
    parser.add_argument("--ensemble", type=str2bool, nargs='?', const=False, default=ENSEMBLE,
                        help="Whether to train the ensemble models.")

    args = parser.parse_args()

    nets_path_ = ['checkpoint/best_ENet_pre-trained.pth',
                  # 'checkpoint/best_UNet_pre-trained.pth',
                  'checkpoint/best_SegNet_pre-trained.pth']

    if args.pre_training:
        # Pre-training Stage
        print('STARTING THE PRE-TRAINING STAGE')
        pre_train()
    elif args.baseline:
        # Baseline Training Stage
        print('STARTING THE BASELINE TRAINING STAGE')
        baseline_file = open('output_baseline_31102018.csv', 'w')
        baseline_fields = ['Epoch', 'ENet_Score', 'SegNet_Score', 'MV_Score']
        baseline_writer = csv.DictWriter(baseline_file, fieldnames=baseline_fields)
        baseline_writer.writeheader()
        train_baseline(nets, nets_path_, labeled_data, unlabeled_data, baseline_writer)
    elif args.ensemble:
        # Ensemble Training Stage
        print('STARTING THE ENSEMBLE TRAINING STAGE')
        ensemble_file = open('output_ensemble_31102018.csv', 'w')
        ensemble_fields = ['Epoch', 'ENet_Score', 'SegNet_Score', 'MV_Score']
        ensemble_writer = csv.DictWriter(ensemble_file, fieldnames=ensemble_fields)
        ensemble_writer.writeheader()
        train_ensemble(nets, nets_path_, labeled_data, unlabeled_data, ensemble_writer)

    # nets_path_ = ['checkpoint/best_ENet_pre-trained.pth',
    #               # 'checkpoint/best_UNet_pre-trained.pth',
    #               'checkpoint/best_SegNet_pre-trained.pth']
    # print('STARTING THE ENSEMBLE TRAINING STAGE')
    # ensemble_file = open('output_ensemble_31102018.csv', 'w')
    # ensemble_fields = ['Epoch', 'ENet_Score', 'SegNet_Score', 'MV_Score']
    # ensemble_writer = csv.DictWriter(ensemble_file, fieldnames=ensemble_fields)
    # ensemble_writer.writeheader()
    # train_ensemble(nets, nets_path_, labeled_data, unlabeled_data, ensemble_writer)
