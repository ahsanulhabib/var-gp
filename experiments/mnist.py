import os
from tqdm.auto import tqdm
import wandb
import torch
from torch.utils.data import DataLoader, ConcatDataset
from torch.utils.tensorboard import SummaryWriter

from continual_gp.datasets import SplitMNIST, PermutedMNIST
from continual_gp.train_utils import set_seeds, create_class_gp, compute_accuracy, EarlyStopper


def train(task_id, train_set, val_set, test_set, ep_var_mean=True,
          epochs=1, M=20, n_f=10, n_var_samples=3, batch_size=512, lr=1e-2, beta=1.0,
          eval_interval=10, patience=20, prev_params=None, logger=None, device=None):
  gp = create_class_gp(train_set, M=M, n_f=n_f, n_var_samples=n_var_samples,
                       ep_var_mean=ep_var_mean, prev_params=prev_params).to(device)

  stopper = EarlyStopper(patience=patience)

  optim = torch.optim.Adam(gp.parameters(), lr=lr)

  N = len(train_set)
  loader = DataLoader(train_set, batch_size=batch_size, shuffle=True)

  for e in tqdm(range(epochs)):
    for x, y in tqdm(loader, leave=False):
      optim.zero_grad()

      kl_hypers, kl_u, lik = gp.loss(x.to(device), y.to(device))

      loss = beta * kl_hypers + kl_u + (N / x.size(0)) * lik
      loss.backward()

      optim.step()

    if (e + 1) % eval_interval == 0:
      train_acc = compute_accuracy(train_set, gp, device=device)
      val_acc = compute_accuracy(val_set, gp, device=device)
      test_acc = compute_accuracy(test_set, gp, device=device)

      loss_summary = {
        f'task{task_id}/loss/kl_hypers': kl_hypers.detach().item(),
        f'task{task_id}/loss/kl_u': kl_u.detach().item(),
        f'task{task_id}/loss/lik': lik.detach().item()
      }

      acc_summary = {
        f'task{task_id}/train/acc': train_acc,
        f'task{task_id}/val/acc': val_acc,
        f'task{task_id}/test/acc': test_acc,
      }

      if logger is not None:
        for k, v in (dict(**loss_summary, **acc_summary)).items():
          logger.add_scalar(k, v, global_step=e + 1)

      stopper(val_acc, dict(state_dict=gp.state_dict(), acc_summary=acc_summary, step=e + 1))
      if stopper.is_done():
        break

  info = stopper.info()
  for k, v in info.get('acc_summary').items():
    logger.add_scalar(f'{k}_best', v, global_step=info.get('step'))

  # viz_ind_pts = info.get('state_dict').get('z')[2*task_id:2*task_id+2][:, torch.randperm(M)[:8], :].view(16, 1, 28, 28)
  # logger.add_images(f'task{task_id}/inducing', viz_ind_pts, global_step=e + 1)

  with open(f'{logger.log_dir}/ckpt{task_id}.pt', 'wb') as f:
    torch.save(info.get('state_dict'), f)
  wandb.save(f'{logger.log_dir}/ckpt{task_id}.pt')

  return info.get('state_dict')


def split_mnist(data_dir='/tmp', epochs=500, M=60, lr=3e-3,
                batch_size=512, beta=10.0, ep_var_mean=True, seed=42):
  set_seeds(seed)

  wandb.init(tensorboard=True)

  device = 'cuda' if torch.cuda.is_available() else 'cpu'
  logger = SummaryWriter(log_dir=wandb.run.dir)

  mnist_train = SplitMNIST(f'{data_dir}', train=True)
  mnist_val = SplitMNIST(f'{data_dir}', train=True)
  mnist_test = SplitMNIST(f'{data_dir}', train=False)

  idx = torch.randperm(len(mnist_train))
  train_idx, val_idx = idx[:-10000], idx[-10000:]
  mnist_train.filter_by_idx(train_idx)
  mnist_val.filter_by_idx(val_idx)

  prev_params = []
  for t in range(5):
    mnist_train.filter_by_class([2 * t, 2 * t + 1])
    mnist_val.filter_by_class(range(2 * t + 2))
    mnist_test.filter_by_class(range(2 * t + 2))
    
    state_dict = train(t, mnist_train, mnist_val, mnist_test,
                       epochs=epochs, M=M, lr=lr, beta=beta, batch_size=batch_size,
                       ep_var_mean=bool(ep_var_mean), prev_params=prev_params, logger=logger, device=device)

    prev_params.append(state_dict)

  logger.close()


def permuted_mnist(data_dir='/tmp', n_tasks=10, epochs=500, M=100, lr=9e-3,
                   batch_size=512, beta=2.0, ep_var_mean=True, seed=42):
  set_seeds(seed)

  wandb.init(tensorboard=True)

  device = 'cuda' if torch.cuda.is_available() else 'cpu'
  logger = SummaryWriter(log_dir=wandb.run.dir)

  ## NOTE: First task is unpermuted MNIST.
  tasks = [torch.arange(784)] + PermutedMNIST.create_tasks(n=n_tasks)

  mnist_train = PermutedMNIST(f'{data_dir}', train=True)

  idx = torch.randperm(len(mnist_train))
  train_idx, val_idx = idx[:-10000], idx[-10000:]
  mnist_train.filter_by_idx(train_idx)

  mnist_val = []
  mnist_test = []

  prev_params = []
  for t in range(len(tasks)):
    mnist_train = PermutedMNIST(f'{data_dir}', train=True)
    mnist_train.filter_by_idx(train_idx)
    mnist_train.set_task(tasks[t])

    mnist_val.append(PermutedMNIST(f'{data_dir}', train=True))
    mnist_val[-1].filter_by_idx(val_idx)
    mnist_val[-1].set_task(tasks[t])

    mnist_test.append(PermutedMNIST(f'{data_dir}', train=False))
    mnist_test[-1].set_task(tasks[t])

    state_dict = train(t, mnist_train, ConcatDataset(mnist_val), ConcatDataset(mnist_test),
                       epochs=epochs, M=M, lr=lr, beta=beta, batch_size=batch_size,
                       ep_var_mean=bool(ep_var_mean), prev_params=prev_params, logger=logger, device=device)

    prev_params.append(state_dict)

  logger.close()


if __name__ == "__main__":
  os.environ['WANDB_MODE'] = 'run' if os.environ.get('IS_UBUILD') else 'dryrun'

  import fire
  fire.Fire(dict(s_mnist=split_mnist, p_mnist=permuted_mnist))
