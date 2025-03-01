import argparse
import logging
import os
import sys
import numpy as np
import torch
import torch.nn as nn
from torch import optim
from tqdm import tqdm
from eval.eval import eval_net,eval_new
from utilsS.get_dataset import get_dataset
from utilsS.get_model import get_model
from torchsummary import summary
from tensorboardX import SummaryWriter
from utilsS.dataset import BasicDataset, setup_seed
from torch.utils.data import DataLoader, random_split
from utilsS.get_loss import get_loss
from eval.data_io import save_train_config,save_val_result,save_train_result
from eval.AverageMeter import AverageMeter
from utilsS.new_metrics import iou,dice
from unet.unet_parts import *

dir_checkpoint = 'D:/ACPA_Net/checkpoints/'

def train_net(net,
              device,
              args,
              epochs=5,
              batch_size=1,
              lr=0.001,
              val_percent=0.1,
              save_cp=True,
              img_scale=0.5):

    train_dataset, val_dataset = get_dataset(args.dataset,img_scale,AU=args.dataAug ,wave=args.wave)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=0, pin_memory=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, num_workers=0, pin_memory=True, drop_last=True)

    n_train=len(train_loader)*batch_size
    n_val=len(val_loader)*batch_size
    writer = SummaryWriter(comment=f'LR_{lr}_BS_{batch_size}_SCALE_{img_scale}')
    global_step = 0
    best_epoch=0
    best_dice_coeff=0

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {lr}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_cp}
        Device:          {device.type}
        Images scaling:  {img_scale}
        Loss Function    {args.loss}
    ''')

    optimizer = optim.RMSprop(net.parameters(), lr=lr, weight_decay=1e-8, momentum=0.9)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min' , patience=3)
    criterion = get_loss(args)

    val_totall_result=[]
    train_totall_result=[]
    lr_list=[]

    for epoch in range(epochs):
        train_avg_meters = {'loss': AverageMeter(),
                            'Dice': AverageMeter(),
                            'Iou': AverageMeter()}

        net.train()

        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                imgs = batch['image']
                true_masks = batch['mask']
                if args.wave:
                    Wave_imgs = batch['wave']
                    for i,w in enumerate(Wave_imgs):
                        Wave_imgs[i]=Wave_imgs[i].to(device=device, dtype=torch.float32)

                imgs = imgs.to(device=device, dtype=torch.float32)

                mask_type = torch.float32
                true_masks = true_masks.to(device=device, dtype=mask_type)

                if args.wave:
                    masks_pred = net(imgs,Wave_imgs)
                else:
                    masks_pred = net(imgs)

                args.DS=False
                if args.DS:
                    loss = 0

                    loss_weight=[]
                    if args.DS=='Decoder':
                        loss_weight = [0.05, 0.1, 0.1, 0.1, 1]
                    elif args.DS=='Enconder+Decoder':
                        loss_weight = [0.1, 0.1, 0.1, 0.1, 0.1,0.1, 0.3]
                    elif args.DS=='DAHead':
                        loss_weight=[0.25,0.25,0.25,0.25]
                    else:
                        pass

                    for out, w in zip(masks_pred,loss_weight):
                        loss=loss+w*criterion(out, true_masks)
                    pred = torch.sigmoid(masks_pred[-1])
                else:
                    loss = criterion(masks_pred, true_masks)
                    pred = torch.sigmoid(masks_pred)

                train_avg_meters['loss'].update(loss.item(), batch_size)
                epoch_loss += loss.item()
                writer.add_scalar('Loss/train', loss.item(), global_step)
                pred = (pred > 0.5).float()
                train_avg_meters['Dice'].update(dice( pred,true_masks), batch_size)
                train_avg_meters['Iou'].update(iou(pred,true_masks), batch_size)
                pbar.set_postfix(**{'loss (batch)': loss.item(),'loss (avg)': train_avg_meters['loss'].avg})
                optimizer.zero_grad()
                loss.backward()

                nn.utils.clip_grad_value_(net.parameters(), 0.1)
                optimizer.step()
                pbar.update(imgs.shape[0])
                global_step += 1

                if global_step % (n_train // (1 * batch_size)) == 0:
                    for tag, value in net.named_parameters():
                        tag = tag.replace('.', '/')
                        writer.add_histogram('weights/' + tag, value.data.cpu().numpy(), global_step)

                    val_score = eval_new(net, val_loader, device,criterion,wave=args.wave)

                    scheduler.step(val_score['val_loss'].avg)

                    writer.add_scalar('learning_rate', optimizer.param_groups[0]['lr'], global_step)

                    logging.info('\n')
                    logging.info('Train Iou Coeff: {}'.format(train_avg_meters['Iou'].avg))
                    logging.info('Train Dice Coeff: {}'.format(train_avg_meters['Dice'].avg))
                    logging.info('Validation loss: {}'.format(val_score['val_loss'].avg))
                    logging.info('Validation Dice Coeff: {}'.format(val_score['dice'].avg))
                    logging.info('Validation Iou Coeff: {}'.format(val_score['iou'].avg))
                    logging.info('Validation Recall Coeff: {}'.format(val_score['recall'].avg))
                    logging.info('Validation Precision Coeff: {}'.format(val_score['precision'].avg))
                    logging.info('Validation F1_score Coeff (mean batch pr): {}'.format(val_score['f1_score'].avg))

                    writer.add_scalar('Loss_avg/train', train_avg_meters['loss'].avg, global_step)
                    writer.add_scalar('avg_Loss/test', val_score['val_loss'].avg, global_step)
                    writer.add_scalar('avg_Dice/test', val_score['dice'].avg, global_step)
                    writer.add_scalar('avg_Iou/test', val_score['iou'].avg, global_step)
                    writer.add_scalar('avg_Recall/test',val_score['recall'].avg, global_step)
                    writer.add_scalar('avg_Precisoin/test', val_score['precision'].avg, global_step)
                    writer.add_scalar('avg_F1_score(m batch pr)/test', val_score['f1_score'].avg, global_step)
                    writer.add_scalar('avg_ACC/test', val_score['Accuracy'].avg, global_step)
                    writer.add_scalar('avg_Sp/test',val_score['specificity'].avg, global_step)
                    writer.add_scalar('avg_Se/test', val_score['sensitivity'].avg, global_step)
                    writer.add_scalar('avg_AUC/test', val_score['Auc'].avg, global_step)

                    writer.add_images('images', imgs, global_step)
                    writer.add_images('masks/true', true_masks, global_step)
                    if args.DS:
                        writer.add_images('masks/pred', torch.sigmoid(masks_pred[-1]) > 0.5, global_step)
                    else:
                        writer.add_images('masks/pred', torch.sigmoid(masks_pred) > 0.5, global_step)

                    if (epoch+1)==1:
                        best_dice_coeff=val_score['dice'].avg
                    elif best_dice_coeff<val_score['dice'].avg:
                        best_dice_coeff=val_score['dice'].avg
                        try:
                            os.mkdir(dir_checkpoint)
                            logging.info('Created checkpoint directory')
                        except OSError:
                            pass
                        best_epoch = epoch + 1

                        net=net.eval()
                        torch.save(net.state_dict(),
                                   dir_checkpoint + f'CP_BestDice_epoch.pth')
                        torch.save(net, dir_checkpoint + f'CP_BestDice_epoch_Allnet.pth')
                        logging.info(f'Checkpoint BestDice{best_epoch} saved !')
                    else:
                        pass

                if global_step%(n_train // batch_size)==0:
                    val_totall_result.append(val_score)
                    train_totall_result.append([train_avg_meters['Dice'].avg,train_avg_meters['Iou'].avg,train_avg_meters['loss'].avg])
                    lr_list.append(optimizer.state_dict()['param_groups'][0]['lr'])

        if args.ES > 0 and (epoch - best_epoch) >= args.ES:
            logging.info('=========>Early stopping')
            break
    writer.close()
    save_train_config(args, n_train, n_val,args.ES,best_epoch)
    save_val_result(args.val_result_path, val_totall_result,lr_list)
    save_train_result(args.train_result_path, train_totall_result,lr_list)

def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-e', '--epochs', metavar='E', type=int, default=150,
                        help='Number of epochs', dest='epochs')
    parser.add_argument('-b', '--batch-size', metavar='B', type=int, nargs='?', default=2,
                        help='Batch size', dest='batchsize')
    parser.add_argument('-l', '--learning-rate', metavar='LR', type=float, nargs='?', default=0.0001,
                        help='Learning rate', dest='lr')
    parser.add_argument('-f', '--load', dest='load', type=str, default=False,
                        help='Load model from a .pth file')
    parser.add_argument('-s', '--scale', dest='scale', type=float, default=0.5,
                        help='Downscaling factor of the images')
    parser.add_argument('-v', '--validation', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('-E', '--early_stopping', dest='ES', type=int, default=20,
                        help='Stop training when the verification result has n epoches no longer improved，0 represent do not early_stop'
                             )
    parser.add_argument('-w', '--wrirte_config_path', type=str, default=r'D:\ACPA_Net\output\config.txt',
                        help='The path to write the config')
    parser.add_argument('--val_result_path', type=str, default=r'D:\ACPA_Net\output\result_val.txt',
                        help='The path to write the result')
    parser.add_argument('--train_result_path', type=str, default=r'D:\ACPA_Net\output\result_train.txt',
                        help='The path to write the result')

    parser.add_argument('-i', '--set_seed', dest='seed', type=int, default=888,
                        help='Chose wheather to set the seed')
    parser.add_argument('-L', '--Loss', dest='loss', type=str, choices=['BCE', 'W_BCE', 'CE', 'BFL', 'FL', 'DL','BCE+DL' ],
                        default='BCE+DL',
                        help='loos: FL:Focal loss  DL:Dice loss')
    #Deep_Supervision
    parser.add_argument('-D', '--Deep_Supervision', dest='DS', type=str,  default='Decoder', choices=['Decoder','Enconder', 'DAHead', 'All' ,'Enconder+Decoder'],
                        help='Decide Weather to use Deep_Supervisionv')
    parser.add_argument('-n', '--net', type=str, default='ACPA_Net',
                        choices=['ACPA_Net'],
                        help='The name of the model')
    parser.add_argument('-d'  , '--dataset', type=str, default='water',choices=['water','crack500'], help='The name of the dataset')
    parser.add_argument('-A', '--dataAug', type=bool, default=False,  help='yes/no data enhancement')
    parser.add_argument('-W', '--wave', type=bool, default=False, help='yes/no Wavelet Transform')
    return parser.parse_args()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = get_args()
    if args.seed:
        setup_seed(args.seed)
        print("随机种子设置为了",args.seed)

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    net=get_model(args)

    if args.load:
        net.load_state_dict(
            torch.load(args.load, map_location=device)
        )
        logging.info(f'Model loaded from {args.load}')
        net.eval()

    net.to(device=device)

    try:
        train_net(net=net,
                  epochs=args.epochs,
                  args=args,
                  batch_size=args.batchsize,
                  lr=args.lr,
                  device=device,
                  img_scale=args.scale,
                  val_percent=args.val/ 100)
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        logging.info('Saved interrupt')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


