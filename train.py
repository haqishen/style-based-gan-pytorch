import os
import argparse
import random
import math

from tqdm import tqdm
import numpy as np
from PIL import Image

import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.autograd import Variable, grad
from torch.utils.data import DataLoader
from torchvision import datasets, transforms, utils

from model import StyledGenerator, Discriminator


def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        par1[k].data.mul_(decay).add_(1 - decay, par2[k].data)


def sample_data(dataset, batch_size, image_size=4):
    transform = transforms.Compose(
        [
            transforms.Resize(image_size),
            transforms.CenterCrop(image_size),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )

    dataset.transform = transform
    loader = DataLoader(dataset, shuffle=True, batch_size=batch_size, num_workers=16)

    return loader


def adjust_lr(optimizer, lr):
    for group in optimizer.param_groups:
        mult = group.get('mult', 1)
        group['lr'] = lr * mult


def train(args, dataset, generator, discriminator):
    step = int(math.log2(args.init_size)) - 2
    resolution = 4 * 2 ** step
    loader = sample_data(
        dataset, args.batch.get(resolution, args.batch_size), resolution
    )
    data_loader = iter(loader)

    adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
    adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

    pbar = tqdm(range(args.iters))

    requires_grad(generator, False)
    requires_grad(discriminator, True)

    disc_loss_val = 0
    gen_loss_val = 0
    grad_loss_val = 0

    alpha = 0
    used_sample = 0

    for i in pbar:
        discriminator.zero_grad()

        alpha = min(1, 1 / args.phase * (used_sample + 1))

        if used_sample > args.phase * 2:
            step += 1

            if step > int(math.log2(args.max_size)) - 2:
                step = int(math.log2(args.max_size)) - 2

            else:
                alpha = 0
                used_sample = 0

            resolution = 4 * 2 ** step

            loader = sample_data(
                dataset, args.batch.get(resolution, max(1, args.batch_size // (2 ** (step - 1)))), resolution
            )
            data_loader = iter(loader)

            if not args.debug:
                torch.save(
                    {
                        'generator': generator.module.state_dict(),
                        'discriminator': discriminator.module.state_dict(),
                        'g_optimizer': g_optimizer.state_dict(),
                        'd_optimizer': d_optimizer.state_dict(),
                    },
                    f'checkpoint/trained_step-{step-1}.model',
                )

            adjust_lr(g_optimizer, args.lr.get(resolution, 0.001))
            adjust_lr(d_optimizer, args.lr.get(resolution, 0.001))

        try:
            real_image, label = next(data_loader)

        except (OSError, StopIteration):
            data_loader = iter(loader)
            real_image, label = next(data_loader)

        used_sample += real_image.shape[0]

        b_size = real_image.size(0)
        real_image = real_image.to(args.device)
        label = label.to(args.device)

        if args.loss == 'wgan-gp':
            real_predict = discriminator(real_image, step=step, alpha=alpha)
            real_predict = real_predict.mean() - 0.001 * (real_predict ** 2).mean()
            (-real_predict).backward()

        elif args.loss == 'r1':
            real_image.requires_grad = True
            real_predict = discriminator(real_image, step=step, alpha=alpha)
            real_predict = F.softplus(-real_predict).mean()
            real_predict.backward(retain_graph=True)

            grad_real = grad(
                outputs=real_predict.sum(), inputs=real_image, create_graph=True
            )[0]
            grad_penalty = (
                grad_real.view(grad_real.size(0), -1).norm(2, dim=1) ** 2
            ).mean()
            grad_penalty = 10 / 2 * grad_penalty
            grad_penalty.backward()
            grad_loss_val = grad_penalty.item()

        if args.mixing and random.random() < 0.9:
            gen_in11, gen_in12, gen_in21, gen_in22 = torch.randn(
                4, b_size, code_size, device=args.device
            ).chunk(4, 0)
            gen_in1 = [gen_in11.squeeze(0), gen_in12.squeeze(0)]
            gen_in2 = [gen_in21.squeeze(0), gen_in22.squeeze(0)]

        else:
            gen_in1, gen_in2 = torch.randn(2, b_size, code_size, device=args.device).chunk(
                2, 0
            )
            gen_in1 = gen_in1.squeeze(0)
            gen_in2 = gen_in2.squeeze(0)

        fake_image = generator(gen_in1, step=step, alpha=alpha)
        fake_predict = discriminator(fake_image, step=step, alpha=alpha)

        if args.loss == 'wgan-gp':
            fake_predict = fake_predict.mean()
            fake_predict.backward()

            eps = torch.rand(b_size, 1, 1, 1).to(args.device)
            x_hat = eps * real_image.data + (1 - eps) * fake_image.data
            x_hat.requires_grad = True
            hat_predict = discriminator(x_hat, step=step, alpha=alpha)
            grad_x_hat = grad(
                outputs=hat_predict.sum(), inputs=x_hat, create_graph=True
            )[0]
            grad_penalty = (
                (grad_x_hat.view(grad_x_hat.size(0), -1).norm(2, dim=1) - 1) ** 2
            ).mean()
            grad_penalty = 10 * grad_penalty
            grad_penalty.backward()
            grad_loss_val = grad_penalty.item()
            disc_loss_val = (real_predict - fake_predict).item()

        elif args.loss == 'r1':
            fake_predict = F.softplus(fake_predict).mean()
            fake_predict.backward()
            disc_loss_val = (real_predict + fake_predict).item()

        d_optimizer.step()

        if (i + 1) % n_critic == 0:
            generator.zero_grad()

            requires_grad(generator, True)
            requires_grad(discriminator, False)

            fake_image = generator(gen_in2, step=step, alpha=alpha)

            predict = discriminator(fake_image, step=step, alpha=alpha)

            if args.loss == 'wgan-gp':
                loss = -predict.mean()

            elif args.loss == 'r1':
                loss = F.softplus(-predict).mean()

            gen_loss_val = loss.item()

            loss.backward()
            g_optimizer.step()
            accumulate(g_running, generator.module)

            requires_grad(generator, False)
            requires_grad(discriminator, True)

        # generate sample images while training
        # if (i + 1) % 100 == 0:
        #     images = []

        #     gen_i, gen_j = args.gen_sample.get(resolution, (10, 5))

        #     with torch.no_grad():
        #         for _ in range(gen_i):
        #             images.append(
        #                 g_running(
        #                     torch.randn(gen_j, code_size).to(args.device), step=step, alpha=alpha
        #                 ).data.cpu()
        #             )

        #     utils.save_image(
        #         torch.cat(images, 0),
        #         f'sample/{str(i + 1).zfill(6)}.png',
        #         nrow=gen_i,
        #         normalize=True,
        #         range=(-1, 1),
        #     )

        if not args.debug and ((i + 1) % 10000 == 0):
            # torch.save(
            #     g_running.state_dict(), f'checkpoint/{str(i + 1).zfill(6)}.model'
            # )
            torch.save(
                {
                    'generator': generator.module.state_dict(),
                    'discriminator': discriminator.module.state_dict(),
                    'g_optimizer': g_optimizer.state_dict(),
                    'd_optimizer': d_optimizer.state_dict(),
                },
                f'checkpoint/{str(i + 1).zfill(6)}.model',
            )

        state_msg = (
            f'Size: {4 * 2 ** step}; G: {gen_loss_val:.3f}; D: {disc_loss_val:.3f};'
            f' Grad: {grad_loss_val:.3f}; Alpha: {alpha:.5f}'
        )

        pbar.set_description(state_msg)


if __name__ == '__main__':
    code_size = 512
    n_critic = 1

    parser = argparse.ArgumentParser(description='Progressive Growing of GANs')

    parser.add_argument(
        '--path', type=str,
        default=os.path.join(os.environ.get('DATA_DIR'), 'animeface-character-dataset/thumb'),
        help='path of specified dataset'
    )
    parser.add_argument(
        '--n_gpu', type=int, default=1, help='number of gpu used for training'
    )
    parser.add_argument(
        '--phase',
        type=int,
        default=320000,
        help='number of samples used for each training phases',
    )
    parser.add_argument('--iters', default=100000, type=int, help='total iterations')
    parser.add_argument('--batch-size', default=32, type=int, help='batch size of step 1')
    parser.add_argument('--from-iter', default=1, type=int, help='train from which step')
    parser.add_argument('--lr', default=0.001, type=float, help='learning rate')
    parser.add_argument('--sched', action='store_true', help='use lr scheduling')
    parser.add_argument('--init-size', default=8, type=int, help='initial image size')
    parser.add_argument('--max-size', default=256, type=int, help='max image size')
    parser.add_argument(
        '--mixing', action='store_true', help='use mixing regularization'
    )
    parser.add_argument(
        '--loss',
        type=str,
        default='wgan-gp',
        choices=['wgan-gp', 'r1'],
        help='class of gan loss',
    )
    parser.add_argument(
        '-d',
        '--data',
        default='folder',
        type=str,
        choices=['folder', 'lsun'],
        help=('Specify dataset. ' 'Currently Image Folder and LSUN is supported'),
    )
    parser.add_argument('--debug', action='store_true')

    args = parser.parse_args()
    args.device = 'cuda' if torch.cuda.is_available() else 'cpu'

    generator = nn.DataParallel(StyledGenerator(code_size)).to(args.device)
    discriminator = nn.DataParallel(Discriminator()).to(args.device)
    g_running = StyledGenerator(code_size).to(args.device)
    g_running.train(False)

    class_loss = nn.CrossEntropyLoss()

    g_optimizer = optim.Adam(
        generator.module.generator.parameters(), lr=args.lr, betas=(0.0, 0.99)
    )
    g_optimizer.add_param_group(
        {
            'params': generator.module.style.parameters(),
            'lr': args.lr * 0.01,
            'mult': 0.01,
        }
    )
    d_optimizer = optim.Adam(discriminator.parameters(), lr=args.lr, betas=(0.0, 0.99))

    # if args.from_iter > 1:
    #     tmp_file = f'checkpoint/trained_step-{args.from_step - 1}.model'
    #     if not os.path.exists(tmp_file):
    #         raise FileNotFoundError(f'model file {tmp_file} not exists!')
    #     with open(tmp_file, 'rb') as reader:
    #         M = torch.load(reader)

    # batch_size: 32 16 8  4   2   1
    # image_size: 8  16 32 64  128 256
    # step_iters: 2w 4w 8w 16w 32w 64w

    accumulate(g_running, generator.module, 0)

    if args.data == 'folder':
        dataset = datasets.ImageFolder(args.path)

    elif args.data == 'lsun':
        dataset = datasets.LSUNClass(args.path, target_transform=lambda x: 0)

    if args.sched:
        args.lr = {128: 0.0015, 256: 0.002, 512: 0.003, 1024: 0.003}
        args.batch = {4: 512, 8: 256, 16: 128, 32: 64, 64: 32, 128: 32, 256: 32}
    else:
        args.lr = {}
        args.batch = {}

    args.gen_sample = {512: (8, 4), 1024: (4, 2)}

    args.batch_default = 32
    train(args, dataset, generator, discriminator)
