import copy
import os

import torch

from .module.stable_diffusion_pipeline_compiler import (CompilationConfig,
                                                        compile_unet)


def is_cuda_malloc_async():
    env_var = os.environ.get("PYTORCH_CUDA_ALLOC_CONF", "")
    return "backend:cudaMallocAsync" in env_var


def gen_stable_fast_config():
    config = CompilationConfig.Default()
    # xformers and triton are suggested for achieving best performance.
    # It might be slow for triton to generate, compile and fine-tune kernels.
    try:
        import xformers
        config.enable_xformers = True
    except ImportError:
        print('xformers not installed, skip')
    try:
        import triton
        config.enable_triton = True
    except ImportError:
        print('triton not installed, skip')

    if config.enable_triton and is_cuda_malloc_async():
        print('disable stable fast triton because of cudaMallocAsync')
        config.enable_triton = False

    # CUDA Graph is suggested for small batch sizes.
    # After capturing, the model only accepts one fixed image size.
    # If you want the model to be dynamic, don't enable it.
    config.enable_cuda_graph = True
    # config.enable_jit_freeze = False
    return config


class StableFastPatch:
    def __init__(self, model, config):
        self.model = model
        self.config = config
        self.stable_fast_model = None
        self.stash_diffusion_model = None
        self.device = None

    def __call__(self, model_function, params):
        input_x = params.get("input")
        timestep_ = params.get("timestep")
        c = params.get("c")
        
        # disable with accelerate for now
        if hasattr(model_function.__self__, "hf_device_map") and self.stable_fast_model is None:
            return model_function(input_x, timestep_, **c)

        if self.stable_fast_model is None:
            if self.config.enable_cuda_graph or self.config.enable_jit_freeze:
                self.stash_diffusion_model = copy.deepcopy(model_function.__self__.diffusion_model.to("cpu"))
                model_function.__self__.diffusion_model.to(self.device)
            self.stable_fast_model = compile_unet(model_function, self.config, input_x.device)

        return self.stable_fast_model(input_x, timestep_, **c)

    def to(self, device):
        if type(device) == torch.device:
            self.device = device
        # comfyui tell we should move to cpu. but we cannt do it with cuda graph and freeze now.
        if device == torch.device("cpu"):
            if self.config.enable_cuda_graph or self.config.enable_jit_freeze:
                del self.stable_fast_model 
                self.stable_fast_model = None
                if not self.stash_diffusion_model is None:
                    self.model.model.diffusion_model = self.stash_diffusion_model
                    self.stash_diffusion_model = None
                # disable cuda graph
                self.config.enable_cuda_graph = False
                self.config.enable_jit_freeze = False
        return self


class ApplyStableFastUnet:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"model": ("MODEL",), }}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply_stable_fast"

    CATEGORY = "loaders"

    def apply_stable_fast(self, model):
        config = gen_stable_fast_config()

        if config.memory_format is not None:
            model.model.to(memory_format=config.memory_format)

        patch = StableFastPatch(model, config)
        model_stable_fast = model.clone()
        model_stable_fast.set_model_unet_function_wrapper(patch)
        return (model_stable_fast,)
