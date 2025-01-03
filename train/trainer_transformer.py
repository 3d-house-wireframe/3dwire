import gc
import datetime
from pathlib import Path
from functools import partial
from contextlib import nullcontext
import torch
from torch.nn import Module
from torch.utils.data import Dataset, DataLoader
from torch.optim.lr_scheduler import LRScheduler
from pytorch_custom_utils import (
    get_adam_optimizer,
    add_wandb_tracker_contextmanager
)

from accelerate import Accelerator
from accelerate.utils import DistributedDataParallelKwargs
from beartype import beartype
from beartype.door import is_bearable
from beartype.typing import Optional, Tuple, Type, List

from train.dataset import custom_collate
from models.wf_transformer import WireframeTransformer
from train.optimizer_scheduler import OptimizerWithScheduler


# constants

DEFAULT_DDP_KWARGS = DistributedDataParallelKwargs(
    find_unused_parameters = True
)

from utils.helpers import (
    exists,
    get_current_time,
    cycle,
    get_lr,
    divisible_by,
)

from utils.utils import segment_coords_to_wireframe

from utils.utils import load_model


# line transformer trainer

@add_wandb_tracker_contextmanager()
class WireframeTransformerTrainer(Module):
    @beartype
    def __init__(
        self,
        model: WireframeTransformer,
        dataset: Dataset,
        num_train_steps: int = 100,
        batch_size: int = 1,
        num_workers: int = 8,
        grad_accum_every: int = 1,
        learning_rate: float = 2e-4,
        weight_decay: float = 0.,
        max_grad_norm: Optional[float] = 0.5,
        val_dataset: Optional[Dataset] = None,
        val_every_step = 1,
        val_num_batches = 5,
        scheduler: Optional[Type[LRScheduler]] = None,
        scheduler_kwargs: dict = dict(),
        accelerator_kwargs: dict = dict(),
        optimizer_kwargs: dict = dict(),
        checkpoint_every_setp = 1000,
        checkpoint_folder = './checkpoints_transformer',
        data_kwargs: Tuple[str, ...] = ['vertices', 'lines', 'line_edges'],
        log_every_step = 10,
        use_wandb_tracking = False,
        resume_training = False,
        checkpoint_file_name: str = '',
        num_step_per_epoch = 1000,
        from_start = False,
        vqvae_ckpt_path: str = '',
    ):
        super().__init__()

        # experiment tracker

        self.use_wandb_tracking = use_wandb_tracking

        if use_wandb_tracking:
            accelerator_kwargs['log_with'] = 'wandb'

        self.log_every_step = log_every_step


        if 'kwargs_handlers' not in accelerator_kwargs:
            accelerator_kwargs['kwargs_handlers'] = [DEFAULT_DDP_KWARGS]

        self.accelerator = Accelerator(**accelerator_kwargs)
        self.accelerator.print(f'device {str(self.accelerator.device)} is used!')

        if not resume_training:
            model.autoencoder = load_model(model.autoencoder, vqvae_ckpt_path, self.accelerator.device)

        self.model = model

        if self.is_main: self.print_params_num()

        optimizer = get_adam_optimizer(
            model.parameters(),
            lr = learning_rate,
            wd = weight_decay,
            filter_by_requires_grad = True, # filter ae model params
            **optimizer_kwargs
        )

        self.optimizer = OptimizerWithScheduler(
            accelerator = self.accelerator,
            optimizer = optimizer,
            scheduler = scheduler,
            scheduler_kwargs = scheduler_kwargs if len(scheduler_kwargs) > 0 else dict(num_train_steps = num_train_steps),
            max_grad_norm = max_grad_norm
        )

        self.dataloader = DataLoader(
            dataset,
            batch_size = batch_size,
            shuffle = True,
            num_workers = num_workers,
            drop_last = True,
            collate_fn = partial(custom_collate, pad_id = model.pad_id)
        )

        self.should_validate = exists(val_dataset)

        self.val_every_step = val_every_step
        if self.should_validate and self.is_main:
            assert len(val_dataset) > 0, 'your validation dataset is empty'

            self.val_num_batches = val_num_batches

            self.val_dataloader = DataLoader(
                val_dataset,
                batch_size = batch_size,
                shuffle = True,
                num_workers = num_workers,                
                drop_last = True,
                collate_fn = partial(custom_collate, pad_id = model.pad_id)
            )

        if hasattr(dataset, 'data_kwargs') and exists(dataset.data_kwargs):
            assert is_bearable(dataset.data_kwargs, List[str])
            self.data_kwargs = dataset.data_kwargs
        else:
            self.data_kwargs = data_kwargs

        (
            self.model,
            self.dataloader,
        ) = self.accelerator.prepare(
            self.model,
            self.dataloader,
        )
        
        self.grad_accum_every = grad_accum_every
        self.num_train_steps = num_train_steps
        self.register_buffer('step', torch.tensor(0))

        self.checkpoint_every_step = checkpoint_every_setp
        self.checkpoint_folder = Path(checkpoint_folder)
        self.checkpoint_folder.mkdir(exist_ok = True, parents = True)

        self.from_start = from_start

        if resume_training:
            print("loading checkpoint from the file: ", checkpoint_file_name)
            self.load(checkpoint_file_name, from_start=from_start)

        self.num_step_per_epoch = num_step_per_epoch // dataset.replica

    def log(self, **data_kwargs):
        self.accelerator.log(data_kwargs, step = self.step.item())

    @property
    def device(self):
        return self.unwrapped_model.device

    @property
    def is_main(self):
        return self.accelerator.is_main_process

    @property
    def unwrapped_model(self):
        return self.accelerator.unwrap_model(self.model)

    @property
    def is_local_main(self):
        return self.accelerator.is_local_main_process

    def wait(self):
        return self.accelerator.wait_for_everyone()

    def print(self, msg):
        return self.accelerator.print(msg)

    def next_data_to_forward_kwargs(self, dl_iter) -> dict:
        data = next(dl_iter)

        if isinstance(data, tuple):
            forward_kwargs = dict(zip(self.data_kwargs, data))

        elif isinstance(data, dict):
            forward_kwargs = data

        return forward_kwargs

    def print_params_num(self):
        total_params = sum(p.numel() for p in self.model.parameters())
        print(f"Model Total parameters: {total_params / 1e6} M")  

        non_trainable_params = sum(p.numel() for p in self.model.parameters() if not p.requires_grad)        
        print(f"Number of non-trainable parameters: {non_trainable_params/ 1e6}") 


    def save(self, milestone):

        pkg = dict(
            model = self.unwrapped_model.state_dict(),
            optimizer = self.optimizer.state_dict(),
            step = self.step.item(),
            scaler = self.accelerator.scaler.state_dict() if exists(self.accelerator.scaler) else None,
        )

        torch.save(pkg, str(self.checkpoint_folder / f'model-{milestone}.pt'))

    def load(self, file_name, from_start=False):

        pkg = torch.load(str(self.checkpoint_folder / file_name), map_location=self.accelerator.device)


        self.unwrapped_model.load_state_dict(pkg['model'])
        
        if not from_start:
            self.optimizer.load_state_dict(pkg['optimizer'])
            self.step.copy_(pkg['step'])
        
        print(f"loaded checkpoint from {self.checkpoint_folder / file_name}")

    def log_loss(self, loss, loss_dict, cur_lr=None, total_norm=None, step=None):
        log_data = {"total_loss": loss}
        
        if loss_dict is not None:
            log_data.update(loss_dict)
        
        if cur_lr is not None:
            log_data["cur_lr"] = cur_lr
        if total_norm is not None:
            log_data["total_norm"] = total_norm if exists(total_norm) else 0.0
        
        if not self.use_wandb_tracking:
            log_str = str(step) + " | " + " | ".join([f"{key}: {value.item() if torch.is_tensor(value) and value.dim() == 0 else value:.3f}" for key, value in log_data.items()])
            print(log_str)
        else:
            self.log(**log_data)

    def train(self):
        step = self.step.item()
        dl_iter = cycle(self.dataloader)

        is_multi_loss = False

        if self.should_validate and self.is_main:
            val_dl_iter = cycle(self.val_dataloader)

        while step < self.num_train_steps:

            for i in range(self.grad_accum_every):
                is_last = i == (self.grad_accum_every - 1)
                maybe_no_sync = partial(self.accelerator.no_sync, self.model) if not is_last else nullcontext

                forward_kwargs = self.next_data_to_forward_kwargs(dl_iter)

                with self.accelerator.autocast(), maybe_no_sync():
                    loss, loss_dict = self.model(**forward_kwargs)

                    self.accelerator.backward(loss / self.grad_accum_every)

            if self.is_main and divisible_by(step, self.log_every_step):
                cur_lr = get_lr(self.optimizer.optimizer)
                
                total_norm = self.optimizer.total_norm
                                
                self.log_loss(loss, loss_dict, cur_lr, total_norm, step)


            self.optimizer.step()
            self.optimizer.zero_grad()

            step += 1
            self.step.add_(1)

            self.wait()

            if self.is_main and self.should_validate and divisible_by(step, self.val_every_step):
                total_val_loss = 0.
                self.unwrapped_model.eval()

                num_val_batches = self.val_num_batches * self.grad_accum_every

                for _ in range(num_val_batches):
                    with self.accelerator.autocast(), torch.no_grad():

                        forward_kwargs = self.next_data_to_forward_kwargs(val_dl_iter)
                        forward_kwargs = {k: v.to(self.device) for k, v in forward_kwargs.items()} # 因为 val_dataloader 里面的数据是在 cpu 上的，所以要转到 gpu 上
                        
                        val_loss, loss_dict = self.unwrapped_model(**forward_kwargs)

                        total_val_loss += (val_loss / num_val_batches)

                current_time = get_current_time()      
                self.print(current_time + f' valid recon loss: {total_val_loss:.3f}')                

                self.log(val_loss = total_val_loss)


            self.wait()

            if self.is_main and (divisible_by(step, self.checkpoint_every_step) or step == self.num_train_steps - 1):
                checkpoint_num = step // self.checkpoint_every_step
                idx = str(checkpoint_num).zfill(2)
                self.save(milestone=idx)
                
                current_time = get_current_time()
                self.print(current_time + f' checkpoint saved at {self.checkpoint_folder / f"line-transformer.ckpt.{idx}.pt"}')

            self.wait()

        # Make sure that the wandb tracker finishes correctly
        self.accelerator.end_training()

        self.print('training complete')
    
    def forward(self, project: str, run: str | None = None, hps: dict | None = None):
        if self.is_main and self.use_wandb_tracking:
            print('using wandb tracking')
            
            with self.wandb_tracking(project=project, run=run, hps=hps):
                self.train()
        else:
            print('not using wandb tracking')
            
            self.train()    