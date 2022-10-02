# This file is part of stable-diffusion-webui (https://github.com/sd-webui/stable-diffusion-webui/).

# Copyright 2022 sd-webui team.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>. 
import argparse, os, sys, glob, re, requests, json, time

import cv2

from logger import logger, set_logger_verbosity, quiesce_logger
from perlin import perlinNoise
from functools import lru_cache
from dataclasses import dataclass, field
from frontend.frontend import draw_gradio_ui
from frontend.job_manager import JobManager, JobInfo
from frontend.image_metadata import ImageMetadata
from frontend.ui_functions import resize_image
import scripts.util.ModelRepo as ModelRepo
import scripts.util.Models as Models


parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--ckpt", type=str, default="models/ldm/stable-diffusion-v1/model.ckpt", help="path to checkpoint of model",)
parser.add_argument("--cli", type=str, help="don't launch web server, take Python function kwargs from this file.", default=None)
parser.add_argument("--config", type=str, default="configs/stable-diffusion/v1-inference.yaml", help="path to config which constructs model",)
parser.add_argument("--defaults", type=str, help="path to configuration file providing UI defaults, uses same format as cli parameter", default='configs/webui/webui.yaml')
parser.add_argument("--esrgan-cpu", action='store_true', help="run ESRGAN on cpu", default=False)
parser.add_argument("--esrgan-gpu", type=int, help="run ESRGAN on specific gpu (overrides --gpu)", default=0)
parser.add_argument("--extra-models-cpu", action='store_true', help="run extra models (GFGPAN/ESRGAN) on cpu", default=False)
parser.add_argument("--extra-models-gpu", action='store_true', help="run extra models (GFGPAN/ESRGAN) on gpu", default=False)
parser.add_argument("--gfpgan-cpu", action='store_true', help="run GFPGAN on cpu", default=False)
parser.add_argument("--gfpgan-dir", type=str, help="GFPGAN directory", default=('./src/gfpgan' if os.path.exists('./src/gfpgan') else './GFPGAN')) # i disagree with where you're putting it but since all guidefags are doing it this way, there you go
parser.add_argument("--gfpgan-gpu", type=int, help="run GFPGAN on specific gpu (overrides --gpu) ", default=0)
parser.add_argument("--gpu", type=int, help="choose which GPU to use if you have multiple", default=0)
parser.add_argument("--grid-format", type=str, help="png for lossless png files; jpg:quality for lossy jpeg; webp:quality for lossy webp, or webp:-compression for lossless webp", default="jpg:95")
parser.add_argument("--inbrowser", action='store_true', help="automatically launch the interface in a new tab on the default browser", default=False)
parser.add_argument("--ldsr-dir", type=str, help="LDSR directory", default=('./src/latent-diffusion' if os.path.exists('./src/latent-diffusion') else './LDSR'))
parser.add_argument("--n_rows", type=int, default=-1, help="rows in the grid; use -1 for autodetect and 0 for n_rows to be same as batch_size (default: -1)",)
parser.add_argument("--no-half", action='store_true', help="do not switch the model to 16-bit floats", default=False)
parser.add_argument("--no-progressbar-hiding", action='store_true', help="do not hide progressbar in gradio UI (we hide it because it slows down ML if you have hardware accleration in browser)", default=False)
parser.add_argument("--no-verify-input", action='store_true', help="do not verify input to check if it's too long", default=False)
parser.add_argument("--optimized-turbo", action='store_true', help="alternative optimization mode that does not save as much VRAM but runs siginificantly faster")
parser.add_argument("--optimized", action='store_true', help="load the model onto the device piecemeal instead of all at once to reduce VRAM usage at the cost of performance")
parser.add_argument("--outdir_scn2img", type=str, nargs="?", help="dir to write scn2img results to (overrides --outdir)", default=None)
parser.add_argument("--outdir_img2img", type=str, nargs="?", help="dir to write img2img results to (overrides --outdir)", default=None)
parser.add_argument("--outdir_imglab", type=str, nargs="?", help="dir to write imglab results to (overrides --outdir)", default=None)
parser.add_argument("--outdir_txt2img", type=str, nargs="?", help="dir to write txt2img results to (overrides --outdir)", default=None)
parser.add_argument("--outdir", type=str, nargs="?", help="dir to write results to", default=None)
parser.add_argument("--filename_format", type=str, nargs="?", help="filenames format", default=None)
parser.add_argument("--port", type=int, help="choose the port for the gradio webserver to use", default=7860)
parser.add_argument("--precision", type=str, help="evaluate at this precision", choices=["full", "autocast"], default="autocast")
parser.add_argument("--realesrgan-dir", type=str, help="RealESRGAN directory", default=('./src/realesrgan' if os.path.exists('./src/realesrgan') else './RealESRGAN'))
parser.add_argument("--realesrgan-model", type=str, help="Upscaling model for RealESRGAN", default=('RealESRGAN_x4plus'))
parser.add_argument("--save-metadata", action='store_true', help="Store generation parameters in the output png. Drop saved png into Image Lab to read parameters", default=False)
parser.add_argument("--share-password", type=str, help="Sharing is open by default, use this to set a password. Username: webui", default=None)
parser.add_argument("--share", action='store_true', help="Should share your server on gradio.app, this allows you to use the UI from your mobile app", default=False)
parser.add_argument("--skip-grid", action='store_true', help="do not save a grid, only individual samples. Helpful when evaluating lots of samples", default=False)
parser.add_argument("--skip-save", action='store_true', help="do not save indiviual samples. For speed measurements.", default=False)
parser.add_argument('--no-job-manager', action='store_true', help="Don't use the experimental job manager on top of gradio", default=False)
parser.add_argument("--max-jobs", type=int, help="Maximum number of concurrent 'generate' commands", default=1)
parser.add_argument("--tiling", action='store_true', help="Generate tiling images", default=False)
parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
parser.add_argument("--bridge", action='store_true', help="don't launch web server, but make this instance into a Horde bridge.", default=False)
parser.add_argument('--horde_api_key', action="store", required=False, type=str, help="The API key corresponding to the owner of this Horde instance")
parser.add_argument('--horde_name', action="store", required=False, type=str, help="The server name for the Horde. It will be shown to the world and there can be only one.")
parser.add_argument('--horde_url', action="store", required=False, type=str, help="The SH Horde URL. Where the bridge will pickup prompts and send the finished generations.")
parser.add_argument('--horde_priority_usernames',type=str, action='append', required=False, help="Usernames which get priority use in this horde instance. The owner's username is always in this list.")
parser.add_argument('--horde_max_power',type=int, required=False, help="How much power this instance has to generate pictures. Min: 2")
parser.add_argument('--horde_nsfw', action='store_true', required=False, help="Set to false if you do not want this worker generating NSFW images.")
opt = parser.parse_args()

#Should not be needed anymore
#os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
# all selected gpus, can probably be done nicer
#if opt.extra_models_gpu:
#    gpus = set([opt.gpu, opt.esrgan_gpu, opt.gfpgan_gpu])
#    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(g) for g in set(gpus))
#else:
#    os.environ["CUDA_VISIBLE_DEVICES"] = str(opt.gpu)

import gradio as gr
import k_diffusion as K
import math
import mimetypes
import numpy as np
import pynvml
import random
import threading, asyncio
import time
import torch
import torch.nn as nn
import yaml
import glob
import copy
from typing import List, Union, Dict, Callable, Any, Optional
from pathlib import Path
from collections import namedtuple
from functools import partial

# tell the user which GPU the code is actually using
if os.getenv("SD_WEBUI_DEBUG", 'False').lower() in ('true', '1', 'y'):
    gpu_in_use = opt.gpu
    # prioritize --esrgan-gpu and --gfpgan-gpu over --gpu, as stated in the option info
    if opt.esrgan_gpu != opt.gpu:
        gpu_in_use = opt.esrgan_gpu
    elif opt.gfpgan_gpu != opt.gpu:
        gpu_in_use = opt.gfpgan_gpu
    print("Starting on GPU {selected_gpu_name}".format(selected_gpu_name=torch.cuda.get_device_name(gpu_in_use)))

from contextlib import contextmanager, nullcontext, ExitStack
from einops import rearrange, repeat
from itertools import islice
from omegaconf import OmegaConf
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps, ImageChops
from io import BytesIO
import base64
import re
import logging
from torch import autocast
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.util import instantiate_from_config

# add global options to models
def patch_conv(**patch):
    cls = torch.nn.Conv2d
    init = cls.__init__
    def __init__(self, *args, **kwargs):
        return init(self, *args, **kwargs, **patch)
    cls.__init__ = __init__

if opt.tiling:
    patch_conv(padding_mode='circular')
    print("patched for tiling")


logging.basicConfig(stream=sys.stdout)
if opt.verbosity > 2:
    logging.getLogger().setLevel(logging.DEBUG)
elif opt.verbosity > 1:
    logging.getLogger().setLevel(logging.INFO)
elif opt.verbosity > 0:
    logging.getLogger().setLevel(logging.WARNING)
else:
    logging.getLogger().setLevel(logging.ERROR)

try:
    # this silences the annoying "Some weights of the model checkpoint were not used when initializing..." message at start.

    from transformers import logging as transformers_logging
    transformers_logging.set_verbosity_error()
except:
    pass

from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from transformers import AutoFeatureExtractor

# load safety model
safety_model_id = "CompVis/stable-diffusion-safety-checker"
safety_feature_extractor = None
safety_checker = None

# this is a fix for Windows users. Without it, javascript files will be served with text/html content-type and the bowser will not show any UI
mimetypes.init()
mimetypes.add_type('application/javascript', '.js')

# some of those options should not be changed at all because they would break the model, so I removed them from options.
opt_C = 4
opt_f = 8

LANCZOS = (Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
invalid_filename_chars = '<>:"/\|?*\n'

if opt.optimized_turbo:
    opt.optimized = True

if opt.no_job_manager:
    job_manager = None
else:
    job_manager = JobManager(opt.max_jobs)
    opt.max_jobs += 10 # Leave a free job open for button clicks

# should probably be moved to a settings menu in the UI at some point
grid_format = [s.lower() for s in opt.grid_format.split(':')]
grid_lossless = False
grid_quality = 100
if grid_format[0] == 'png':
    grid_ext = 'png'
    grid_format = 'png'
elif grid_format[0] in ['jpg', 'jpeg']:
    grid_quality = int(grid_format[1]) if len(grid_format) > 1 else 100
    grid_ext = 'jpg'
    grid_format = 'jpeg'
elif grid_format[0] == 'webp':
    grid_quality = int(grid_format[1]) if len(grid_format) > 1 else 100
    grid_ext = 'webp'
    grid_format = 'webp'
    if grid_quality < 0: # e.g. webp:-100 for lossless mode
        grid_lossless = True
        grid_quality = abs(grid_quality)


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def crash(e, s):
    global model
    global device

    print(s, '\n', e)
    try:
        del device
    except:
        try:
            del device
        except:
            pass
        pass

    print('exiting...calling os._exit(0)')
    t = threading.Timer(0.25, os._exit, args=[0])
    t.start()

class MemUsageMonitor(threading.Thread):
    stop_flag = False
    max_usage = 0
    total = -1

    def __init__(self, name):
        threading.Thread.__init__(self)
        self.name = name

    def run(self):
        try:
            pynvml.nvmlInit()
        except:
            print(f"[{self.name}] Unable to initialize NVIDIA management. No memory stats. \n")
            return
        print(f"[{self.name}] Recording max memory usage...\n")
        # check if we're using a scoped-down GPU environment (pynvml does not listen to CUDA_VISIBLE_DEVICES)
        # so that we can measure memory on the correct GPU
        try:
            isinstance(int(os.environ["CUDA_VISIBLE_DEVICES"]), int)
            handle = pynvml.nvmlDeviceGetHandleByIndex(int(os.environ["CUDA_VISIBLE_DEVICES"]))
        except (KeyError, ValueError) as pynvmlHandleError:
            if os.getenv("SD_WEBUI_DEBUG", 'False').lower() in ('true', '1', 'y'):
                print("[MemMon][WARNING]", pynvmlHandleError)
                print("[MemMon][INFO]", "defaulting to monitoring memory on the default gpu (set via --gpu flag)")
            handle = pynvml.nvmlDeviceGetHandleByIndex(opt.gpu)
        self.total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        while not self.stop_flag:
            m = pynvml.nvmlDeviceGetMemoryInfo(handle)
            self.max_usage = max(self.max_usage, m.used)
            # print(self.max_usage)
            time.sleep(0.1)
        print(f"[{self.name}] Stopped recording.\n")
        pynvml.nvmlShutdown()

    def read(self):
        return self.max_usage, self.total

    def stop(self):
        self.stop_flag = True

    def read_and_stop(self):
        self.stop_flag = True
        return self.max_usage, self.total

class CFGMaskedDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, x, sigma, uncond, cond, cond_scale, mask, x0, xi):
        x_in = x
        x_in = torch.cat([x_in] * 2)
        sigma_in = torch.cat([sigma] * 2)
        cond_in = torch.cat([uncond, cond])
        uncond, cond = self.inner_model(x_in, sigma_in, cond=cond_in).chunk(2)
        denoised = uncond + (cond - uncond) * cond_scale

        if mask is not None:
            assert x0 is not None
            img_orig = x0
            mask_inv = 1. - mask
            denoised = (img_orig * mask_inv) + (mask * denoised)

        return denoised

class CFGDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, x, sigma, uncond, cond, cond_scale):
        x_in = torch.cat([x] * 2)
        sigma_in = torch.cat([sigma] * 2)
        cond_in = torch.cat([uncond, cond])
        uncond, cond = self.inner_model(x_in, sigma_in, cond=cond_in).chunk(2)
        return uncond + (cond - uncond) * cond_scale


class KDiffusionSampler:
    def __init__(self, m, sampler):
        self.model = m
        self.model_wrap = K.external.CompVisDenoiser(m)
        self.schedule = sampler
    def get_sampler_name(self):
        return self.schedule
    def sample(self, S, conditioning, batch_size, shape, verbose, unconditional_guidance_scale, unconditional_conditioning, eta, x_T, img_callback: Callable = None ):
        sigmas = self.model_wrap.get_sigmas(S)
        x = x_T * sigmas[0]
        model_wrap_cfg = CFGDenoiser(self.model_wrap)

        samples_ddim = K.sampling.__dict__[f'sample_{self.schedule}'](model_wrap_cfg, x, sigmas, extra_args={'cond': conditioning, 'uncond': unconditional_conditioning, 'cond_scale': unconditional_guidance_scale}, disable=False, callback=partial(KDiffusionSampler.img_callback_wrapper, img_callback))

        return samples_ddim, None

    @classmethod
    def img_callback_wrapper(cls, callback: Callable, *args):
        ''' Converts a KDiffusion callback to the standard img_callback '''
        if callback:
            arg_dict = args[0]
            callback(image_sample=arg_dict['denoised'], iter_num=arg_dict['i'])

def create_random_tensors(shape, seeds):
    xs = []
    for seed in seeds:
        torch.manual_seed(seed)

        # randn results depend on device; gpu and cpu get different results for same seed;
        # the way I see it, it's better to do this on CPU, so that everyone gets same result;
        # but the original script had it like this so i do not dare change it for now because
        # it will break everyone's seeds.
        xs.append(torch.randn(shape, device=device))
    x = torch.stack(xs)
    return x

def torch_gc():
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()

device = torch.device(f"cuda:{opt.gpu}") if torch.cuda.is_available() else torch.device("cpu")

# Available model names (useful for autocompletion)
class ModelNames:
    RealESRGANx4Plus = "RealESRGAN_x4plus"
    RealESRGANx4Plus_Anime_6B = "RealESRGAN_x4plus_anime_6B"
    RealESRGANx2Plus = "RealESRGAN_x4plus"  # Uses x4 model
    RealESRGANx2Plus_Anime_6B = "RealESRGAN_x4plus_anime_6B"  # Uses x4 model
    LDSR = "LDSR"
    SD_opt_unet = "sd_unet"
    SD_opt_cs = "sd_cs"
    SD_opt_fs = "sd_fs"
    SD_full = "sd_full"
    GFPGAN = "GFPGAN"

    # Aliases for optimized mode
    SD_unet = "sd_full" if not opt.optimized else "sd_unet"
    SD_cs = "sd_full" if not opt.optimized else "sd_cs"
    SD_fs = "sd_full" if not opt.optimized else "sd_fs"

    # Alias for any RealESRGAN
    RealESRGAN = "RealESRGAN_x4plus"

if opt.extra_models_cpu:
    opt.esrgan_cpu = True
    opt.gfpgan_cpu = True
elif opt.extra_models_gpu:
    opt.esrgan_cpu = False
    opt.gfpgan_cpu = False

if opt.esrgan_cpu:
    esrgan_device = torch.device('cpu')
else:
    esrgan_device = torch.device(f"cuda:{opt.esrgan_gpu}")

if opt.gfpgan_cpu:
    gfpgan_device = torch.device('cpu')
else:
    gfpgan_device = torch.device(f"cuda:{opt.gfpgan_gpu}")

sd_device = torch.device(f"cuda:{opt.gpu}")

model_loaders: Dict[str, ModelRepo.ModelLoader] = {
    ModelNames.RealESRGANx4Plus:
        Models.RealESRGAN( esrgan_dir = opt.realesrgan_dir, model_name=opt.realesrgan_model, half_precision=not opt.no_half,
                           device=esrgan_device ),

    ModelNames.GFPGAN:
        Models.GFPGAN( gfpgan_dir = opt.gfpgan_dir, device=gfpgan_device),

    ModelNames.LDSR:
        Models.LDSR( ldsr_dir=opt.ldsr_dir ),

    ModelNames.SD_full:
        Models.SD( checkpoint=opt.ckpt, config_yaml=opt.config, half_precision=not opt.no_half, device=sd_device ),

    ModelNames.SD_opt_cs:
        Models.SD_Optimized( checkpoint=opt.ckpt, config_yaml=opt.config, stage=Models.SD_Optimized.Stage.COND_STAGE,
                             half_precision=not opt.no_half, device=sd_device),

    ModelNames.SD_opt_unet:
        Models.SD_Optimized( checkpoint=opt.ckpt, config_yaml=opt.config, stage=Models.SD_Optimized.Stage.UNET,
                             half_precision=not opt.no_half, max_depth = 0 if opt.optimized_turbo else 1,
                             device=sd_device),

    ModelNames.SD_opt_fs:
        Models.SD_Optimized( checkpoint=opt.ckpt, config_yaml=opt.config, stage=Models.SD_Optimized.Stage.FIRST_STAGE,
                             half_precision=not opt.no_half, device=sd_device)
}
# Register every model in model_loaders above
model_manager = ModelRepo.Manager(default_device=device)
for name, loader in model_loaders.items():
    model_manager.register_model_loader(name=name, loader=loader)



# Set the default esrgan model
esrgan_priority = [ModelNames.RealESRGANx4Plus, ModelNames.RealESRGANx4Plus_Anime_6B]
for name in esrgan_priority:
    if model_manager.is_loadable(name):
        ModelNames.RealESRGAN = name
        break


if model_manager.is_loadable(ModelNames.GFPGAN):
    print("Found GFPGAN")

if model_manager.is_loadable(ModelNames.RealESRGAN):
    print("Found RealESRGAN")

if model_manager.is_loadable(ModelNames.LDSR):
    print("Found LDSR")

try:
    with model_manager.model_context(ModelNames.SD_unet) as model:
        has_embeddings = model.embedding_manager is not None
except AttributeError as e:
    has_embeddings = False


def load_embeddings(model, fp):
    if fp is not None and has_embeddings:
        model.embedding_manager.load(fp.name)


def get_font(fontsize):
    fonts = ["arial.ttf", "DejaVuSans.ttf"]
    for font_name in fonts:
        try:
            return ImageFont.truetype(font_name, fontsize)
        except OSError:
           pass

    # ImageFont.load_default() is practically unusable as it only supports
    # latin1, so raise an exception instead if no usable font was found
    raise Exception(f"No usable font found (tried {', '.join(fonts)})")

