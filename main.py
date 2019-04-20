from __future__ import division, print_function, absolute_import

import os
import pdb
import time
import random
import argparse

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm

from model import VqaEncoder, AnswerDecoder
from utils import GOATLogger, save_ckpt, compute_score
from data_loader import prepare_data
from arguments import get_args
from constants import *


def evaluate(val_loader, model, epoch, device, logger):
    for module in model:
        module.eval()

    cr_loss = nn.CrossEntropyLoss()

    batches = len(val_loader)
    for step, (v, q, a, mca, q_lens, a_lens, _, _) in enumerate(tqdm(val_loader, ascii=True)):

        v = v.to(device)
        q = q.to(device)
        a = a.to(device)
        mca = mca.to(device)

        batch_size = len(a)
        loss = 0

        joint_embed, mca_embed = model[0](v, q, mca, q_lens)

        decoder_input = torch.LongTensor([[SOS_TOKEN for _ in range(batch_size)]])
        decoder_input = decoder_input.to(device)

        decoder_hidden = joint_embed

        for t in range(a.size(1)):
            decoder_output, decoder_hidden = model[1](decoder_input,
                                                      decoder_hidden,
                                                      mca_embed)

            _, topi = decoder_output.topk(1)
            decoder_input = torch.LongTensor([[topi[i][0] for i in range(batch_size)]])
            decoder_input = decoder_input.to(device)

            loss += cr_loss(decoder_output, a)

        score = 1 #compute_score(logits, gt)

        logger.batch_info_eval(epoch, step, batches, loss.item(), score)

    score = logger.batch_info_eval(epoch, -1, batches)
    return score


def train(train_loader,
          model,
          optims,
          epoch,
          device,
          logger,
          moving_loss):

    for module in model:
        module.train()

    cr_loss = nn.CrossEntropyLoss(ignore_index=0)
    smooth_const = 0.1

    batches = len(train_loader)
    start = time.time()
    for step, (v, q, a, mca, q_lens, a_lens, _, _) in enumerate(train_loader):
        data_time = time.time() - start

        v = v.to(device)
        q = q.to(device)
        a = a.to(device)
        mca = mca.to(device)

        batch_size = len(a)
        loss = 0

        joint_embed, mca_embed = model[0](v, q, mca, q_lens)

        decoder_input = torch.LongTensor([[SOS_TOKEN for _ in range(batch_size)]])
        decoder_input = decoder_input.to(device)

        decoder_hidden = joint_embed

        for t in range(a.size(1)):
            decoder_output, decoder_hidden = model[1](decoder_input,
                                                      decoder_hidden,
                                                      mca_embed)

            _, topi = decoder_output.topk(1)
            decoder_input = torch.LongTensor([[topi[i][0] for i in range(batch_size)]])
            decoder_input = decoder_input.to(device)

            loss += cr_loss(decoder_output, a)

        for optim in optims:
            optim.zero_grad()

        loss.backward()

        for module in model:
            nn.utils.clip_grad_norm_(module.parameters(), 0.25)

        for optim in optims:
            optim.step()

        moving_loss = (loss.item() if epoch == 0 and step ==0 else
                        (1 - smooth_const) * moving_loss + smooth_const * loss.item())

        batch_time = time.time() - start
        score = 1 #compute_score(logits, a)
        logger.batch_info(epoch, step, batches, data_time, moving_loss, score, batch_time)
        start = time.time()

    return moving_loss


def main():

    parser = get_args()
    args, unparsed = parser.parse_known_args()
    if len(unparsed) != 0:
        raise NameError("Argument {} not recognized".format(unparsed))

    logger = GOATLogger(args.mode, args.save, args.log_freq)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cpu:
        device = torch.device('cpu')
    else:
        if not torch.cuda.is_available():
            raise RuntimeError("GPU unavailable.")

        args.devices = torch.cuda.device_count()
        args.batch_size *= args.devices
        torch.backends.cudnn.benchmark = True
        device = torch.device('cuda')
        torch.cuda.manual_seed(args.seed)

    # Get data
    train_loader, val_loader, vocab_size, num_answers = prepare_data(args)

    # Set up model

    vqa_enc = VqaEncoder(vocab_size, args.word_embed_dim, args.hidden_size, args.resnet_out)
    ans_dec = AnswerDecoder(args.hidden_size)

    model = [vqa_enc, ans_dec]

    for idx, module in enumerate(model):
        model[idx] = nn.DataParallel(module).to(device)

    logger.loginfo("Parameters: {:.3f}M".format(sum(sum(p.numel() for p in module.parameters())
                                                    for module in model) / 1e6))

    # Set up optimizer
    optims = [torch.optim.Adam(module.parameters(), 2e-4) for module in model]


    last_epoch = 0
    bscore = 0.0
    moving_loss = 0.0

    if args.resume:
        logger.loginfo("Initialized from ckpt: " + args.resume)
        ckpt = torch.load(args.resume, map_location=device)
        last_epoch = ckpt['epoch']
        for idx, module in enumerate(model):
            module.load_state_dict(ckpt['state_dict'])
            optims[idx].load_state_dict(ckpt['optim_state_dict'])

    if args.mode == 'eval':
        _ = evaluate(val_loader, model, last_epoch, device, logger)
        return

    # Train
    for epoch in range(last_epoch, args.epoch):
        moving_loss = train(train_loader, model, optims, epoch, device, logger, moving_loss)
        score = evaluate(val_loader, model, epoch, device, logger)
        #bscore = save_ckpt(score, bscore, epoch, model, optims, args.save, logger)

    logger.loginfo("Done")


if __name__ == '__main__':
    main()
