import argparse
import hashlib
import itertools
import math
import os
import warnings
from pathlib import Path
from typing import Optional

import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch.utils.data import Dataset

from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from diffusers import AutoencoderKL, DDPMScheduler, DiffusionPipeline, UNet2DConditionModel
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version
from diffusers.utils.import_utils import is_xformers_available
from huggingface_hub import HfFolder, Repository, whoami
from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from transformers import AutoTokenizer, PretrainedConfig

import streamlit as st
# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
check_min_version("0.10.0.dev0")

logger = get_logger(__name__)


class DreamTrainer:

    def __init__(self, 
                model = None,
                dataset=None, 
                ):

        self.args = self.parse_args()
        self.logging_dir = Path(self.args.output_dir, self.args.logging_dir)

        self.accelerator = Accelerator(
            gradient_accumulation_steps=self.args.gradient_accumulation_steps,
            mixed_precision=self.args.mixed_precision,
            log_with="tensorboard",
            logging_dir=logging_dir,
        )

        # Currently, it's not possible to do gradient accumulation when training two models with accelerate.accumulate
        # This will be enabled soon in accelerate. For now, we don't allow gradient accumulation when training two models.
        # TODO (patil-suraj): Remove this check when gradient accumulation with two models is enabled in accelerate.
        if self.args.train_text_encoder and self.args.gradient_accumulation_steps > 1 and self.accelerator.num_processes > 1:
            raise ValueError(
                "Gradient accumulation is not supported when training the text encoder in distributed training. "
                "Please set gradient_accumulation_steps to 1. This feature will be supported in the future."
            )

        if self.args.seed is not None:
            set_seed(self.args.seed)

        # Handle the repository creation
        if self.accelerator.is_main_process:
            if self.args.push_to_hub:
                if self.args.hub_model_id is None:
                    repo_name = get_full_repo_name(Path(self.args.output_dir).name, token=self.args.hub_token)
                else:
                    repo_name = self.args.hub_model_id
                repo = Repository(self.args.output_dir, clone_from=repo_name)

                with open(os.path.join(self.args.output_dir, ".gitignore"), "w+") as gitignore:
                    if "step_*" not in gitignore:
                        gitignore.write("step_*\n")
                    if "epoch_*" not in gitignore:
                        gitignore.write("epoch_*\n")
            elif self.args.output_dir is not None:
                os.makedirs(self.args.output_dir, exist_ok=True)

          
        # import correct text encoder class
        text_encoder_cls = import_model_class_from_model_name_or_path(self.args.pretrained_model_name_or_path, self.args.revision)

        # Load models and create wrapper for stable diffusion
        self.text_encoder = self.text_encoder_cls.from_pretrained(
            self.args.pretrained_model_name_or_path,
            subfolder="text_encoder",
            revision=self.args.revision,
        )
        self.vae = AutoencoderKL.from_pretrained(
            self.args.pretrained_model_name_or_path,
            subfolder="vae",
            revision=self.args.revision,
        )
        self.unet = UNet2DConditionModel.from_pretrained(
            self.args.pretrained_model_name_or_path,
            subfolder="unet",
            revision=self.args.revision,
        )


        self.tokenizer = AutoTokenizer.from_pretrained(
                        self.args.pretrained_model_name_or_path,
                        subfolder="tokenizer",
                        revision=self.args.revision,
                        use_fast=False,
                    )

        if self.args.enable_xformers_memory_efficient_attention:
            if is_xformers_available():
                unet.enable_xformers_memory_efficient_attention()
            else:
                raise ValueError("xformers is not available. Make sure it is installed correctly")

        self.vae.requires_grad_(False)
        if not self.args.train_text_encoder:
            self.text_encoder.requires_grad_(False)

        if self.args.gradient_checkpointing:
            self.unet.enable_gradient_checkpointing()
            if self.args.train_text_encoder:
                self.text_encoder.gradient_checkpointing_enable()

        if self.args.scale_lr:
            self.args.learning_rate = (
                self.args.learning_rate * self.args.gradient_accumulation_steps * self.args.train_batch_size * self.accelerator.num_processes
            )

        # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
        if self.args.use_8bit_adam:
            try:
                import bitsandbytes as bnb
            except ImportError:
                raise ImportError(
                    "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
                )

            optimizer_class = bnb.optim.AdamW8bit
        else:
            optimizer_class = torch.optim.AdamW

        params_to_optimize = (
            itertools.chain(self.unet.parameters(), self.text_encoder.parameters()) if self.args.train_text_encoder else self.unet.parameters()
        )
        self.optimizer = optimizer_class(
            params_to_optimize,
            lr=self.args.learning_rate,
            betas=(self.args.adam_beta1, self.args.adam_beta2),
            weight_decay=self.args.adam_weight_decay,
            eps=self.args.adam_epsilon,
        )

        self.noise_scheduler = DDPMScheduler.from_pretrained(self.args.pretrained_model_name_or_path, subfolder="scheduler")

        self.dataset = DreamBoothDataset(
            instance_data_root=self.args.instance_data_dir,
            instance_prompt=self.args.instance_prompt,
            class_data_root=self.args.class_data_dir,
            class_prompt=self.args.class_prompt,
            tokenizer=tokenizer,
            size=self.args.resolution,
            center_crop=self.args.center_crop,
        )

        # Scheduler and math around the number of training steps.
        overrode_max_train_steps = False
        num_update_steps_per_epoch = math.ceil(len(self.dataloader) / self.args.gradient_accumulation_steps)
        if self.args.max_train_steps is None:
            self.args.max_train_steps = self.args.num_train_epochs * num_update_steps_per_epoch
            overrode_max_train_steps = True

        self.lr_scheduler = get_scheduler(
            self.args.self.lr_scheduler,
            optimizer=optimizer,
            num_warmup_steps=self.args.lr_warmup_steps * self.args.gradient_accumulation_steps,
            num_training_steps=self.args.max_train_steps * self.args.gradient_accumulation_steps,
            num_cycles=self.args.lr_num_cycles,
            power=self.args.lr_power,
        )

        if self.args.train_text_encoder:
            self.unet, self.text_encoder, self.optimizer, self.dataloader, self.lr_scheduler = self.accelerator.prepare(
                self.unet, self.text_encoder, self.optimizer, self.dataloader, self.lr_scheduler
            )
        else:
            self.unet, self.optimizer, self.dataloader, self.lr_scheduler = self.accelerator.prepare(
                self.unet, self.optimizer, self.dataloader, self.lr_scheduler
            )
        self.accelerator.register_for_checkpointing(self.lr_scheduler)

        weight_dtype = torch.float32
        if self.accelerator.mixed_precision == "fp16":
            weight_dtype = torch.float16
        elif self.accelerator.mixed_precision == "bf16":
            weight_dtype = torch.bfloat16

        # Move text_encode and vae to gpu.
        # For mixed precision training we cast the self.text_encoder and vae weights to half-precision
        # as these models are only used for inference, keeping weights in full precision is not required.
        self.vae.to(self.accelerator.device, dtype=weight_dtype)
        if not self.args.train_text_encoder:
            self.text_encoder.to(self.accelerator.device, dtype=weight_dtype)

        # We need to recalculate our total training steps as the size of the training dataloader may have changed.
        num_update_steps_per_epoch = math.ceil(len(self.dataset) / self.args.gradient_accumulation_steps)
        if overrode_max_train_steps:
            self.args.max_train_steps = self.args.num_train_epochs * num_update_steps_per_epoch
        # Afterwards we recalculate our number of training epochs
        self.args.num_train_epochs = math.ceil(self.args.max_train_steps / num_update_steps_per_epoch)

        # We need to initialize the trackers we use, and also store our configuration.
        # The trackers initializes automatically on the main process.
        if self.accelerator.is_main_process:
            self.accelerator.init_trackers("dreambooth", config=vars(args))

        # Train!
        total_batch_size = self.args.train_batch_size * self.accelerator.num_processes * self.args.gradient_accumulation_steps

        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {len(self.dataset)}")
        logger.info(f"  Num batches each epoch = {len(self.dataloader)}")
        logger.info(f"  Num Epochs = {self.args.num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {self.args.train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {self.args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {self.args.max_train_steps}")
        global_step = 0
        first_epoch = 0

        if self.args.resume_from_checkpoint:
            if self.args.resume_from_checkpoint != "latest":
                path = os.path.basename(self.args.resume_from_checkpoint)
            else:
                # Get the mos recent checkpoint
                dirs = os.listdir(self.args.output_dir)
                dirs = [d for d in dirs if d.startswith("checkpoint")]
                dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
                path = dirs[-1]
            self.accelerator.print(f"Resuming from checkpoint {path}")
            self.accelerator.load_state(os.path.join(self.args.output_dir, path))
            global_step = int(path.split("-")[1])

            resume_global_step = global_step * self.args.gradient_accumulation_steps
            first_epoch = resume_global_step // num_update_steps_per_epoch
            resume_step = resume_global_step % num_update_steps_per_epoch

        # Only show the progress bar once on each machine.
        progress_bar = tqdm(range(global_step, self.args.max_train_steps), disable=not self.accelerator.is_local_main_process)
        progress_bar.set_description("Steps")


    @staticmethod
    def import_model_class_from_model_name_or_path(pretrained_model_name_or_path: str, revision: str):
        self.text_encoder_config = PretrainedConfig.from_pretrained(
            pretrained_model_name_or_path,
            subfolder="self.text_encoder",
            revision=revision,
        )
        model_class = self.text_encoder_config.architectures[0]

        if model_class == "CLIPTextModel":
            from transformers import CLIPTextModel

            return CLIPTextModel
        elif model_class == "RobertaSeriesModelWithTransformation":
            from diffusers.pipelines.alt_diffusion.modeling_roberta_series import RobertaSeriesModelWithTransformation

            return RobertaSeriesModelWithTransformation
        else:
            raise ValueError(f"{model_class} is not supported.")


    @staticmethod
    def parse_args(input_args=None):
        parser = argparse.ArgumentParser(description="Simple example of a training script.")
        parser.add_argument(
            "--pretrained_model_name_or_path",
            type=str,
            default='',
            required=True,
            help="Path to pretrained model or model identifier from huggingface.co/models.",
        )
        parser.add_argument(
            "--revision",
            type=str,
            default=None,
            required=False,
            help="Revision of pretrained model identifier from huggingface.co/models.",
        )
        parser.add_argument(
            "--tokenizer_name",
            type=str,
            default=None,
            help="Pretrained tokenizer name or path if not the same as model_name",
        )
        parser.add_argument(
            "--instance_data_dir",
            type=str,
            default=None,
            required=True,
            help="A folder containing the training data of instance images.",
        )
        parser.add_argument(
            "--class_data_dir",
            type=str,
            default=None,
            required=False,
            help="A folder containing the training data of class images.",
        )
        parser.add_argument(
            "--instance_prompt",
            type=str,
            default=None,
            required=True,
            help="The prompt with identifier specifying the instance",
        )
        parser.add_argument(
            "--class_prompt",
            type=str,
            default=None,
            help="The prompt to specify images in the same class as provided instance images.",
        )
        parser.add_argument(
            "--with_prior_preservation",
            default=False,
            action="store_true",
            help="Flag to add prior preservation loss.",
        )
        parser.add_argument("--prior_loss_weight", type=float, default=1.0, help="The weight of prior preservation loss.")
        parser.add_argument(
            "--num_class_images",
            type=int,
            default=100,
            help=(
                "Minimal class images for prior preservation loss. If there are not enough images already present in"
                " class_data_dir, additional images will be sampled with class_prompt."
            ),
        )
        parser.add_argument(
            "--output_dir",
            type=str,
            default="text-inversion-model",
            help="The output directory where the model predictions and checkpoints will be written.",
        )
        parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
        parser.add_argument(
            "--resolution",
            type=int,
            default=512,
            help=(
                "The resolution for input images, all the images in the train/validation dataset will be resized to this"
                " resolution"
            ),
        )
        parser.add_argument(
            "--center_crop", action="store_true", help="Whether to center crop images before resizing to resolution"
        )
        parser.add_argument("--train_text_encoder", action="store_true", help="Whether to train the text encoder")
        parser.add_argument(
            "--train_batch_size", type=int, default=4, help="Batch size (per device) for the training dataloader."
        )
        parser.add_argument(
            "--sample_batch_size", type=int, default=4, help="Batch size (per device) for sampling images."
        )
        parser.add_argument("--num_train_epochs", type=int, default=1)
        parser.add_argument(
            "--max_train_steps",
            type=int,
            default=None,
            help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
        )
        parser.add_argument(
            "--checkpointing_steps",
            type=int,
            default=500,
            help=(
                "Save a checkpoint of the training state every X updates. These checkpoints can be used both as final"
                " checkpoints in case they are better than the last checkpoint, and are also suitable for resuming"
                " training using `--resume_from_checkpoint`."
            ),
        )
        parser.add_argument(
            "--resume_from_checkpoint",
            type=str,
            default=None,
            help=(
                "Whether training should be resumed from a previous checkpoint. Use a path saved by"
                ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
            ),
        )
        parser.add_argument(
            "--gradient_accumulation_steps",
            type=int,
            default=1,
            help="Number of updates steps to accumulate before performing a backward/update pass.",
        )
        parser.add_argument(
            "--gradient_checkpointing",
            action="store_true",
            help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
        )
        parser.add_argument(
            "--learning_rate",
            type=float,
            default=5e-6,
            help="Initial learning rate (after the potential warmup period) to use.",
        )
        parser.add_argument(
            "--scale_lr",
            action="store_true",
            default=False,
            help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
        )
        parser.add_argument(
            "--self.lr_scheduler",
            type=str,
            default="constant",
            help=(
                'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
                ' "constant", "constant_with_warmup"]'
            ),
        )
        parser.add_argument(
            "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
        )
        parser.add_argument(
            "--lr_num_cycles",
            type=int,
            default=1,
            help="Number of hard resets of the lr in cosine_with_restarts scheduler.",
        )
        parser.add_argument("--lr_power", type=float, default=1.0, help="Power factor of the polynomial scheduler.")
        parser.add_argument(
            "--use_8bit_adam", action="store_true", help="Whether or not to use 8-bit Adam from bitsandbytes."
        )
        parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
        parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
        parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
        parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
        parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
        parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
        parser.add_argument("--hub_token", type=str, default=None, help="The token to use to push to the Model Hub.")
        parser.add_argument(
            "--hub_model_id",
            type=str,
            default=None,
            help="The name of the repository to keep in sync with the local `output_dir`.",
        )
        parser.add_argument(
            "--logging_dir",
            type=str,
            default="logs",
            help=(
                "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
                " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
            ),
        )
        parser.add_argument(
            "--mixed_precision",
            type=str,
            default=None,
            choices=["no", "fp16", "bf16"],
            help=(
                "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
                " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
                " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
            ),
        )
        parser.add_argument(
            "--prior_generation_precision",
            type=str,
            default=None,
            choices=["no", "fp32", "fp16", "bf16"],
            help=(
                "Choose prior generation precision between fp32, fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
                " 1.10.and an Nvidia Ampere GPU.  Default to  fp16 if a GPU is available else fp32."
            ),
        )
        parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
        parser.add_argument(
            "--enable_xformers_memory_efficient_attention", action="store_true", help="Whether or not to use xformers."
        )

        if input_args is not None:
            args = parser.parse_args(input_args)
        else:
            args = parser.parse_args()

        env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
        if env_local_rank != -1 and env_local_rank != self.args.local_rank:
            self.args.local_rank = env_local_rank
        return args


    @staticmethod
    def get_full_repo_name(model_id: str, organization: Optional[str] = None, token: Optional[str] = None):
        if token is None:
            token = HfFolder.get_token()
        if organization is None:
            username = whoami(token)["name"]
            return f"{username}/{model_id}"
        else:
            return f"{organization}/{model_id}"


    def train_epoch():
        for epoch in range(first_epoch, self.args.num_train_epochs):
            self.unet.train()
            if self.args.train_text_encoder:
                self.text_encoder.train()
            for step, batch in enumerate(self.dataloader):
                # Skip steps until we reach the resumed step
                if self.args.resume_from_checkpoint and epoch == first_epoch and step < resume_step:
                    if step % self.args.gradient_accumulation_steps == 0:
                        progress_bar.update(1)
                    continue

                with self.accelerator.accumulate(self.unet):
                    # Convert images to latent space
                    latents = self.vae.encode(batch["pixel_values"].to(dtype=weight_dtype)).latent_dist.sample()
                    latents = latents * 0.18215

                    # Sample noise that we'll add to the latents
                    noise = torch.randn_like(latents)
                    bsz = latents.shape[0]
                    # Sample a random timestep for each image
                    timesteps = torch.randint(0, self.noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
                    timesteps = timesteps.long()

                    # Add noise to the latents according to the noise magnitude at each timestep
                    # (this is the forward diffusion process)
                    noisy_latents = self.noise_scheduler.add_noise(latents, noise, timesteps)

                    # Get the text embedding for conditioning
                    encoder_hidden_states = self.text_encoder(batch["input_ids"])[0]

                    # Predict the noise residual
                    model_pred = self.unet(noisy_latents, timesteps, encoder_hidden_states).sample

                    # Get the target for loss depending on the prediction type
                    if self.noise_scheduler.config.prediction_type == "epsilon":
                        target = noise
                    elif self.noise_scheduler.config.prediction_type == "v_prediction":
                        target = self.noise_scheduler.get_velocity(latents, noise, timesteps)
                    else:
                        raise ValueError(f"Unknown prediction type {self.noise_scheduler.config.prediction_type}")

                    loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

                    self.accelerator.backward(loss)
                    if self.accelerator.sync_gradients:
                        params_to_clip = (
                            itertools.chain(self.unet.parameters(), self.text_encoder.parameters())
                            if self.args.train_text_encoder
                            else self.unet.parameters()
                        )
                        self.accelerator.clip_grad_norm_(params_to_clip, self.args.max_grad_norm)
                    optimizer.step()
                    self.lr_scheduler.step()
                    optimizer.zero_grad()

                # Checks if the self.accelerator has performed an optimization step behind the scenes
                if self.accelerator.sync_gradients:
                    progress_bar.update(1)
                    global_step += 1

                    if global_step % self.args.checkpointing_steps == 0:
                        if self.accelerator.is_main_process:
                            save_path = os.path.join(self.args.output_dir, f"checkpoint-{global_step}")
                            self.accelerator.save_state(save_path)
                            logger.info(f"Saved state to {save_path}")

                logs = {"loss": loss.detach().item(), "lr": self.lr_scheduler.get_last_lr()[0]}
                progress_bar.set_postfix(**logs)
                self.accelerator.log(logs, step=global_step)

                if global_step >= self.args.max_train_steps:
                    break

            self.accelerator.wait_for_everyone()

        # Create the pipeline using using the trained modules and save it.
        if self.accelerator.is_main_process:
            pipeline = DiffusionPipeline.from_pretrained(
                self.args.pretrained_model_name_or_path,
                unet=self.accelerator.unwrap_model(self.unet),
                text_encoder=self.accelerator.unwrap_model(self.text_encoder),
                revision=self.args.revision,
            )
            pipeline.save_pretrained(self.args.output_dir)

            if self.args.push_to_hub:
                repo.push_to_hub(commit_message="End of training", blocking=False, auto_lfs_prune=True)

        self.accelerator.end_training()

    @classmethod
    def demo(cls):

        self = cls()
        st.write()




if __name__ == "__main__":
    DreamTrainer.demo()

    