def image_grid(imgs, batch_size, force_n_rows=None, captions=None):
    if force_n_rows is not None:
        rows = force_n_rows
    elif opt.n_rows > 0:
        rows = opt.n_rows
    elif opt.n_rows == 0:
        rows = batch_size
    else:
        rows = math.sqrt(len(imgs))
        rows = round(rows)

    cols = math.ceil(len(imgs) / rows)

    w, h = imgs[0].size
    grid = Image.new('RGB', size=(cols * w, rows * h), color='black')

    fnt = get_font(30)

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
        if captions and i<len(captions):
            d = ImageDraw.Draw( grid )
            size = d.textbbox( (0,0), captions[i], font=fnt, stroke_width=2, align="center" )
            d.multiline_text((i % cols * w + w/2, i // cols * h + h - size[3]), captions[i], font=fnt, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0), anchor="mm", align="center")

    return grid

def seed_to_int(s):
    if type(s) is int:
        return s
    if s is None or s == '':
        return random.randint(0, 2**32 - 1)
    n = abs(int(s) if s.isdigit() else random.Random(s).randint(0, 2**32 - 1))
    while n >= 2**32:
        n = n >> 32
    return n

def draw_prompt_matrix(im, width, height, all_prompts):
    def wrap(text, d, font, line_length):
        lines = ['']
        for word in text.split():
            line = f'{lines[-1]} {word}'.strip()
            if d.textlength(line, font=font) <= line_length:
                lines[-1] = line
            else:
                lines.append(word)
        return '\n'.join(lines)

    def draw_texts(pos, x, y, texts, sizes):
        for i, (text, size) in enumerate(zip(texts, sizes)):
            active = pos & (1 << i) != 0

            if not active:
                text = '\u0336'.join(text) + '\u0336'

            d.multiline_text((x, y + size[1] / 2), text, font=fnt, fill=color_active if active else color_inactive, anchor="mm", align="center")

            y += size[1] + line_spacing

    fontsize = (width + height) // 25
    line_spacing = fontsize // 2
    fnt = get_font(fontsize)
    color_active = (0, 0, 0)
    color_inactive = (153, 153, 153)

    pad_top = height // 4
    pad_left = width * 3 // 4 if len(all_prompts) > 2 else 0

    cols = im.width // width
    rows = im.height // height

    prompts = all_prompts[1:]

    result = Image.new("RGB", (im.width + pad_left, im.height + pad_top), "white")
    result.paste(im, (pad_left, pad_top))

    d = ImageDraw.Draw(result)

    boundary = math.ceil(len(prompts) / 2)
    prompts_horiz = [wrap(x, d, fnt, width) for x in prompts[:boundary]]
    prompts_vert = [wrap(x, d, fnt, pad_left) for x in prompts[boundary:]]

    sizes_hor = [(x[2] - x[0], x[3] - x[1]) for x in [d.multiline_textbbox((0, 0), x, font=fnt) for x in prompts_horiz]]
    sizes_ver = [(x[2] - x[0], x[3] - x[1]) for x in [d.multiline_textbbox((0, 0), x, font=fnt) for x in prompts_vert]]
    hor_text_height = sum([x[1] + line_spacing for x in sizes_hor]) - line_spacing
    ver_text_height = sum([x[1] + line_spacing for x in sizes_ver]) - line_spacing

    for col in range(cols):
        x = pad_left + width * col + width / 2
        y = pad_top / 2 - hor_text_height / 2

        draw_texts(col, x, y, prompts_horiz, sizes_hor)

    for row in range(rows):
        x = pad_left / 2
        y = pad_top + height * row + height / 2 - ver_text_height / 2

        draw_texts(row, x, y, prompts_vert, sizes_ver)

    return result




def check_prompt_length(prompt, comments):
    """this function tests if prompt is too long, and if so, adds a message to comments"""

    with model_manager.model_context(ModelNames.SD_cs) as model:
        tokenizer = model.cond_stage_model.tokenizer
        max_length = model.cond_stage_model.max_length
        info = tokenizer([prompt], truncation=True, max_length=max_length, return_overflowing_tokens=True,
                         padding="max_length", return_tensors="pt")

    ovf = info['overflowing_tokens'][0]
    overflowing_count = ovf.shape[0]
    if overflowing_count == 0:
        return

    vocab = {v: k for k, v in tokenizer.get_vocab().items()}
    overflowing_words = [vocab.get(int(x), "") for x in ovf]
    overflowing_text = tokenizer.convert_tokens_to_string(''.join(overflowing_words))

    comments.append(f"Warning: too many input tokens; some ({len(overflowing_words)}) have been truncated:\n{overflowing_text}\n")


def save_sample(image, sample_path_i, filename, jpg_sample, write_info_files, write_sample_info_to_log_file, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode, skip_metadata=False):
    ''' saves the image according to selected parameters. Expects to find generation parameters on image, set by ImageMetadata.set_on_image() '''
    metadata = ImageMetadata.get_from_image(image)
    if not skip_metadata and metadata is None:
        print("No metadata passed in to save. Set metadata on the image before calling save_sample using the ImageMetadata.set_on_image() function.")
        skip_metadata = True
    filename_i = os.path.join(sample_path_i, filename)
    if not jpg_sample:
        if opt.save_metadata and not skip_metadata:
            image.save(f"{filename_i}.png", pnginfo=metadata.as_png_info())
        else:
            image.save(f"{filename_i}.png")
    else:
        image.save(f"{filename_i}.jpg", 'jpeg', quality=100, optimize=True)
    if write_info_files or write_sample_info_to_log_file:
        # toggles differ for txt2img vs. img2img:
        offset = 0 if init_img is None else 2
        toggles = []
        if prompt_matrix:
            toggles.append(0)
        if metadata.normalize_prompt_weights:
            toggles.append(1)
        if init_img is not None:
            if uses_loopback:
                toggles.append(2)
            if uses_random_seed_loopback:
                toggles.append(3)
        if not skip_save:
            toggles.append(2 + offset)
        if not skip_grid:
            toggles.append(3 + offset)
        if sort_samples:
            toggles.append(4 + offset)
        if write_info_files:
            toggles.append(5 + offset)
        if write_sample_info_to_log_file:
            toggles.append(6+offset)
        if metadata.GFPGAN:
            toggles.append(7 + offset)

        info_dict = dict(
            target="txt2img" if init_img is None else "img2img",
            prompt=metadata.prompt, ddim_steps=metadata.steps, toggles=toggles, sampler_name=sampler_name,
            ddim_eta=ddim_eta, n_iter=n_iter, batch_size=batch_size, cfg_scale=metadata.cfg_scale,
            seed=metadata.seed, width=metadata.width, height=metadata.height
        )
        if init_img is not None:
            # Not yet any use for these, but they bloat up the files:
            #info_dict["init_img"] = init_img
            #info_dict["init_mask"] = init_mask
            info_dict["denoising_strength"] = denoising_strength
            info_dict["resize_mode"] = resize_mode
        if write_info_files:
            with open(f"{filename_i}.yaml", "w", encoding="utf8") as f:
                yaml.dump(info_dict, f, allow_unicode=True, width=10000)

        if write_sample_info_to_log_file:
            ignore_list = ["prompt", "target", "toggles", "ddim_eta", "batch_size"]
            rename_dict = {"ddim_steps": "steps", "n_iter": "number", "sampler_name": "sampler"} #changes the name of parameters to match with dynamic parameters
            sample_log_path = os.path.join(sample_path_i, "log.yaml")
            log_dump = info_dict.get("prompt") # making sure the first item that is listed in the txt is the prompt text
            for key, value in info_dict.items():
                if key in ignore_list:
                    continue
                found_key = rename_dict.get(key)

                if key == "cfg_scale": #adds zeros to to cfg_scale necessary for dynamic params
                    value = str(value).zfill(2)

                if found_key:
                    key = found_key
                log_dump += f" {key} {value}"

            log_dump = log_dump + " \n" #space at the end for dynamic params to accept the last param
            with open(sample_log_path, "a", encoding="utf8") as log_file:
                log_file.write(log_dump)



def get_next_sequence_number(path, prefix=''):
    """
    Determines and returns the next sequence number to use when saving an
    image in the specified directory.

    If a prefix is given, only consider files whose names start with that
    prefix, and strip the prefix from filenames before extracting their
    sequence number.

    The sequence starts at 0.
    """
    # Because when running in bridge-mode, we do not have a dir
    if opt.bridge: 
        return(0)
    result = -1
    for p in Path(path).iterdir():
        if p.name.endswith(('.png', '.jpg')) and p.name.startswith(prefix):
            tmp = p.name[len(prefix):]
            try:
                result = max(int(tmp.split('-')[0]), result)
            except ValueError:
                pass
    return result + 1


def oxlamon_matrix(prompt, seed, n_iter, batch_size):
    pattern = re.compile(r'(,\s){2,}')

    class PromptItem:
        def __init__(self, text, parts, item):
            self.text = text
            self.parts = parts
            if item:
                self.parts.append( item )

    def clean(txt):
        return re.sub(pattern, ', ', txt)

    def getrowcount( txt ):
        for data in re.finditer( ".*?\\((.*?)\\).*", txt ):
            if data:
                return len(data.group(1).split("|"))
            break
        return None

    def repliter( txt ):
        for data in re.finditer( ".*?\\((.*?)\\).*", txt ):
            if data:
                r = data.span(1)
                for item in data.group(1).split("|"):
                    yield (clean(txt[:r[0]-1] + item.strip() + txt[r[1]+1:]), item.strip())
            break

    def iterlist( items ):
        outitems = []
        for item in items:
            for newitem, newpart in repliter(item.text):
                outitems.append( PromptItem(newitem, item.parts.copy(), newpart) )

        return outitems

    def getmatrix( prompt ):
        dataitems = [ PromptItem( prompt[1:].strip(), [], None ) ]
        while True:
            newdataitems = iterlist( dataitems )
            if len( newdataitems ) == 0:
                return dataitems
            dataitems = newdataitems

    def classToArrays( items, seed, n_iter ):
        texts = []
        parts = []
        seeds = []

        for item in items:
            itemseed = seed
            for i in range(n_iter):
                texts.append( item.text )
                parts.append( f"Seed: {itemseed}\n" + "\n".join(item.parts) )
                seeds.append( itemseed )
                itemseed += 1

        return seeds, texts, parts

    all_seeds, all_prompts, prompt_matrix_parts = classToArrays(getmatrix( prompt ), seed, n_iter)
    n_iter = math.ceil(len(all_prompts) / batch_size)

    needrows = getrowcount(prompt)
    if needrows:
        xrows = math.sqrt(len(all_prompts))
        xrows = round(xrows)
        # if columns is to much
        cols = math.ceil(len(all_prompts) / xrows)
        if cols > needrows*4:
            needrows *= 2

    return all_seeds, n_iter, prompt_matrix_parts, all_prompts, needrows

def perform_masked_image_restoration(
        image: Image, init_img: Image, init_mask: Image, mask_blur_strength, mask_restore, esrgan_model):
    if not mask_restore:
        return image
    else:
        init_mask = init_mask.filter(ImageFilter.GaussianBlur(mask_blur_strength))
        init_mask = init_mask.convert('L')
        init_img = init_img.convert('RGB')
        image = image.convert('RGB')

        if esrgan_model is not None:
            output, img_mode = esrgan_model.enhance(np.array(init_mask, dtype=np.uint8))
            init_mask = Image.fromarray(output)
            init_mask = init_mask.convert('L')

            output, img_mode = esrgan_model.enhance(np.array(init_img, dtype=np.uint8))
            init_img = Image.fromarray(output)
            init_img = init_img.convert('RGB')

        image = Image.composite(init_img, image, init_mask)

        return image


def perform_color_correction(img_rgb, correction_target_lab):
    try:
        from skimage import exposure
    except:
        print("Install scikit-image to perform color correction")
        return img_rgb

    if correction_target_lab is None: return img_rgb

    return (
        Image.fromarray(cv2.cvtColor(exposure.match_histograms(
                cv2.cvtColor(
                    np.asarray(img_rgb),
                    cv2.COLOR_RGB2LAB
                ),
                correction_target_lab,
                channel_axis=2
            ), cv2.COLOR_LAB2RGB).astype("uint8")
        )
    )

def process_images(
        outpath, func_init, func_sample, prompt, seed, sampler_name, skip_grid, skip_save, batch_size,
        n_iter, steps, cfg_scale, width, height, prompt_matrix, filter_nsfw, use_GFPGAN, use_RealESRGAN, realesrgan_model_name,
        fp, ddim_eta=0.0, do_not_save_grid=False, normalize_prompt_weights=True, init_img=None, init_mask=None,
        keep_mask=False, mask_blur_strength=3, mask_restore=False, denoising_strength=0.75, resize_mode=None, uses_loopback=False,
        uses_random_seed_loopback=False, sort_samples=True, write_info_files=True, write_sample_info_to_log_file=False, jpg_sample=False,
        variant_amount=0.0, variant_seed=None,imgProcessorTask=False, job_info: JobInfo = None, do_color_correction=False, correction_target=None):
    """this is the main loop that both txt2img and img2img use; it calls func_init once inside all the scopes and func_sample once per batch"""

    def numpy_to_pil(images):
        """
        Convert a numpy image or a batch of images to a PIL image.
        """
        if images.ndim == 3:
            images = images[None, ...]
        images = (images * 255).round().astype("uint8")
        pil_images = [Image.fromarray(image) for image in images]

        return pil_images

    # load replacement of nsfw content
    def load_replacement(x):
        try:
            hwc = x.shape
            y = Image.open("images/nsfw.jpeg").convert("RGB").resize((hwc[1], hwc[0]))
            y = (np.array(y)/255.0).astype(x.dtype)
            assert y.shape == x.shape
            return y
        except Exception:
            return x

    # check and replace nsfw content
    def check_safety(x_image):
        global safety_feature_extractor, safety_checker
        if safety_feature_extractor is None:
            safety_feature_extractor = AutoFeatureExtractor.from_pretrained(safety_model_id)
            safety_checker = StableDiffusionSafetyChecker.from_pretrained(safety_model_id)
        safety_checker_input = safety_feature_extractor(numpy_to_pil(x_image), return_tensors="pt")
        x_checked_image, has_nsfw_concept = safety_checker(images=x_image, clip_input=safety_checker_input.pixel_values)
        for i in range(len(has_nsfw_concept)):
            if has_nsfw_concept[i]:
                x_checked_image[i] = load_replacement(x_checked_image[i])
        return x_checked_image, has_nsfw_concept

    prompt = prompt or ''
    torch_gc()
    # start time after garbage collection (or before?)
    start_time = time.time()

    mem_mon = MemUsageMonitor('MemMon')
    mem_mon.start()

    if has_embeddings:
        with model_manager.model_context(ModelNames.SD_unet) as model:
            load_embeddings(model, fp)

    if not opt.bridge:
        os.makedirs(outpath, exist_ok=True)

    sample_path = os.path.join(outpath, "samples")
    if not opt.bridge:
        os.makedirs(sample_path, exist_ok=True)

    if not ("|" in prompt) and prompt.startswith("@"):
        prompt = prompt[1:]

    negprompt = ''
    if '###' in prompt:
        prompt, negprompt = prompt.split('###', 1)
        prompt = prompt.strip()
        negprompt = negprompt.strip()

    comments = []

    prompt_matrix_parts = []
    simple_templating = False
    add_original_image = True
    if prompt_matrix:
        if prompt.startswith("@"):
            simple_templating = True
            add_original_image = not (use_RealESRGAN or use_GFPGAN)
            all_seeds, n_iter, prompt_matrix_parts, all_prompts, frows = oxlamon_matrix(prompt, seed, n_iter, batch_size)
        else:
            all_prompts = []
            prompt_matrix_parts = prompt.split("|")
            combination_count = 2 ** (len(prompt_matrix_parts) - 1)
            for combination_num in range(combination_count):
                current = prompt_matrix_parts[0]

                for n, text in enumerate(prompt_matrix_parts[1:]):
                    if combination_num & (2 ** n) > 0:
                        current += ("" if text.strip().startswith(",") else ", ") + text

                all_prompts.append(current)

            n_iter = math.ceil(len(all_prompts) / batch_size)
            all_seeds = len(all_prompts) * [seed]

        print(f"Prompt matrix will create {len(all_prompts)} images using a total of {n_iter} batches.")
    else:

        if not opt.no_verify_input:
            try:
                check_prompt_length(prompt, comments)
            except:
                import traceback
                print("Error verifying input:", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)

        all_prompts = batch_size * n_iter * [prompt]
        all_seeds = [seed + x for x in range(len(all_prompts))]
    original_seeds = all_seeds.copy()

    precision_scope = autocast if opt.precision == "autocast" else nullcontext
    if job_info:
        output_images = job_info.images
    else:
        output_images = []
    grid_captions = []
    stats = []
    with ExitStack() as stack:
        stack.enter_context( torch.no_grad() )
        stack.enter_context( precision_scope("cuda") )
        model = stack.enter_context( model_manager.model_context(ModelNames.SD_unet) )
        if not opt.optimized:
            stack.enter_context(model.ema_scope())

        init_data = func_init()
        tic = time.time()


        # if variant_amount > 0.0 create noise from base seed
        base_x = None
        if variant_amount > 0.0:
            target_seed_randomizer = seed_to_int('') # random seed
            torch.manual_seed(seed) # this has to be the single starting seed (not per-iteration)
            base_x = create_random_tensors([opt_C, height // opt_f, width // opt_f], seeds=[seed])
            # we don't want all_seeds to be sequential from starting seed with variants,
            # since that makes the same variants each time,
            # so we add target_seed_randomizer as a random offset
            for si in range(len(all_seeds)):
                all_seeds[si] += target_seed_randomizer

        for n in range(n_iter):
            if job_info and job_info.should_stop.is_set():
                print("Early exit requested")
                break

            print(f"Iteration: {n+1}/{n_iter}")
            prompts = all_prompts[n * batch_size:(n + 1) * batch_size]
            captions = prompt_matrix_parts[n * batch_size:(n + 1) * batch_size]
            seeds = all_seeds[n * batch_size:(n + 1) * batch_size]
            current_seeds = original_seeds[n * batch_size:(n + 1) * batch_size]

            if job_info:
                job_info.job_status = f"Processing Iteration {n+1}/{n_iter}. Batch size {batch_size}"
                job_info.rec_steps_imgs.clear()
                for idx,(p,s) in enumerate(zip(prompts,seeds)):
                    job_info.job_status += f"\nItem {idx}: Seed {s}\nPrompt: {p}"
                    print(f"Current prompt: {p}")

            with model_manager.model_context(ModelNames.SD_cs) as model_cs:
                uc = model_cs.get_learned_conditioning(len(prompts)*[negprompt])
                if isinstance(prompts, tuple):
                    prompts = list(prompts)

                # split the prompt if it has : for weighting
                # TODO for speed it might help to have this occur when all_prompts filled??
                weighted_subprompts = split_weighted_subprompts(prompts[0], normalize_prompt_weights)

                # sub-prompt weighting used if more than 1
                if len(weighted_subprompts) > 1:
                    c = torch.zeros_like(uc)  # i dont know if this is correct.. but it works
                    for i in range(0, len(weighted_subprompts)):
                        # note if alpha negative, it functions same as torch.sub
                        c = torch.add(c, model.get_learned_conditioning(
                            weighted_subprompts[i][0]), alpha=weighted_subprompts[i][1])
                else:  # just behave like usual
                    c = model_cs.get_learned_conditioning(prompts)

            shape = [opt_C, height // opt_f, width // opt_f]

            cur_variant_amount = variant_amount
            if variant_amount == 0.0:
                # we manually generate all input noises because each one should have a specific seed
                x = create_random_tensors(shape, seeds=seeds)
            else: # we are making variants
                # using variant_seed as sneaky toggle,
                # when not None or '' use the variant_seed
                # otherwise use seeds
                if variant_seed != None and variant_seed != '':
                    specified_variant_seed = seed_to_int(variant_seed)
                    torch.manual_seed(specified_variant_seed)
                    target_x = create_random_tensors(shape, seeds=[specified_variant_seed])
                    # with a variant seed we would end up with the same variant as the basic seed
                    # does not change. But we can increase the steps to get an interesting result
                    # that shows more and more deviation of the original image and let us adjust
                    # how far we will go (using 10 iterations with variation amount set to 0.02 will
                    # generate an icreasingly variated image which is very interesting for movies)
                    cur_variant_amount += n*variant_amount
                else:
                    target_x = create_random_tensors(shape, seeds=seeds)
                # finally, slerp base_x noise to target_x noise for creating a variant
                x = slerp(device, max(0.0, min(1.0, cur_variant_amount)), base_x, target_x)

            def sample_iteration_callback(image_sample: torch.Tensor, iter_num: int):
                ''' Called from the sampler every iteration '''
                if job_info:
                    job_info.active_iteration_cnt = iter_num
                    record_periodic_image = job_info.rec_steps_enabled and (0 == iter_num % job_info.rec_steps_intrvl)
                    if record_periodic_image or job_info.refresh_active_image_requested.is_set():
                        preview_start_time = time.time()

                        with model_manager.model_context(ModelNames.SD_fs) as model_fs:
                            decoded_batch: List[torch.Tensor] = []
                            # Break up batch to save VRAM
                            for sample in image_sample:
                                sample = sample[None, :]  # expands the tensor as if it still had a batch dimension
                                decoded_sample = model_fs.decode_first_stage(sample)[0]
                                decoded_sample = torch.clamp((decoded_sample + 1.0) / 2.0, min=0.0, max=1.0)
                                decoded_sample = decoded_sample.cpu()
                                decoded_batch.append(decoded_sample)
                        batch_size = len(decoded_batch)

                        images: List[Image.Image] = []
                        # Convert tensor to image (copied from code below)
                        for ddim in decoded_batch:
                            x_sample = 255. * rearrange(ddim.numpy(), 'c h w -> h w c')
                            x_sample = x_sample.astype(np.uint8)
                            image = Image.fromarray(x_sample)
                            images.append(image)

                        caption = f"Iter {iter_num}"
                        grid = image_grid(images, len(images), force_n_rows=1, captions=[caption]*len(images))

                        # Save the images if recording steps, and append existing saved steps
                        if job_info.rec_steps_enabled:
                            gallery_img_size = tuple(int(0.25*dim) for dim in images[0].size)
                            job_info.rec_steps_imgs.append(grid.resize(gallery_img_size))

                        # Notify the requester that the image is updated
                        if job_info.refresh_active_image_requested.is_set():
                            if job_info.rec_steps_enabled:
                                grid_rows = None if batch_size == 1 else len(job_info.rec_steps_imgs)
                                grid = image_grid(imgs=job_info.rec_steps_imgs[::-1], batch_size=1, force_n_rows=grid_rows)
                            job_info.active_image = grid
                            job_info.refresh_active_image_done.set()
                            job_info.refresh_active_image_requested.clear()

                        preview_elapsed_timed = time.time() - preview_start_time
                        if preview_elapsed_timed / job_info.rec_steps_intrvl > 1:
                            print(
                                f"Warning: Preview generation is slowing image generation. It took {preview_elapsed_timed:.2f}s to generate progress images for batch of {batch_size} images!")

                    # Interrupt current iteration?
                    if job_info.stop_cur_iter.is_set():
                        job_info.stop_cur_iter.clear()
                        raise StopIteration()

            try:
                samples_ddim = func_sample(init_data=init_data, x=x, conditioning=c, unconditional_conditioning=uc, sampler_name=sampler_name, img_callback=sample_iteration_callback)
            except StopIteration:
                print("Skipping iteration")
                job_info.job_status = "Skipping iteration"
                continue

            for i in range(len(samples_ddim)):
                with model_manager.model_context(ModelNames.SD_fs) as model_fs:
                    x_samples_ddim = model_fs.decode_first_stage(samples_ddim[i].unsqueeze(0))
                    x_sample = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

                if filter_nsfw:
                    x_samples_ddim_numpy = x_sample.cpu().permute(0, 2, 3, 1).numpy()
                    x_checked_image, has_nsfw_concept = check_safety(x_samples_ddim_numpy)
                    x_sample = torch.from_numpy(x_checked_image).permute(0, 3, 1, 2)

                sanitized_prompt = prompts[i].replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})
                if variant_seed != None and variant_seed != '':
                    if variant_amount == 0.0:
                        seed_used = f"{current_seeds[i]}-{variant_seed}"
                    else:
                        seed_used = f"{seed}-{variant_seed}"
                else:
                   seed_used = f"{current_seeds[i]}"
                if sort_samples:
                    sanitized_prompt = sanitized_prompt[:128] #200 is too long
                    sample_path_i = os.path.join(sample_path, sanitized_prompt)
                    if not opt.bridge:
                        os.makedirs(sample_path_i, exist_ok=True)
                    base_count = get_next_sequence_number(sample_path_i)
                    filename = opt.filename_format or "[STEPS]_[SAMPLER]_[SEED]_[VARIANT_AMOUNT]"
                else:
                    sample_path_i = sample_path
                    base_count = get_next_sequence_number(sample_path_i)
                    filename = opt.filename_format or "[STEPS]_[SAMPLER]_[SEED]_[VARIANT_AMOUNT]_[PROMPT]"

                #Add new filenames tags here
                filename = f"{base_count:05}-" + filename
                filename = filename.replace("[STEPS]", str(steps))
                filename = filename.replace("[CFG]", str(cfg_scale))
                filename = filename.replace("[PROMPT]", sanitized_prompt[:128])
                filename = filename.replace("[PROMPT_SPACES]", prompts[i].translate({ord(x): '' for x in invalid_filename_chars})[:128])
                filename = filename.replace("[WIDTH]", str(width))
                filename = filename.replace("[HEIGHT]", str(height))
                filename = filename.replace("[SAMPLER]", sampler_name)
                filename = filename.replace("[SEED]", seed_used)
                filename = filename.replace("[VARIANT_AMOUNT]", f"{cur_variant_amount:.2f}")

                x_sample = 255. * rearrange(x_sample[0].cpu().numpy(), 'c h w -> h w c')
                x_sample = x_sample.astype(np.uint8)
                metadata = ImageMetadata(prompt=prompts[i], seed=seeds[i], height=height, width=width, steps=steps,
                                    cfg_scale=cfg_scale, normalize_prompt_weights=normalize_prompt_weights, denoising_strength=denoising_strength,
                                    GFPGAN=use_GFPGAN )
                image = Image.fromarray(x_sample)

                if do_color_correction:
                    image = perform_color_correction(image, correction_target)
                ImageMetadata.set_on_image(image, metadata)

                processed_samples = x_sample
                original_filename = filename
                processed_metadata: ImageMetadata = copy.copy(metadata)

                if use_RealESRGAN:
                    with model_manager.model_context(ModelNames.RealESRGAN) as realesrgan_model:
                        processed_samples, img_mode = realesrgan_model.enhance(processed_samples)
                    processed_samples = processed_samples[:, :, ::-1]
                    filename += "-esrgan4x"  # TODO: Model name


                if use_GFPGAN:
                    with model_manager.model_context(ModelNames.GFPGAN) as gfpgan_model:
                        cropped_faces, fixed_faces, processed_samples = gfpgan_model.enhance(
                            processed_samples, has_aligned=False, only_center_face=False, paste_back=True)
                    processed_samples = processed_samples[:, :, ::-1]
                    filename += "-gfpgan"
                    processed_metadata.GFPGAN = True


                processed_image = Image.fromarray(processed_samples)
                if do_color_correction and correction_target is not None:
                    processed_image = perform_color_correction(processed_image, correction_target)

                processed_image = perform_masked_image_restoration(
                    processed_image, init_img, init_mask, mask_blur_strength, mask_restore, None)

                ImageMetadata.set_on_image(processed_image, processed_metadata)

                # if use_RealESRGAN and RealESRGAN is not None and not use_GFPGAN:
                #     skip_save = True  # 287 >_>
                #     torch_gc()
                #     output, img_mode = RealESRGAN.enhance(original_sample[:, :, ::-1])
                #     esrgan_filename = original_filename + '-esrgan4x'
                #     esrgan_sample = output[:, :, ::-1]
                #     esrgan_image = Image.fromarray(esrgan_sample)
                #     esrgan_image =
                #     esrgan_image = perform_masked_image_restoration(
                #         esrgan_image, init_img, init_mask,
                #         mask_blur_strength, mask_restore,
                #         use_RealESRGAN, RealESRGAN
                #     )
                #     ImageMetadata.set_on_image(esrgan_image, metadata)
                #     save_sample(esrgan_image, sample_path_i, esrgan_filename, jpg_sample, write_info_files,
                #                 write_sample_info_to_log_file, prompt_matrix, init_img, uses_loopback,
                #                 uses_random_seed_loopback, skip_save, skip_grid, sort_samples, sampler_name, ddim_eta,
                #                 n_iter, batch_size, i, denoising_strength, resize_mode, skip_metadata=False)
                #     output_images.append(esrgan_image)  # 287
                #     # if simple_templating:
                #     #    grid_captions.append( captions[i] + "\nesrgan" )

                # if use_RealESRGAN and RealESRGAN is not None and use_GFPGAN and GFPGAN is not None:
                #     skip_save = True  # 287 >_>
                #     torch_gc()
                #     cropped_faces, restored_faces, restored_img = GFPGAN.enhance(
                #         x_sample[:, :, ::-1], has_aligned=False, only_center_face=False, paste_back=True)
                #     gfpgan_sample = restored_img[:, :, ::-1]
                #     output, img_mode = RealESRGAN.enhance(gfpgan_sample[:, :, ::-1])
                #     gfpgan_esrgan_filename = original_filename + '-gfpgan-esrgan4x'
                #     gfpgan_esrgan_sample = output[:, :, ::-1]
                #     gfpgan_esrgan_image = Image.fromarray(gfpgan_esrgan_sample)
                #     gfpgan_esrgan_image = perform_color_correction(
                #         gfpgan_esrgan_image, correction_target, do_color_correction)
                #     gfpgan_esrgan_image = perform_masked_image_restoration(
                #         gfpgan_esrgan_image, init_img, init_mask,
                #         mask_blur_strength, mask_restore,
                #         use_RealESRGAN, RealESRGAN
                #     )
                #     ImageMetadata.set_on_image(gfpgan_esrgan_image, metadata)
                #     save_sample(
                #         gfpgan_esrgan_image, sample_path_i, gfpgan_esrgan_filename, jpg_sample, write_info_files,
                #         write_sample_info_to_log_file, prompt_matrix, init_img, uses_loopback,
                #         uses_random_seed_loopback, skip_save, skip_grid, sort_samples, sampler_name, ddim_eta, n_iter,
                #         batch_size, i, denoising_strength, resize_mode, skip_metadata=False)
                #     output_images.append(gfpgan_esrgan_image)  # 287
                #     # if simple_templating:
                #     #    grid_captions.append( captions[i] + "\ngfpgan_esrgan" )

                # image = perform_masked_image_restoration(
                #     image, init_img, init_mask,
                #     mask_blur_strength, mask_restore,
                #     # RealESRGAN image already processed in if-case above.
                #     use_RealESRGAN=False, RealESRGAN=None
                # )

                if not skip_save:
                    save_sample(processed_image, sample_path_i, filename, jpg_sample, write_info_files,
                                write_sample_info_to_log_file, prompt_matrix, init_img, uses_loopback,
                                uses_random_seed_loopback, skip_save, skip_grid, sort_samples, sampler_name, ddim_eta,
                                n_iter, batch_size, i, denoising_strength, resize_mode, False)
                if add_original_image or not simple_templating:
                    output_images.append(processed_image)
                    if simple_templating:
                        grid_captions.append( captions[i] )

            # Save the progress images?
            if job_info:
                if job_info.rec_steps_enabled and (job_info.rec_steps_to_file or job_info.rec_steps_to_gallery):
                    steps_grid = image_grid(job_info.rec_steps_imgs, 1)
                    if job_info.rec_steps_to_gallery:
                        gallery_img_size = tuple(2*dim for dim in image.size)
                        output_images.append( steps_grid.resize( gallery_img_size ) )
                    if job_info.rec_steps_to_file:
                        steps_grid_filename = f"{original_filename}_step_grid"
                        save_sample(steps_grid, sample_path_i, steps_grid_filename, jpg_sample, write_info_files, write_sample_info_to_log_file, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
                                    skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode, False)

        if (prompt_matrix or not skip_grid) and not do_not_save_grid:
            grid = None
            if prompt_matrix:
                if simple_templating:
                    grid = image_grid(output_images, batch_size, force_n_rows=frows, captions=grid_captions)
                else:
                    grid = image_grid(output_images, batch_size, force_n_rows=1 << ((len(prompt_matrix_parts)-1)//2))
                    try:
                        grid = draw_prompt_matrix(grid, width, height, prompt_matrix_parts)
                    except:
                        import traceback
                        print("Error creating prompt_matrix text:", file=sys.stderr)
                        print(traceback.format_exc(), file=sys.stderr)
            elif len(output_images) > 0 and (batch_size > 1  or n_iter > 1):
                grid = image_grid(output_images, batch_size)
            if grid is not None:
                grid_count = get_next_sequence_number(outpath, 'grid-')
                grid_file = f"grid-{grid_count:05}-{seed}_{prompts[i].replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})[:128]}.{grid_ext}"
                grid.save(os.path.join(outpath, grid_file), grid_format, quality=grid_quality, lossless=grid_lossless, optimize=True)
                if prompt_matrix:
                    output_images.append(grid)

        toc = time.time()

    mem_max_used, mem_total = mem_mon.read_and_stop()
    time_diff = time.time()-start_time
    args_and_names = {
        "seed": seed,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler": sampler_name,
    }

    full_string = f"{prompt}\n"+ " ".join([f"{k}:" for k,v in args_and_names.items()])
    info = {
        'text': full_string,
        'entities': [{'entity':str(v), 'start': full_string.find(f"{k}:"),'end': full_string.find(f"{k}:") + len(f"{k} ")} for k,v in args_and_names.items()]
     }
#     info = f"""
# {prompt} --seed {seed} --W {width} --H {height}  -s {steps} -C {cfg_scale} --sampler {sampler_name}  {', Denoising strength: '+str(denoising_strength) if init_img is not None else ''}{', GFPGAN' if use_GFPGAN and GFPGAN is not None else ''}{', '+realesrgan_model_name if use_RealESRGAN and RealESRGAN is not None else ''}{', Prompt Matrix Mode.' if prompt_matrix else ''}""".strip()
    stats = f'''
Took { round(time_diff, 2) }s total ({ round(time_diff/(len(all_prompts)),2) }s per image)
Peak memory usage: { -(mem_max_used // -1_048_576) } MiB / { -(mem_total // -1_048_576) } MiB / { round(mem_max_used/mem_total*100, 3) }%'''

    for comment in comments:
        info['text'] += "\n\n" + comment

    #mem_mon.stop()
    #del mem_mon
    torch_gc()

    return output_images, seed, info, stats


def txt2img(
        prompt: str, 
        ddim_steps: int = 50, 
        sampler_name: str = 'k_lms', 
        toggles: List[int] = [1, 4], 
        realesrgan_model_name: str = '',
        ddim_eta: float = 0.0, 
        n_iter: int = 1, 
        batch_size: int = 1, 
        cfg_scale: float = 5.0, 
        seed: Union[int, str, None] = None,
        height: int = 512, 
        width: int = 512, 
        fp = None, 
        variant_amount: float = 0.0, 
        variant_seed: int = None, 
        job_info: JobInfo = None):
    outpath = opt.outdir_txt2img or opt.outdir or "outputs/txt2img-samples"
    err = False
    seed = seed_to_int(seed)
    prompt_matrix = 0 in toggles
    normalize_prompt_weights = 1 in toggles
    skip_save = 2 not in toggles
    skip_grid = 3 not in toggles
    sort_samples = 4 in toggles
    write_info_files = 5 in toggles
    write_to_one_file = 6 in toggles
    jpg_sample = 7 in toggles
    filter_nsfw = 8 in toggles
    use_GFPGAN = 9 in toggles
    use_RealESRGAN = 10 in toggles

    do_color_correction = False
    correction_target = None

    with model_manager.model_context(ModelNames.SD_unet) as model_unet:
        if sampler_name == 'PLMS':
            sampler = PLMSSampler(model_unet)
        elif sampler_name == 'DDIM':
            sampler = DDIMSampler(model_unet)
        elif sampler_name == 'k_dpm_2_a':
            sampler = KDiffusionSampler(model_unet, 'dpm_2_ancestral')
        elif sampler_name == 'k_dpm_2':
            sampler = KDiffusionSampler(model_unet, 'dpm_2')
        elif sampler_name == 'k_euler_a':
            sampler = KDiffusionSampler(model_unet, 'euler_ancestral')
        elif sampler_name == 'k_euler':
            sampler = KDiffusionSampler(model_unet, 'euler')
        elif sampler_name == 'k_heun':
            sampler = KDiffusionSampler(model_unet, 'heun')
        elif sampler_name == 'k_lms':
            sampler = KDiffusionSampler(model_unet, 'lms')
        else:
            raise Exception("Unknown sampler: " + sampler_name)

    def init():
        pass

    def sample(init_data, x, conditioning, unconditional_conditioning, sampler_name, img_callback: Callable = None):
        samples_ddim, _ = sampler.sample(S=ddim_steps, conditioning=conditioning, batch_size=int(x.shape[0]), shape=x[0].shape, verbose=False, unconditional_guidance_scale=cfg_scale, unconditional_conditioning=unconditional_conditioning, eta=ddim_eta, x_T=x, img_callback=img_callback)
        return samples_ddim

    try:
        output_images, seed, info, stats = process_images(
            outpath=outpath,
            func_init=init,
            func_sample=sample,
            prompt=prompt,
            seed=seed,
            sampler_name=sampler_name,
            skip_save=skip_save,
            skip_grid=skip_grid,
            batch_size=batch_size,
            n_iter=n_iter,
            steps=ddim_steps,
            cfg_scale=cfg_scale,
            width=width,
            height=height,
            prompt_matrix=prompt_matrix,
            filter_nsfw=filter_nsfw,
            use_GFPGAN=use_GFPGAN,
            use_RealESRGAN=use_RealESRGAN,
            realesrgan_model_name=realesrgan_model_name,
            fp=fp,
            ddim_eta=ddim_eta,
            normalize_prompt_weights=normalize_prompt_weights,
            sort_samples=sort_samples,
            write_info_files=write_info_files,
            write_sample_info_to_log_file=write_to_one_file,
            jpg_sample=jpg_sample,
            variant_amount=variant_amount,
            variant_seed=variant_seed,
            job_info=job_info,
            do_color_correction=do_color_correction,
            correction_target=correction_target
        )

        del sampler

        return output_images, seed, info, stats
    except RuntimeError as e:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        err = e
        err_msg = f'CRASHED:<br><textarea rows="5" style="color:white;background: black;width: -webkit-fill-available;font-family: monospace;font-size: small;font-weight: bold;">{str(e)}</textarea><br><br>Please wait while the program restarts.'
        stats = err_msg
        return [], seed, 'err', stats
    finally:
        if err:
            crash(err, '!!Runtime error (txt2img)!!')


class Flagging(gr.FlaggingCallback):

    def setup(self, components, flagging_dir: str):
        pass

    def flag(self, flag_data, flag_option=None, flag_index=None, username=None):
        import csv

        os.makedirs("log/images", exist_ok=True)

        # those must match the "txt2img" function !! + images, seed, comment, stats !! NOTE: changes to UI output must be reflected here too
        prompt, ddim_steps, sampler_name, toggles, ddim_eta, n_iter, batch_size, cfg_scale, seed, height, width, fp, variant_amount, variant_seed, images, seed, comment, stats = flag_data

        filenames = []

        with open("log/log.csv", "a", encoding="utf8", newline='') as file:
            import time
            import base64

            at_start = file.tell() == 0
            writer = csv.writer(file)
            if at_start:
                writer.writerow(["sep=,"])
                writer.writerow(["prompt", "seed", "width", "height", "sampler", "toggles", "n_iter", "n_samples", "cfg_scale", "steps", "filename"])

            filename_base = str(int(time.time() * 1000))
            for i, filedata in enumerate(images):
                filename = "log/images/"+filename_base + ("" if len(images) == 1 else "-"+str(i+1)) + ".png"

                if filedata.startswith("data:image/png;base64,"):
                    filedata = filedata[len("data:image/png;base64,"):]

                with open(filename, "wb") as imgfile:
                    imgfile.write(base64.decodebytes(filedata.encode('utf-8')))

                filenames.append(filename)

            writer.writerow([prompt, seed, width, height, sampler_name, toggles, n_iter, batch_size, cfg_scale, ddim_steps, filenames[0]])

        print("Logged:", filenames[0])


def blurArr(a,r=8):
    im1=Image.fromarray((a*255).astype(np.int8),"L")
    im2 = im1.filter(ImageFilter.GaussianBlur(radius = r))
    out= np.array(im2)/255
    return out



def img2img(prompt: str, image_editor_mode: str, mask_mode: str, mask_blur_strength: int, mask_restore: bool, ddim_steps: int, sampler_name: str,
            toggles: List[int], realesrgan_model_name: str, n_iter: int,  cfg_scale: float, denoising_strength: float,
            seed: int, height: int, width: int, resize_mode: int, init_info: any = None, init_info_mask: any = None, fp = None, job_info: JobInfo = None):
    # print([prompt, image_editor_mode, init_info, init_info_mask, mask_mode,
    #                               mask_blur_strength, ddim_steps, sampler_name, toggles,
    #                               realesrgan_model_name, n_iter, cfg_scale,
    #                               denoising_strength, seed, height, width, resize_mode,
    #                               fp])
    outpath = opt.outdir_img2img or opt.outdir or "outputs/img2img-samples"
    err = False
    seed = seed_to_int(seed)

    batch_size = 1

    prompt_matrix = 0 in toggles
    normalize_prompt_weights = 1 in toggles
    loopback = 2 in toggles
    random_seed_loopback = 3 in toggles
    skip_save = 4 not in toggles
    skip_grid = 5 not in toggles
    sort_samples = 6 in toggles
    write_info_files = 7 in toggles
    write_sample_info_to_log_file = 8 in toggles
    jpg_sample = 9 in toggles
    do_color_correction = 10 in toggles
    filter_nsfw = 11 in toggles
    use_GFPGAN = 12 in toggles
    use_RealESRGAN = 13 in toggles
    with model_manager.model_context(ModelNames.SD_unet) as model_unet:
        if sampler_name == 'DDIM':
            sampler = DDIMSampler(model_unet)
        elif sampler_name == 'k_dpm_2_a':
            sampler = KDiffusionSampler(model_unet, 'dpm_2_ancestral')
        elif sampler_name == 'k_dpm_2':
            sampler = KDiffusionSampler(model_unet, 'dpm_2')
        elif sampler_name == 'k_euler_a':
            sampler = KDiffusionSampler(model_unet, 'euler_ancestral')
        elif sampler_name == 'k_euler':
            sampler = KDiffusionSampler(model_unet, 'euler')
        elif sampler_name == 'k_heun':
            sampler = KDiffusionSampler(model_unet, 'heun')
        elif sampler_name == 'k_lms':
            sampler = KDiffusionSampler(model_unet, 'lms')
        else:
            raise Exception("Unknown sampler: " + sampler_name)

    if image_editor_mode == 'Mask':
        init_img = init_info_mask["image"]
        init_img_transparency = ImageOps.invert(init_img.split()[-1]).convert('L').point(lambda x: 255 if x > 0 else 0, mode='1')
        init_img = init_img.convert("RGB")
        init_img = resize_image(resize_mode, init_img, width, height)
        init_img = init_img.convert("RGB")
        init_mask = init_info_mask["mask"]
        init_mask = ImageChops.lighter(init_img_transparency, init_mask.convert('L')).convert('RGBA')
        init_mask = init_mask.convert("RGB")
        init_mask = resize_image(resize_mode, init_mask, width, height)
        init_mask = init_mask.convert("RGB")
        keep_mask = mask_mode == 0
        init_mask = init_mask if keep_mask else ImageOps.invert(init_mask)
    else:
        init_img = init_info
        init_mask = None
        keep_mask = False

    assert 0. <= denoising_strength <= 1., 'can only work with strength in [0.0, 1.0]'
    t_enc = int(denoising_strength * ddim_steps)

    def init():
        image = init_img.convert("RGB")
        image = resize_image(resize_mode, image, width, height)
        #image = image.convert("RGB")
        image = np.array(image).astype(np.float32) / 255.0
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image)

        mask_channel = None
        if image_editor_mode == "Mask":
            alpha = init_mask.convert("RGBA")
            alpha = resize_image(resize_mode, alpha, width // 8, height // 8)
            mask_channel = alpha.split()[1]

        mask = None
        if mask_channel is not None:
            mask = np.array(mask_channel).astype(np.float32) / 255.0
            mask = (1 - mask)
            mask = np.tile(mask, (4, 1, 1))
            mask = mask[None].transpose(0, 1, 2, 3)
            mask = torch.from_numpy(mask).to(device)

        #let's try and find where init_image is 0's
        #shape is probably (3,width,height)?

        if image_editor_mode == "Uncrop":
            _image=image.numpy()[0]
            _mask=np.ones((_image.shape[1],_image.shape[2]))

            #compute bounding box
            cmax=np.max(_image,axis=0)
            rowmax=np.max(cmax,axis=0)
            colmax=np.max(cmax,axis=1)
            rowwhere=np.where(rowmax>0)[0]
            colwhere=np.where(colmax>0)[0]
            rowstart=rowwhere[0]
            rowend=rowwhere[-1]+1
            colstart=colwhere[0]
            colend=colwhere[-1]+1
            print('bounding box: ',rowstart,rowend,colstart,colend)

            #this is where noise will get added
            PAD_IMG=16
            boundingbox=np.zeros(shape=(height,width))
            boundingbox[colstart+PAD_IMG:colend-PAD_IMG,rowstart+PAD_IMG:rowend-PAD_IMG]=1
            boundingbox=blurArr(boundingbox,4)

            #this is the mask for outpainting
            PAD_MASK=24
            boundingbox2=np.zeros(shape=(height,width))
            boundingbox2[colstart+PAD_MASK:colend-PAD_MASK,rowstart+PAD_MASK:rowend-PAD_MASK]=1
            boundingbox2=blurArr(boundingbox2,4)

            #noise=np.random.randn(*_image.shape)
            noise=np.array([perlinNoise(height,width,height/64,width/64) for i in range(3)])
            _mask*=1-boundingbox2

            #convert 0,1 to -1,1
            _image = 2. * _image - 1.

            #add noise
            boundingbox=np.tile(boundingbox,(3,1,1))
            _image=_image*boundingbox+noise*(1-boundingbox)

            #resize mask
            _mask = np.array(resize_image(resize_mode, Image.fromarray(_mask*255), width // 8, height // 8))/255

            #convert back to torch tensor
            init_image=torch.from_numpy(np.expand_dims(_image,axis=0).astype(np.float32)).to(device)
            mask=torch.from_numpy(_mask.astype(np.float32)).to(device)

        else:
            init_image = 2. * image - 1.

        init_image = init_image.to(device)
        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
        with model_manager.model_context(ModelNames.SD_fs) as model_fs:
            init_latent = model_fs.get_first_stage_encoding(
                model_fs.encode_first_stage(init_image))  # move to latent space

        return init_latent, mask,

    def sample(init_data, x, conditioning, unconditional_conditioning, sampler_name, img_callback: Callable = None):
        t_enc_steps = t_enc
        obliterate = False
        if ddim_steps == t_enc_steps:
            t_enc_steps = t_enc_steps - 1
            obliterate = True

        if sampler_name != 'DDIM':
            x0, z_mask = init_data

            sigmas = sampler.model_wrap.get_sigmas(ddim_steps)
            noise = x * sigmas[ddim_steps - t_enc_steps - 1]

            xi = x0 + noise

            # Obliterate masked image
            if z_mask is not None and obliterate:
                random = torch.randn(z_mask.shape, device=xi.device)
                xi = (z_mask * noise) + ((1-z_mask) * xi)

            sigma_sched = sigmas[ddim_steps - t_enc_steps - 1:]
            model_wrap_cfg = CFGMaskedDenoiser(sampler.model_wrap)
            samples_ddim = K.sampling.__dict__[f'sample_{sampler.get_sampler_name()}'](model_wrap_cfg, xi, sigma_sched, extra_args={'cond': conditioning, 'uncond': unconditional_conditioning, 'cond_scale': cfg_scale, 'mask': z_mask, 'x0': x0, 'xi': xi}, disable=False, callback=partial(KDiffusionSampler.img_callback_wrapper, img_callback))
        else:

            x0, z_mask = init_data

            sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=0.0, verbose=False)
            z_enc = sampler.stochastic_encode(x0, torch.tensor([t_enc_steps]*batch_size).to(device))

            # Obliterate masked image
            if z_mask is not None and obliterate:
                random = torch.randn(z_mask.shape, device=z_enc.device)
                z_enc = (z_mask * random) + ((1-z_mask) * z_enc)

                                # decode it
            samples_ddim = sampler.decode(z_enc, conditioning, t_enc_steps,
                                            unconditional_guidance_scale=cfg_scale,
                                            unconditional_conditioning=unconditional_conditioning,
                                            z_mask=z_mask, x0=x0)
        return samples_ddim


    correction_target = None
    if loopback:
        output_images, info = None, None
        history = []
        initial_seed = None

        # turn on color correction for loopback to prevent known issue of color drift
        do_color_correction = True

        for i in range(n_iter):
            if do_color_correction and i == 0:
                correction_target = cv2.cvtColor(np.asarray(init_img.copy()), cv2.COLOR_RGB2LAB)

            output_images, seed, info, stats = process_images(
                outpath=outpath,
                func_init=init,
                func_sample=sample,
                prompt=prompt,
                seed=seed,
                sampler_name=sampler_name,
                skip_save=skip_save,
                skip_grid=skip_grid,
                batch_size=1,
                n_iter=1,
                steps=ddim_steps,
                cfg_scale=cfg_scale,
                width=width,
                height=height,
                prompt_matrix=prompt_matrix,
                filter_nsfw=filter_nsfw,
                use_GFPGAN=use_GFPGAN,
                use_RealESRGAN=False, # Forcefully disable upscaling when using loopback
                realesrgan_model_name=realesrgan_model_name,
                fp=fp,
                do_not_save_grid=True,
                normalize_prompt_weights=normalize_prompt_weights,
                init_img=init_img,
                init_mask=init_mask,
                keep_mask=keep_mask,
                mask_blur_strength=mask_blur_strength,
                mask_restore=mask_restore,
                denoising_strength=denoising_strength,
                resize_mode=resize_mode,
                uses_loopback=loopback,
                uses_random_seed_loopback=random_seed_loopback,
                sort_samples=sort_samples,
                write_info_files=write_info_files,
                write_sample_info_to_log_file=write_sample_info_to_log_file,
                jpg_sample=jpg_sample,
                job_info=job_info,
                do_color_correction=do_color_correction,
                correction_target=correction_target
            )

            if initial_seed is None:
                initial_seed = seed

            init_img = output_images[0]

            if not random_seed_loopback:
                seed = seed + 1
            else:
                seed = seed_to_int(None)
            denoising_strength = max(denoising_strength * 0.95, 0.1)
            history.append(init_img)

        if not skip_grid:
            grid_count = get_next_sequence_number(outpath, 'grid-')
            grid = image_grid(history, batch_size, force_n_rows=1)
            grid_file = f"grid-{grid_count:05}-{seed}_{prompt.replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})[:128]}.{grid_ext}"
            grid.save(os.path.join(outpath, grid_file), grid_format, quality=grid_quality, lossless=grid_lossless, optimize=True)


        output_images = history
        seed = initial_seed

    else:
        if do_color_correction:
            correction_target = cv2.cvtColor(np.asarray(init_img.copy()), cv2.COLOR_RGB2LAB)

        output_images, seed, info, stats = process_images(
            outpath=outpath,
            func_init=init,
            func_sample=sample,
            prompt=prompt,
            seed=seed,
            sampler_name=sampler_name,
            skip_save=skip_save,
            skip_grid=skip_grid,
            batch_size=batch_size,
            n_iter=n_iter,
            steps=ddim_steps,
            cfg_scale=cfg_scale,
            width=width,
            height=height,
            prompt_matrix=prompt_matrix,
            filter_nsfw=filter_nsfw,
            use_GFPGAN=use_GFPGAN,
            use_RealESRGAN=use_RealESRGAN,
            realesrgan_model_name=realesrgan_model_name,
            fp=fp,
            normalize_prompt_weights=normalize_prompt_weights,
            init_img=init_img,
            init_mask=init_mask,
            keep_mask=keep_mask,
            mask_blur_strength=mask_blur_strength,
            denoising_strength=denoising_strength,
            mask_restore=mask_restore,
            resize_mode=resize_mode,
            uses_loopback=loopback,
            sort_samples=sort_samples,
            write_info_files=write_info_files,
            write_sample_info_to_log_file=write_sample_info_to_log_file,
            jpg_sample=jpg_sample,
            job_info=job_info,
            do_color_correction=do_color_correction,
            correction_target=correction_target
        )

    del sampler

    return output_images, seed, info, stats


prompt_parser = re.compile("""
    (?P<prompt>     # capture group for 'prompt'
    (?:\\\:|[^:])+  # match one or more non ':' characters or escaped colons '\:'
    )               # end 'prompt'
    (?:             # non-capture group
    :+              # match one or more ':' characters
    (?P<weight>     # capture group for 'weight'
    -?\d*\.{0,1}\d+ # match positive or negative integer or decimal number
    )?              # end weight capture group, make optional
    \s*             # strip spaces after weight
    |               # OR
    $               # else, if no ':' then match end of line
    )               # end non-capture group
""", re.VERBOSE)

# grabs all text up to the first occurrence of ':' as sub-prompt
# takes the value following ':' as weight
# if ':' has no value defined, defaults to 1.0
# repeats until no text remaining
def split_weighted_subprompts(input_string, normalize=True):
    parsed_prompts = [(match.group("prompt").replace("\\:", ":"), float(match.group("weight") or 1)) for match in re.finditer(prompt_parser, input_string)]
    if not normalize:
        return parsed_prompts
    weight_sum = sum(map(lambda x: x[1], parsed_prompts))
    if weight_sum == 0:
        print("Warning: Subprompt weights add up to zero. Discarding and using even weights instead.")
        equal_weight = 1 / (len(parsed_prompts) or 1)
        return [(x[0], equal_weight) for x in parsed_prompts]
    return [(x[0], x[1] / weight_sum) for x in parsed_prompts]

def slerp(device, t, v0:torch.Tensor, v1:torch.Tensor, DOT_THRESHOLD=0.9995):
    v0 = v0.detach().cpu().numpy()
    v1 = v1.detach().cpu().numpy()

    dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
    if np.abs(dot) > DOT_THRESHOLD:
        v2 = (1 - t) * v0 + t * v1
    else:
        theta_0 = np.arccos(dot)
        sin_theta_0 = np.sin(theta_0)
        theta_t = theta_0 * t
        sin_theta_t = np.sin(theta_t)
        s0 = np.sin(theta_0 - theta_t) / sin_theta_0
        s1 = sin_theta_t / sin_theta_0
        v2 = s0 * v0 + s1 * v1

    v2 = torch.from_numpy(v2).to(device)

    return v2



def imgproc(image,image_batch,imgproc_prompt,imgproc_toggles, imgproc_upscale_toggles,imgproc_realesrgan_model_name,imgproc_sampling,
 imgproc_steps, imgproc_height, imgproc_width, imgproc_cfg, imgproc_denoising, imgproc_seed,imgproc_gfpgan_strength,imgproc_ldsr_steps,imgproc_ldsr_pre_downSample,imgproc_ldsr_post_downSample):

    outpath = opt.outdir_imglab or opt.outdir or "outputs/imglab-samples"
    output = []
    images = []
    def processGFPGAN(image,strength):
        image = image.convert("RGB")
        metadata = ImageMetadata.get_from_image(image)
        with model_manager.model_context(ModelNames.GFPGAN) as gfpgan:
            cropped_faces, restored_faces, restored_img = gfpgan.enhance(
                np.array(image, dtype=np.uint8),
                has_aligned=False, only_center_face=False, paste_back=True)
        result = Image.fromarray(restored_img)
        if metadata:
            metadata.GFPGAN = True
            ImageMetadata.set_on_image(image, metadata)

        if strength < 1.0:
            result = Image.blend(image, result, strength)

        return result
    def processRealESRGAN(image):
        if 'x2' in imgproc_realesrgan_model_name:
            # downscale to 1/2 size
            modelMode = imgproc_realesrgan_model_name.replace('x2','x4')
        else:
            modelMode = imgproc_realesrgan_model_name
        image = image.convert("RGB")
        metadata = ImageMetadata.get_from_image(image)

        with model_manager.model_context(modelMode) as model:
            result, res = model.enhance(np.array(image, dtype=np.uint8))

        result = Image.fromarray(result)
        ImageMetadata.set_on_image(result, metadata)
        if 'x2' in imgproc_realesrgan_model_name:
            # downscale to 1/2 size
            result = result.resize((result.width//2, result.height//2), LANCZOS)

        return result
    def processGoBig(image):
        metadata = ImageMetadata.get_from_image(image)
        result = processRealESRGAN(image,)
        if 'x4' in imgproc_realesrgan_model_name:
            #downscale to 1/2 size
            result = result.resize((result.width//2, result.height//2), LANCZOS)



        #make sense of parameters
        n_iter = 1
        batch_size = 1
        seed = seed_to_int(imgproc_seed)
        ddim_steps = int(imgproc_steps)
        resize_mode = 0 #need to add resize mode to form, or infer correct resolution from file name
        width = int(imgproc_width)
        height = int(imgproc_height)
        cfg_scale = float(imgproc_cfg)
        denoising_strength = float(imgproc_denoising)
        skip_save = True
        skip_grid = True
        prompt = imgproc_prompt
        t_enc = int(denoising_strength * ddim_steps)
        sampler_name = imgproc_sampling

        with model_manager.model_context(ModelNames.SD_fs) as model_fs:
            if sampler_name == 'DDIM':
                sampler = DDIMSampler(model_fs)
            elif sampler_name == 'k_dpm_2_a':
                sampler = KDiffusionSampler(model_fs, 'dpm_2_ancestral')
            elif sampler_name == 'k_dpm_2':
                sampler = KDiffusionSampler(model_fs, 'dpm_2')
            elif sampler_name == 'k_euler_a':
                sampler = KDiffusionSampler(model_fs, 'euler_ancestral')
            elif sampler_name == 'k_euler':
                sampler = KDiffusionSampler(model_fs, 'euler')
            elif sampler_name == 'k_heun':
                sampler = KDiffusionSampler(model_fs, 'heun')
            elif sampler_name == 'k_lms':
                sampler = KDiffusionSampler(model_fs, 'lms')
            else:
                raise Exception("Unknown sampler: " + sampler_name)
        init_img = result
        init_mask = None
        keep_mask = False
        mask_restore = False
        assert 0. <= denoising_strength <= 1., 'can only work with strength in [0.0, 1.0]'

        def init():
            image = init_img.convert("RGB")
            image = resize_image(resize_mode, image, width, height)
            image = np.array(image).astype(np.float32) / 255.0
            image = image[None].transpose(0, 3, 1, 2)
            image = torch.from_numpy(image)

            init_image = 2. * image - 1.
            init_image = init_image.to(device)
            init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
            with model_manager.model_context(ModelNames.SD_fs) as model_fs:
                init_latent = model_fs.get_first_stage_encoding(
                    model_fs.encode_first_stage(init_image))  # move to latent space

            return init_latent,

        def sample(init_data, x, conditioning, unconditional_conditioning, sampler_name, img_callback: Callable = None):
            if sampler_name != 'DDIM':
                x0, = init_data

                sigmas = sampler.model_wrap.get_sigmas(ddim_steps)
                noise = x * sigmas[ddim_steps - t_enc - 1]

                xi = x0 + noise
                sigma_sched = sigmas[ddim_steps - t_enc - 1:]
                model_wrap_cfg = CFGDenoiser(sampler.model_wrap)
                samples_ddim = K.sampling.__dict__[f'sample_{sampler.get_sampler_name()}'](model_wrap_cfg, xi, sigma_sched, extra_args={'cond': conditioning, 'uncond': unconditional_conditioning, 'cond_scale': cfg_scale}, disable=False, callback=partial(KDiffusionSampler.img_callback_wrapper, img_callback))

            else:
                x0, = init_data
                sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=0.0, verbose=False)
                z_enc = sampler.stochastic_encode(x0, torch.tensor([t_enc]*batch_size).to(device))
                                    # decode it
                samples_ddim = sampler.decode(z_enc, conditioning, t_enc,
                                                unconditional_guidance_scale=cfg_scale,
                                                unconditional_conditioning=unconditional_conditioning,)
            return samples_ddim
        def split_grid(image, tile_w=512, tile_h=512, overlap=64):
            Grid = namedtuple("Grid", ["tiles", "tile_w", "tile_h", "image_w", "image_h", "overlap"])
            w = image.width
            h = image.height

            now = tile_w - overlap  # non-overlap width
            noh = tile_h - overlap

            cols = math.ceil((w - overlap) / now)
            rows = math.ceil((h - overlap) / noh)

            grid = Grid([], tile_w, tile_h, w, h, overlap)
            for row in range(rows):
                row_images = []

                y = row * noh

                if y + tile_h >= h:
                    y = h - tile_h

                for col in range(cols):
                    x = col * now

                    if x+tile_w >= w:
                        x = w - tile_w

                    tile = image.crop((x, y, x + tile_w, y + tile_h))

                    row_images.append([x, tile_w, tile])

                grid.tiles.append([y, tile_h, row_images])

            return grid


        def combine_grid(grid):
            def make_mask_image(r):
                r = r * 255 / grid.overlap
                r = r.astype(np.uint8)
                return Image.fromarray(r, 'L')

            mask_w = make_mask_image(np.arange(grid.overlap, dtype=np.float).reshape((1, grid.overlap)).repeat(grid.tile_h, axis=0))
            mask_h = make_mask_image(np.arange(grid.overlap, dtype=np.float).reshape((grid.overlap, 1)).repeat(grid.image_w, axis=1))

            combined_image = Image.new("RGB", (grid.image_w, grid.image_h))
            for y, h, row in grid.tiles:
                combined_row = Image.new("RGB", (grid.image_w, h))
                for x, w, tile in row:
                    if x == 0:
                        combined_row.paste(tile, (0, 0))
                        continue

                    combined_row.paste(tile.crop((0, 0, grid.overlap, h)), (x, 0), mask=mask_w)
                    combined_row.paste(tile.crop((grid.overlap, 0, w, h)), (x + grid.overlap, 0))

                if y == 0:
                    combined_image.paste(combined_row, (0, 0))
                    continue

                combined_image.paste(combined_row.crop((0, 0, combined_row.width, grid.overlap)), (0, y), mask=mask_h)
                combined_image.paste(combined_row.crop((0, grid.overlap, combined_row.width, h)), (0, y + grid.overlap))

            return combined_image

        grid = split_grid(result, tile_w=width, tile_h=height, overlap=64)
        work = []
        work_results = []

        for y, h, row in grid.tiles:
            for tiledata in row:
                work.append(tiledata[2])
        batch_count = math.ceil(len(work) / batch_size)
        print(f"GoBig upscaling will process a total of {len(work)} images tiled as {len(grid.tiles[0][2])}x{len(grid.tiles)} in a total of {batch_count} batches.")
        for i in range(batch_count):
            init_img = work[i*batch_size:(i+1)*batch_size][0]
            output_images, seed, info, stats = process_images(
                    outpath=outpath,
                    func_init=init,
                    func_sample=sample,
                    prompt=prompt,
                    seed=seed,
                    sampler_name=sampler_name,
                    skip_save=skip_save,
                    skip_grid=skip_grid,
                    batch_size=batch_size,
                    n_iter=n_iter,
                    steps=ddim_steps,
                    cfg_scale=cfg_scale,
                    width=width,
                    height=height,
                    prompt_matrix=None,
                    filter_nsfw=False,
                    use_GFPGAN=None,
                    use_RealESRGAN=None,
                    realesrgan_model_name=None,
                    fp=None,
                    normalize_prompt_weights=False,
                    init_img=init_img,
                    init_mask=None,
                    keep_mask=False,
                    mask_blur_strength=None,
                    denoising_strength=denoising_strength,
                    mask_restore=mask_restore,
                    resize_mode=resize_mode,
                    uses_loopback=False,
                    sort_samples=True,
                    write_info_files=True,
                    write_sample_info_to_log_file=False,
                    jpg_sample=False,
                    imgProcessorTask=True
                )
            #if initial_seed is None:
            #    initial_seed = seed
            #seed = seed + 1

            work_results.append(output_images[0])
        image_index = 0
        for y, h, row in grid.tiles:
            for tiledata in row:
                tiledata[2] = work_results[image_index]
                image_index += 1

        combined_image = combine_grid(grid)
        grid_count = len(os.listdir(outpath)) - 1
        del sampler

        torch.cuda.empty_cache()
        ImageMetadata.set_on_image(combined_image, metadata)
        return combined_image
    def processLDSR(image):
        metadata = ImageMetadata.get_from_image(image)
        with model_manager.model_context(ModelNames.LDSR) as ldsr:
            result = ldsr.superResolution(image,int(imgproc_ldsr_steps),str(imgproc_ldsr_pre_downSample),str(imgproc_ldsr_post_downSample))
        ImageMetadata.set_on_image(result, metadata)
        return result

    def processGoLatent(image):
        image = processGoBig(image)
        image = processGoLatent(image)
        return image

    if image_batch != None:
        if image != None:
            print("Batch detected and single image detected, please only use one of the two. Aborting.")
            return None
        #convert file to pillow image
        for img in image_batch:
            image = Image.fromarray(np.array(Image.open(img)))
            images.append(image)

    elif image != None:
        if image_batch != None:
            print("Batch detected and single image detected, please only use one of the two. Aborting.")
            return None
        else:
            images.append(image)

    if len(images) > 0:
        print("Processing images...")

        fix_faces = (0 in imgproc_toggles)
        upscale = (1 in imgproc_toggles)
        outpathDir = outpath
        if upscale:
            if imgproc_upscale_toggles == 0:  # RealESRGAN
                upscaler, upscaler_name = processRealESRGAN, "realESRGAN"
            elif imgproc_upscale_toggles == 1:  # GoBig
                upscaler, upscaler_name = processGoBig, "GoBig"
            elif imgproc_upscale_toggles == 2:  # Latent Diffusion SR
                upscaler, upscaler_name = processLDSR, "LDSR"
            elif imgproc_upscale_toggles == 3:  # GoLatent
                upscaler, upscaler_name = processGoLatent, "GoLatent"
            outpathDir = os.path.join(outpath, upscaler_name)

        name_modifiers = ""
        if fix_faces:
            name_modifiers += "-gfpgan"

        for orig_image in images:
            processed_image = orig_image

            if fix_faces:
                processed_image = processGFPGAN(processed_image, imgproc_gfpgan_strength)

            if upscale:
                # Upscalers may not save metadata
                metadata = ImageMetadata.get_from_image(processed_image)
                processed_image = upscaler(processed_image)
                ImageMetadata.set_on_image(processed_image, metadata)

            # TODO: What was the toggle to disable saving this before?
            os.makedirs(outpathDir, exist_ok=True)
            batchNumber = get_next_sequence_number(outpathDir)
            outFilename = f"{batchNumber}-result{name_modifiers}"
            save_sample(processed_image, outpathDir, outFilename, False, None, None, None, None, None,
                        False, None, None, None, None, None, None, None, None, None, False)

            output.append(processed_image)

    print("Done.")
    return output

if opt.defaults is not None and os.path.isfile(opt.defaults):
    try:
        with open(opt.defaults, "r", encoding="utf8") as f:
            user_defaults = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"Error loading defaults file {opt.defaults}:", e, file=sys.stderr)
        print("Falling back to program defaults.", file=sys.stderr)
        user_defaults = {}
else:
    user_defaults = {}

# make sure these indicies line up at the top of txt2img()
txt2img_toggles = [
    'Create prompt matrix (separate multiple prompts using |, and get all combinations of them)',
    'Normalize Prompt Weights (ensure sum of weights add up to 1.0)',
    'Save individual images',
    'Save grid',
    'Sort samples by prompt',
    'Write sample info files',
    'write sample info to log file',
    'jpg samples',
    'Filter NSFW content',
]

if model_manager.is_loadable(ModelNames.GFPGAN):
    txt2img_toggles.append('Fix faces using GFPGAN')
if model_manager.is_loadable(ModelNames.RealESRGAN):
    txt2img_toggles.append('Upscale images using RealESRGAN')

txt2img_defaults = {
    'prompt': '',
    'ddim_steps': 50,
    'toggles': [1, 2, 3],
    'sampler_name': 'k_lms',
    'ddim_eta': 0.0,
    'n_iter': 1,
    'batch_size': 1,
    'cfg_scale': 7.5,
    'seed': '',
    'height': 512,
    'width': 512,
    'fp': None,
    'variant_amount': 0.0,
    'variant_seed': '',
    'submit_on_enter': 'Yes',
    'realesrgan_model_name': 'RealESRGAN_x4plus',
}

if 'txt2img' in user_defaults:
    txt2img_defaults.update(user_defaults['txt2img'])

txt2img_toggle_defaults = [txt2img_toggles[i] for i in txt2img_defaults['toggles']]

imgproc_defaults = {
    'prompt': '',
    'ddim_steps': 50,
    'sampler_name': 'k_lms',
    'cfg_scale': 7.5,
    'seed': '',
    'height': 512,
    'width': 512,
    'denoising_strength': 0.30
}
imgproc_mode_toggles = [
    'Fix Faces',
    'Upscale'
]

#sample_img2img = "assets/stable-samples/img2img/sketch-mountains-input.jpg"
#sample_img2img = sample_img2img if os.path.exists(sample_img2img) else None
sample_img2img = None
# make sure these indicies line up at the top of img2img()
img2img_toggles = [
    'Create prompt matrix (separate multiple prompts using |, and get all combinations of them)',
    'Normalize Prompt Weights (ensure sum of weights add up to 1.0)',
    'Loopback (use images from previous batch when creating next batch)',
    'Random loopback seed',
    'Save individual images',
    'Save grid',
    'Sort samples by prompt',
    'Write sample info files',
    'Write sample info to one file',
    'jpg samples',
    'Color correction (always enabled on loopback mode)',
    'Filter NSFW content',
]
# removed for now becuase of Image Lab implementation
if model_manager.is_loadable(ModelNames.GFPGAN):
    img2img_toggles.append('Fix faces using GFPGAN')
if model_manager.is_loadable(ModelNames.RealESRGAN):
    img2img_toggles.append('Upscale images using RealESRGAN')

img2img_mask_modes = [
    "Keep masked area",
    "Regenerate only masked area",
]

img2img_resize_modes = [
    "Just resize",
    "Crop and resize",
    "Resize and fill",
]

img2img_defaults = {
    'prompt': '',
    'ddim_steps': 50,
    'toggles': [1, 4, 5],
    'sampler_name': 'k_lms',
    'ddim_eta': 0.0,
    'n_iter': 1,
    'batch_size': 1,
    'cfg_scale': 5.0,
    'denoising_strength': 0.75,
    'mask_mode': 0,
    'mask_restore': False,
    'resize_mode': 0,
    'seed': '',
    'height': 512,
    'width': 512,
    'fp': None,
    'mask_blur_strength': 1,
    'realesrgan_model_name': 'RealESRGAN_x4plus',
    'image_editor_mode': 'Mask'
}

if 'img2img' in user_defaults:
    img2img_defaults.update(user_defaults['img2img'])

img2img_toggle_defaults = [img2img_toggles[i] for i in img2img_defaults['toggles']]
img2img_image_mode = 'sketch'

from scn2img import get_scn2img, scn2img_define_args
# avoid circular import, by passing all necessary types, functions 
# and variables to get_scn2img, which will return scn2img function.
scn2img = get_scn2img(
    MemUsageMonitor, save_sample, get_next_sequence_number, seed_to_int, 
    txt2img, txt2img_defaults, img2img, img2img_defaults, 
    opt
)

scn2img_toggles = [
    'Clear Cache',
    'Output intermediate images',
    'Save individual images',
    'Write sample info files',
    'Write sample info to one file',
    'jpg samples',
]
scn2img_defaults = {
    'prompt': '',
    'seed': '',
    'toggles': [1, 2, 3]
}

if 'scn2img' in user_defaults:
    scn2img_defaults.update(user_defaults['scn2img'])

scn2img_toggle_defaults = [scn2img_toggles[i] for i in scn2img_defaults['toggles']]

help_text = """
    ## Mask/Crop
    * The masking/cropping is very temperamental.
    * It may take some time for the image to show when switching from Crop to Mask.
    * If the image doesn't appear after switching to Mask, switch back to Crop and then back again to Mask
    * If the mask appears distorted (the brush is weirdly shaped instead of round), switch back to Crop and then back again to Mask.

    ## Advanced Editor
    * For now the button needs to be clicked twice the first time.
    * Once you have edited your image, you _need_ to click the save button for the next step to work.
    * Clear the image from the crop editor (click the x)
    * Click "Get Image from Advanced Editor" to get the image you saved. If it doesn't work, try opening the editor and saving again.

    If it keeps not working, try switching modes again, switch tabs, clear the image or reload.
"""

def show_help():
    return [gr.update(visible=False), gr.update(visible=True), gr.update(value=help_text)]

def hide_help():
    return [gr.update(visible=True), gr.update(visible=False), gr.update(value="")]


demo = draw_gradio_ui(opt,
                      user_defaults=user_defaults,
                      txt2img=txt2img,
                      img2img=img2img,
                      imgproc=imgproc,
                      scn2img=scn2img,
                      txt2img_defaults=txt2img_defaults,
                      txt2img_toggles=txt2img_toggles,
                      txt2img_toggle_defaults=txt2img_toggle_defaults,
                      show_embeddings=has_embeddings,
                      img2img_defaults=img2img_defaults,
                      img2img_toggles=img2img_toggles,
                      img2img_toggle_defaults=img2img_toggle_defaults,
                      img2img_mask_modes=img2img_mask_modes,
                      img2img_resize_modes=img2img_resize_modes,
                      sample_img2img=sample_img2img,
                      imgproc_defaults=imgproc_defaults,
                      imgproc_mode_toggles=imgproc_mode_toggles,
                      has_real_esrgan=model_manager.is_loadable(ModelNames.RealESRGAN),
                      has_gfpgan=model_manager.is_loadable(ModelNames.GFPGAN),
                      has_ldsr=model_manager.is_loadable(ModelNames.LDSR),
                      scn2img_defaults=scn2img_defaults,
                      scn2img_toggles=scn2img_toggles,
                      scn2img_toggle_defaults=scn2img_toggle_defaults,
                      scn2img_define_args=scn2img_define_args,
                      job_manager=job_manager
                        )

class ServerLauncher(threading.Thread):
    def __init__(self, demo):
        threading.Thread.__init__(self)
        self.name = 'Gradio Server Thread'
        self.demo = demo

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        gradio_params = {
            'inbrowser': opt.inbrowser,
            'server_name': '0.0.0.0',
            'server_port': opt.port,
            'share': opt.share,
            'show_error': True
        }
        if not opt.share:
            demo.queue(concurrency_count=opt.max_jobs)
        if opt.share and opt.share_password:
            gradio_params['auth'] = ('webui', opt.share_password)

        # Check to see if Port 7860 is open
        port_status = 1
        while port_status != 0:
            try:
                self.demo.launch(**gradio_params)
            except (OSError) as e:
                print (f'Error: Port: {opt.port} is not open yet. Please wait, this may take upwards of 60 seconds...')
                time.sleep(10)
            else:
                port_status = 0

    def stop(self):
        self.demo.close() # this tends to hang

def launch_server():
    server_thread = ServerLauncher(demo)
    server_thread.start()

    try:
        while server_thread.is_alive():
            time.sleep(60)
    except (KeyboardInterrupt, OSError) as e:
        crash(e, 'Shutting down...')

def run_headless():
    with open(opt.cli, 'r', encoding='utf8') as f:
        kwargs = yaml.safe_load(f)
    target = kwargs.pop('target')
    if target == 'txt2img':
        target_func = txt2img
    elif target == 'img2img':
        target_func = img2img
        raise NotImplementedError()
    else:
        raise ValueError(f'Unknown target: {target}')
    prompts = kwargs.pop("prompt")
    prompts = prompts if type(prompts) is list else [prompts]
    for i, prompt_i in enumerate(prompts):
        print(f"===== Prompt {i+1}/{len(prompts)}: {prompt_i} =====")
        output_images, seed, info, stats = target_func(prompt=prompt_i, **kwargs)
        print(f'Seed: {seed}')
        print(info)
        print(stats)
        print()

@logger.catch
def run_bridge(interval, api_key, horde_name, horde_url, priority_usernames, horde_max_pixels, horde_nsfw):
    current_id = None
    current_payload = None
    loop_retry = 0
    while True:
        gen_dict = {
            "name": horde_name,
            "max_pixels": horde_max_pixels,
            "priority_usernames": priority_usernames,
            "nsfw": horde_nsfw,
        }
        headers = {"apikey": api_key}
        if current_id:
            loop_retry += 1
        else:
            try:
                pop_req = requests.post(horde_url + '/api/v2/generate/pop', json = gen_dict, headers = headers)
            except requests.exceptions.ConnectionError:
                logger.warning(f"Server {horde_url} unavailable during pop. Waiting 10 seconds...")
                time.sleep(10)
                continue
            except requests.exceptions.JSONDecodeError():
                logger.warning(f"Server {horde_url} unavailable during pop. Waiting 10 seconds...")
                time.sleep(10)
                continue
            try:
                pop = pop_req.json()
            except json.decoder.JSONDecodeError:
                logger.error(f"Could not decode response from {horde_url} as json. Please inform its administrator!")
                time.sleep(interval)
                continue
            if pop == None:
                logger.error(f"Something has gone wrong with {horde_url}. Please inform its administrator!")
                time.sleep(interval)
                continue
            if not pop_req.ok:
                message = pop['message']
                logger.warning(f"During gen pop, server {horde_url} responded with status code {pop_req.status_code}: {pop['message']}. Waiting for 10 seconds...")
                if 'errors' in pop:
                    logger.warning(f"Detailed Request Errors: {pop['errors']}")
                time.sleep(10)
                continue
            if not pop.get("id"):
                skipped_info = pop.get('skipped')
                if skipped_info and len(skipped_info):
                    skipped_info = f" Skipped Info: {skipped_info}."
                else:
                    skipped_info = ''
                logger.debug(f"Server {horde_url} has no valid generations to do for us.{skipped_info}")
                time.sleep(interval)
                continue
            current_id = pop['id']
            logger.debug(f"Request with id {current_id} picked up. Initiating work...")
            current_payload = pop['payload']
            if 'toggles' in current_payload and current_payload['toggles'] == None:
                logger.error(f"Received Bad payload: {pop}")
                current_id = None
                current_payload = None
                current_generation = None
                time.sleep(10)
                continue
        images, seed, info, stats = txt2img(**current_payload)
        buffer = BytesIO()
        # We send as WebP to avoid using all the horde bandwidth
        images[0].save(buffer, format="WebP", quality=90)
        # logger.info(info)
        submit_dict = {
            "id": current_id,
            "generation": base64.b64encode(buffer.getvalue()).decode("utf8"),
            "api_key": api_key,
            "seed": seed,
            "max_pixels": horde_max_pixels,
        }
        current_generation = seed
        while current_id and current_generation:
            try:
                submit_req = requests.post(horde_url + '/api/v2/generate/submit', json = submit_dict, headers = headers)
                try:
                    submit = submit_req.json()
                except json.decoder.JSONDecodeError:
                    logger.error(f"Something has gone wrong with {horde_url} during submit. Please inform its administrator!")
                    time.sleep(interval)
                    continue
                if submit_req.status_code == 404:
                    logger.warning(f"The generation we were working on got stale. Aborting!")
                elif not submit_req.ok:
                    logger.warning(f"During gen submit, server {horde_url} responded with status code {submit_req.status_code}: {submit['message']}. Waiting for 10 seconds...")
                    if 'errors' in submit:
                        logger.warning(f"Detailed Request Errors: {submit['errors']}")
                    time.sleep(10)
                    continue
                else:
                    logger.info(f'Submitted generation with id {current_id} and contributed for {submit_req.json()["reward"]}')
                current_id = None
                current_payload = None
                current_generation = None
            except requests.exceptions.ConnectionError:
                logger.warning(f"Server {horde_url} unavailable during submit. Waiting 10 seconds...")
                time.sleep(10)
                continue
        time.sleep(interval)


if __name__ == '__main__':
    set_logger_verbosity(opt.verbosity)
    quiesce_logger(opt.quiet)
    if opt.cli:
        run_headless()
    if opt.bridge:
        try:
            import bridgeData as cd
        except:
            logger.warning("No bridgeData found, use default where no CLI args are set")
            class temp(object):
                def __init__(self):
                    random.seed()
                    self.horde_url = "https://stablehorde.net"
                    # Give a cool name to your instance
                    self.horde_name = f"Automated Instance #{random.randint(-100000000, 100000000)}"
                    # The api_key identifies a unique user in the horde
                    self.horde_api_key = "0000000000"
                    # Put other users whose prompts you want to prioritize.
                    # The owner's username is always included so you don't need to add it here, unless you want it to have lower priority than another user
                    self.horde_priority_usernames = []
                    self.horde_max_power = 8
                    self.nsfw = True
            cd = temp()
        horde_api_key = opt.horde_api_key if opt.horde_api_key else cd.horde_api_key
        horde_name = opt.horde_name if opt.horde_name else cd.horde_name
        horde_url = opt.horde_url if opt.horde_url else cd.horde_url
        horde_priority_usernames = opt.horde_priority_usernames if opt.horde_priority_usernames else cd.horde_priority_usernames
        horde_max_power = opt.horde_max_power if opt.horde_max_power else cd.horde_max_power
        try:
            horde_nsfw = opt.horde_nsfw if opt.horde_nsfw else cd.horde_nsfw 
        except AttributeError:
            horde_nsfw = True
        if horde_max_power < 2:
            horde_max_power = 2
        horde_max_pixels = 64*64*8*horde_max_power
        logger.info(f"Joining Horde with parameters: API Key '{horde_api_key}'. Server Name '{horde_name}'. Horde URL '{horde_url}'. Max Pixels {horde_max_pixels}")
        try:
            run_bridge(1, horde_api_key, horde_name, horde_url, horde_priority_usernames, horde_max_pixels, horde_nsfw)
        except KeyboardInterrupt:
            logger.info(f"Keyboard Interrupt Received. Ending Bridge")
    else:
        launch_server()