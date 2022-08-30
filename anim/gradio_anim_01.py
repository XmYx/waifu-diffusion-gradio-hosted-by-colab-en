# def DeforumArgs():
#     #@markdown **Save & Display Settings**
#     batch_name = "StableFun" #@param {type:"string"}
#     outdir = get_output_folder(output_path, batch_name)
#     save_grid = False
#     save_settings = True #@param {type:"boolean"}
#     save_samples = True #@param {type:"boolean"}
#     display_samples = True #@param {type:"boolean"}
#
#     #@markdown **Image Settings**
#     n_samples = 1 #@param
#     W = 512 #@param
#     H = 512 #@param
#     W, H = map(lambda x: x - x % 64, (W, H))  # resize to integer multiple of 64
#
#     #@markdown **Init Settings**
#     use_init = False #@param {type:"boolean"}
#     strength = 0.5 #@param {type:"number"}
#     init_image = "https://cdn.pixabay.com/photo/2022/07/30/13/10/green-longhorn-beetle-7353749_1280.jpg" #@param {type:"string"}
#
#     #@markdown **Sampling Settings**
#     seed = -1 #@param
#     sampler = 'klms' #@param ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral","plms", "ddim"]
#     steps = 10 #@param
#     scale = 7 #@param
#     ddim_eta = 0.0 #@param
#     dynamic_threshold = None
#     static_threshold = None
#
#     #@markdown **Batch Settings**
#     n_batch = 1 #@param
#     seed_behavior = "iter" #@param ["iter","fixed","random"]
#
#     precision = 'autocast'
#     fixed_code = True
#     C = 4
#     f = 8
#
#     prompt = ""
#     timestring = ""
#     init_latent = None
#     init_sample = None
#     init_c = None
#
#     return locals()
#
# args = SimpleNamespace(**DeforumArgs())
#
#
# def DeforumAnimArgs():
#
#     #@markdown ####**Animation:**
#     animation_mode = 'None' #@param ['None', '2D', 'Video Input', 'Interpolation'] {type:'string'}
#     max_frames = 1000#@param {type:"number"}
#     border = 'wrap' #@param ['wrap', 'replicate'] {type:'string'}
#
#     #@markdown ####**Motion Parameters:**
#     key_frames = True #@param {type:"boolean"}
#     interp_spline = 'Linear' #Do not change, currently will not look good. param ['Linear','Quadratic','Cubic']{type:"string"}
#     angle = "0:(0)"#@param {type:"string"}
#     zoom = "0: (1.04)"#@param {type:"string"}
#     translation_x = "0: (0)"#@param {type:"string"}
#     translation_y = "0: (0)"#@param {type:"string"}
#
#     #@markdown ####**Coherence:**
#     color_coherence = 'MatchFrame0' #@param ['None', 'MatchFrame0'] {type:'string'}
#     previous_frame_noise = 0.02#@param {type:"number"}
#     previous_frame_strength = 0.65 #@param {type:"number"}
#
#     #@markdown ####**Video Input:**
#     video_init_path ='/content/video_in.mp4'#@param {type:"string"}
#     extract_nth_frame = 1#@param {type:"number"}
#
#     #@markdown ####**Interpolation:**
#     interpolate_x_frames = 4 #@param {type:"number"}
#
#     return locals()
#
# anim_args = SimpleNamespace(**DeforumAnimArgs())
#@markdown **Python Definitions**
import json
from IPython import display

import argparse, glob, os, pathlib, subprocess, sys, time
import cv2
import numpy as np
import pandas as pd
import random
import requests
import shutil
import torch
import torch.nn as nn
import torchvision.transforms as T
import torchvision.transforms.functional as TF
from contextlib import contextmanager, nullcontext
from einops import rearrange, repeat
from itertools import islice
from omegaconf import OmegaConf
from PIL import Image
from pytorch_lightning import seed_everything
from skimage.exposure import match_histograms
from torchvision.utils import make_grid
from tqdm import tqdm, trange
from types import SimpleNamespace
from torch import autocast

sys.path.append('./src/taming-transformers')
sys.path.append('./src/clip')
sys.path.append('./stable-diffusion/')
sys.path.append('./k-diffusion')

from helpers import save_samples
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler

from k_diffusion import sampling
from k_diffusion.external import CompVisDenoiser

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

def add_noise(sample: torch.Tensor, noise_amt: float):
    return sample + torch.randn(sample.shape, device=sample.device) * noise_amt

def get_output_folder(output_path,batch_folder=None):
    yearMonth = time.strftime('%Y-%m/')
    out_path = os.path.join(output_path,yearMonth)
    if batch_folder != "":
        out_path = os.path.join(out_path,batch_folder)
        # we will also make sure the path suffix is a slash if linux and a backslash if windows
        if out_path[-1] != os.path.sep:
            out_path += os.path.sep
    os.makedirs(out_path, exist_ok=True)
    return out_path

def load_img(path, shape):
    if path.startswith('http://') or path.startswith('https://'):
        image = Image.open(requests.get(path, stream=True).raw).convert('RGB')
    else:
        image = Image.open(path).convert('RGB')

    image = image.resize(shape, resample=Image.LANCZOS)
    image = np.array(image).astype(np.float16) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

def maintain_colors(prev_img, color_match_sample, hsv=False):
    if hsv:
        prev_img_hsv = cv2.cvtColor(prev_img, cv2.COLOR_RGB2HSV)
        color_match_hsv = cv2.cvtColor(color_match_sample, cv2.COLOR_RGB2HSV)
        matched_hsv = match_histograms(prev_img_hsv, color_match_hsv, multichannel=True)
        return cv2.cvtColor(matched_hsv, cv2.COLOR_HSV2RGB)
    else:
        return match_histograms(prev_img, color_match_sample, multichannel=True)

def make_callback(sampler, dynamic_threshold=None, static_threshold=None):
    # Creates the callback function to be passed into the samplers
    # The callback function is applied to the image after each step
    def dynamic_thresholding_(img, threshold):
        # Dynamic thresholding from Imagen paper (May 2022)
        s = np.percentile(np.abs(img.cpu()), threshold, axis=tuple(range(1,img.ndim)))
        s = np.max(np.append(s,1.0))
        torch.clamp_(img, -1*s, s)
        torch.FloatTensor.div_(img, s)

    # Callback for samplers in the k-diffusion repo, called thus:
    #   callback({'x': x, 'i': i, 'sigma': sigmas[i], 'sigma_hat': sigmas[i], 'denoised': denoised})
    def k_callback(args_dict):
        if static_threshold is not None:
            torch.clamp_(args_dict['x'], -1*static_threshold, static_threshold)
        if dynamic_threshold is not None:
            dynamic_thresholding_(args_dict['x'], dynamic_threshold)

    # Function that is called on the image (img) and step (i) at each step
    def img_callback(img, i):
        # Thresholding functions
        if dynamic_threshold is not None:
            dynamic_thresholding_(img, dynamic_threshold)
        if static_threshold is not None:
            torch.clamp_(img, -1*static_threshold, static_threshold)

    if sampler in ["plms","ddim"]:
        # Callback function formated for compvis latent diffusion samplers
        callback = img_callback
    else:
        # Default callback function uses k-diffusion sampler variables
        callback = k_callback

    return callback

def generate(args, return_latent=False, return_sample=False, return_c=False):
    seed_everything(seed)
    os.makedirs(outdir, exist_ok=True)

    if sampler == 'plms':
        sampler = PLMSSampler(model)
    else:
        sampler = DDIMSampler(model)

    model_wrap = CompVisDenoiser(model)
    batch_size = n_samples
    prompt = prompt
    assert prompt is not None
    data = [batch_size * [prompt]]

    init_latent = None
    if init_latent is not None:
        init_latent = init_latent
    elif init_sample is not None:
        init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_sample))
    elif init_image != None and init_image != '':
        init_image = load_img(init_image, shape=(W, H)).to(device)
        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
        init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_image))  # move to latent space

    sampler.make_schedule(ddim_num_steps=steps, ddim_eta=ddim_eta, verbose=False)

    t_enc = int((1.0-strength) * steps)

    start_code = None
    if fixed_code and init_latent == None:
        start_code = torch.randn([n_samples, C, H // f, W // f], device=device)

    callback = make_callback(sampler=sampler,
                            dynamic_threshold=dynamic_threshold,
                            static_threshold=static_threshold)

    results = []
    precision_scope = autocast if precision == "autocast" else nullcontext
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                for n in range(n_samples):
                    for prompts in data:
                        uc = None
                        if scale != 1.0:
                            uc = model.get_learned_conditioning(batch_size * [""])
                        if isinstance(prompts, tuple):
                            prompts = list(prompts)
                        c = model.get_learned_conditioning(prompts)

                        if init_c != None:
                          c = init_c

                        if sampler in ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral"]:
                            shape = [C, H // f, W // f]
                            sigmas = model_wrap.get_sigmas(steps)
                            if use_init:
                                sigmas = sigmas[len(sigmas)-t_enc-1:]
                                x = init_latent + torch.randn([n_samples, *shape], device=device) * sigmas[0]
                            else:
                                x = torch.randn([n_samples, *shape], device=device) * sigmas[0]
                            model_wrap_cfg = CFGDenoiser(model_wrap)
                            extra_args = {'cond': c, 'uncond': uc, 'cond_scale': scale}
                            if sampler=="klms":
                                samples = sampling.sample_lms(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            elif sampler=="dpm2":
                                samples = sampling.sample_dpm_2(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            elif sampler=="dpm2_ancestral":
                                samples = sampling.sample_dpm_2_ancestral(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            elif sampler=="heun":
                                samples = sampling.sample_heun(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            elif sampler=="euler":
                                samples = sampling.sample_euler(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            elif sampler=="euler_ancestral":
                                samples = sampling.sample_euler_ancestral(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                        else:

                            if init_latent != None:
                                z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(device))
                                samples = sampler.decode(z_enc, c, t_enc, unconditional_guidance_scale=scale,
                                                        unconditional_conditioning=uc,)
                            else:
                                if sampler == 'plms' or sampler == 'ddim':
                                    shape = [C, H // f, W // f]
                                    samples, _ = sampler.sample(S=steps,
                                                                    conditioning=c,
                                                                    batch_size=n_samples,
                                                                    shape=shape,
                                                                    verbose=False,
                                                                    unconditional_guidance_scale=scale,
                                                                    unconditional_conditioning=uc,
                                                                    eta=ddim_eta,
                                                                    x_T=start_code,
                                                                    img_callback=callback)

                        if return_latent:
                            results.append(samples.clone())

                        x_samples = model.decode_first_stage(samples)
                        if return_sample:
                            results.append(x_samples.clone())

                        x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                        if return_c:
                            results.append(c.clone())

                        for x_sample in x_samples:
                            x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                            image = Image.fromarray(x_sample.astype(np.uint8))
                            results.append(image)
    return results

def sample_from_cv2(sample: np.ndarray) -> torch.Tensor:
    sample = ((sample.astype(float) / 255.0) * 2) - 1
    sample = sample[None].transpose(0, 3, 1, 2).astype(np.float16)
    sample = torch.from_numpy(sample)
    return sample

def sample_to_cv2(sample: torch.Tensor) -> np.ndarray:
    sample_f32 = rearrange(sample.squeeze().cpu().numpy(), "c h w -> h w c").astype(np.float32)
    sample_f32 = ((sample_f32 * 0.5) + 0.5).clip(0, 1)
    sample_int8 = (sample_f32 * 255).astype(np.uint8)
    return sample_int8


def make_xform_2d(width, height, translation_x, translation_y, angle, scale):
    center = (width // 2, height // 2)
    trans_mat = np.float32([[1, 0, translation_x], [0, 1, translation_y]])
    rot_mat = cv2.getRotationMatrix2D(center, angle, scale)
    trans_mat = np.vstack([trans_mat, [0,0,1]])
    rot_mat = np.vstack([rot_mat, [0,0,1]])
    return np.matmul(rot_mat, trans_mat)

def parse_key_frames(string, prompt_parser=None):
    import re
    pattern = r'((?P<frame>[0-9]+):[\s]*[\(](?P<param>[\S\s]*?)[\)])'
    frames = dict()
    for match_object in re.finditer(pattern, string):
        frame = int(match_object.groupdict()['frame'])
        param = match_object.groupdict()['param']
        if prompt_parser:
            frames[frame] = prompt_parser(param)
        else:
            frames[frame] = param
    if frames == {} and len(string) != 0:
        raise RuntimeError('Key Frame string not correctly formatted')
    return frames

def get_inbetweens(key_frames, integer=False):
    key_frame_series = pd.Series([np.nan for a in range(max_frames)])

    for i, value in key_frames.items():
        key_frame_series[i] = value
    key_frame_series = key_frame_series.astype(float)

    interp_method = interp_spline
    if interp_method == 'Cubic' and len(key_frames.items()) <=3:
      interp_method = 'Quadratic'
    if interp_method == 'Quadratic' and len(key_frames.items()) <= 2:
      interp_method = 'Linear'

    key_frame_series[0] = key_frame_series[key_frame_series.first_valid_index()]
    key_frame_series[max_frames-1] = key_frame_series[key_frame_series.last_valid_index()]
    key_frame_series = key_frame_series.interpolate(method=interp_method.lower(),limit_direction='both')
    if integer:
        return key_frame_series.astype(int)
    return key_frame_series

def next_seed(seed, seed_behavior):
    if seed_behavior == 'iter':
        seed += 1
    elif seed_behavior == 'fixed':
        pass # always keep seed the same
    else:
        seed = random.randint(0, 2**32)
    return seed

def render_image_batch(args):
    prompts = prompts

    # create output folder for the batch
    os.makedirs(outdir, exist_ok=True)
    if save_settings or save_samples:
        print(f"Saving to {os.path.join(outdir, timestring)}_*")

    # save settings for the batch
    if save_settings:
        filename = os.path.join(outdir, f"{timestring}_settings.txt")
        with open(filename, "w+", encoding="utf-8") as f:
            json.dump(dict(__dict__), f, ensure_ascii=False, indent=4)

    index = 0

    # function for init image batching
    init_array = []
    if use_init:
        if init_image == "":
            raise FileNotFoundError("No path was given for init_image")
        if init_image.startswith('http://') or init_image.startswith('https://'):
            init_array.append(init_image)
        elif not os.path.isfile(init_image):
            if init_image[-1] != "/": # avoids path error by adding / to end if not there
                init_image += "/"
            for image in sorted(os.listdir(init_image)): # iterates dir and appends images to init_array
                if image.split(".")[-1] in ("png", "jpg", "jpeg"):
                    init_array.append(init_image + image)
        else:
            init_array.append(init_image)
    else:
        init_array = [""]

    for batch_index in range(n_batch):
        print(f"Batch {batch_index+1} of {n_batch}")

        for image in init_array: # iterates the init images
            init_image = image
            for prompt in prompts:
                prompt = prompt
                results = generate(args)
                for image in results:
                    if save_samples:
                        filename = f"{timestring}_{index:05}_{seed}.png"
                        image.save(os.path.join(outdir, filename))
                    if display_samples:
                        display.display(image)
                    index += 1
                seed = next_seed(args)

#def render_animation(args, anim_args):


def render_input_video(args, anim_args):
    # create a folder for the video input frames to live in
    video_in_frame_path = os.path.join(outdir, 'inputframes')
    os.makedirs(os.path.join(outdir, video_in_frame_path), exist_ok=True)

    # save the video frames from input video
    print(f"Exporting Video Frames (1 every {anim_extract_nth_frame}) frames to {video_in_frame_path}...")
    try:
        for f in pathlib.Path(video_in_frame_path).glob('*.jpg'):
            f.unlink()
    except:
        pass
    vf = f'select=not(mod(n\,{anim_extract_nth_frame}))'
    subprocess.run([
        'ffmpeg', '-i', f'{anim_video_init_path}',
        '-vf', f'{vf}', '-vsync', 'vfr', '-q:v', '2',
        '-loglevel', 'error', '-stats',
        os.path.join(video_in_frame_path, '%04d.jpg')
    ], stdout=subprocess.PIPE).stdout.decode('utf-8')

    # determine max frames from length of input frames
    anim_max_frames = len([f for f in pathlib.Path(video_in_frame_path).glob('*.jpg')])

    use_init = True
    print(f"Loading {anim_max_frames} input frames from {video_in_frame_path} and saving video frames to {outdir}")
    render_animation(args, anim_args)

def render_interpolation(args, anim_args):
    # animations use key framed prompts
    prompts = animation_prompts

    # create output folder for the batch
    os.makedirs(outdir, exist_ok=True)
    print(f"Saving animation frames to {outdir}")

    # save settings for the batch
    settings_filename = os.path.join(outdir, f"{timestring}_settings.txt")
    with open(settings_filename, "w+", encoding="utf-8") as f:
        s = {**dict(__dict__), **dict(anim___dict__)}
        json.dump(s, f, ensure_ascii=False, indent=4)

    # Interpolation Settings
    n_samples = 1
    seed_behavior = 'fixed' # force fix seed at the moment bc only 1 seed is available
    prompts_c_s = [] # cache all the text embeddings

    print(f"Preparing for interpolation of the following...")

    for i, prompt in animation_prompts.items():
      prompt = prompt

      # sample the diffusion model
      results = generate(args, return_c=True)
      c, image = results[0], results[1]
      prompts_c_s.append(c)

      # display.clear_output(wait=True)
      display.display(image)

      seed = next_seed(args)

    display.clear_output(wait=True)
    print(f"Interpolation start...")

    frame_idx = 0

    for i in range(len(prompts_c_s)-1):
      for j in range(anim_interpolate_x_frames+1):
        # interpolate the text embedding
        prompt1_c = prompts_c_s[i]
        prompt2_c = prompts_c_s[i+1]
        init_c = prompt1_c.add(prompt2_c.sub(prompt1_c).mul(j * 1/(anim_interpolate_x_frames+1)))

        # sample the diffusion model
        results = generate(args)
        image = results[0]

        filename = f"{timestring}_{frame_idx:05}.png"
        image.save(os.path.join(outdir, filename))
        frame_idx += 1

        display.clear_output(wait=True)
        display.display(image)

        seed = next_seed(args)

    # generate the last prompt
    init_c = prompts_c_s[-1]
    results = generate(args)
    image = results[0]
    filename = f"{timestring}_{frame_idx:05}.png"
    image.save(os.path.join(outdir, filename))

    display.clear_output(wait=True)
    display.display(image)
    seed = next_seed(args)

    #clear init_c
    init_c = None



def dream_anim(animation_prompts: str, prompts: str, animation_mode: str, max_frames: int, border: str, key_frames: bool, interp_spline: str, angle: str, zoom: str, translation_x: str, translation_y: str, color_coherence: str, previous_frame_noise: float, previous_frame_strength: float, video_init_path: str, extract_nth_frame: int, interpolate_x_frames: int, batch_name: str, outdir: str, save_grid: bool, save_settings: bool, save_samples: bool, display_samples: bool, n_samples: int, W: int, H: int, init_image: str, seed: str, sampler: str, steps: int, scale: int, ddim_eta: float, seed_behavior: str, n_batch: int):
    torch.cuda.empty_cache()
    parser = argparse.ArgumentParser()



    parser.add_argument(
        "--skip_grid",
        action='store_true',
        help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
    )

    parser.add_argument(
        "--skip_save",
        action='store_true',
        help="do not save indiviual samples. For speed measurements.",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor, most often 8 or 16",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        help="if specified, load prompts from this file",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=height,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=width,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v1-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="/gdrive/My Drive/model.ckpt",
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast"
    )

    opt = parser.parse_args()

    dynamic_threshold = None
    static_threshold = None
    precision = 'autocast'
    fixed_code = True
    C = 4
    f = 8
    prompt = ""
    timestring = ""
    init_latent = None
    init_sample = None
    init_c = None

    if animation_mode == 'None':
    max_frames = 1
    if key_frames:
    angle_series = get_inbetweens(parse_key_frames(angle))
    zoom_series = get_inbetweens(parse_key_frames(zoom))
    translation_x_series = get_inbetweens(parse_key_frames(translation_x))
    translation_y_series = get_inbetweens(parse_key_frames(translation_y))
    if seed == -1:
    seed = random.randint(0, 2**32)
    if animation_mode == 'Video Input':
    use_init = True
    if not use_init:
    init_image = None
    strength = 0
    if sampler == 'plms' and (use_init or animation_mode != 'None'):
    print(f"Init images aren't supported with PLMS yet, switching to KLMS")
    sampler = 'klms'
    if sampler != 'ddim':
        ddim_eta = 0

    output_images = []

    # animations use key framed prompts
    prompts = animation_prompts

    # create output folder for the batch
    os.makedirs(outdir, exist_ok=True)
    print(f"Saving animation frames to {outdir}")

    # save settings for the batch
    settings_filename = os.path.join(outdir, f"{timestring}_settings.txt")
    with open(settings_filename, "w+", encoding="utf-8") as f:
        s = {**dict(__dict__), **dict(__dict__)}
        json.dump(s, f, ensure_ascii=False, indent=4)

    # expand prompts out to per-frame
    prompt_series = pd.Series([np.nan for a in range(max_frames)])
    for i, prompt in animation_prompts.items():
        prompt_series[i] = prompt
    prompt_series = prompt_series.ffill().bfill()

    # check for video inits
    using_vid_init = animation_mode == 'Video Input'

    n_samples = 1
    timestring = time.strftime('%Y%m%d%H%M%S')
    strength = max(0.0, min(1.0, strength))

    prev_sample = None
    color_match_sample = None
    for frame_idx in range(max_frames):
        print(f"Rendering animation frame {frame_idx} of {max_frames}")

        # apply transforms to previous frame
        if prev_sample is not None:
            if key_frames:
                angle = angle_series[frame_idx]
                zoom = zoom_series[frame_idx]
                translation_x = translation_x_series[frame_idx]
                translation_y = translation_y_series[frame_idx]
                print(
                    f'angle: {angle}',
                    f'zoom: {zoom}',
                    f'translation_x: {translation_x}',
                    f'translation_y: {translation_y}',
                )
            xform = make_xform_2d(W, H, translation_x, translation_y, angle, zoom)

            # transform previous frame
            prev_img = sample_to_cv2(prev_sample)
            prev_img = cv2.warpPerspective(
                prev_img,
                xform,
                (prev_img.shape[1], prev_img.shape[0]),
                borderMode=cv2.BORDER_WRAP if border == 'wrap' else cv2.BORDER_REPLICATE
            )

            # apply color matching
            if color_coherence == 'MatchFrame0':
                if color_match_sample is None:
                    color_match_sample = prev_img.copy()
                else:
                    prev_img = maintain_colors(prev_img, color_match_sample, (frame_idx%2) == 0)

            # apply frame noising
            noised_sample = add_noise(sample_from_cv2(prev_img), previous_frame_noise)

            # use transformed previous frame as init for current
            use_init = True
            init_sample = noised_sample.half().to(device)



            strength = max(0.0, min(1.0, previous_frame_strength))

        # grab prompt for current frame
        prompt = prompt_series[frame_idx]
        print(f"{prompt} {seed}")

        # grab init image for current frame
        if using_vid_init:
            init_frame = os.path.join(outdir, 'inputframes', f"{frame_idx+1:04}.jpg")
            print(f"Using video init frame {init_frame}")
            init_image = init_frame

        # sample the diffusion model
#        results = generate(args, return_latent=False, return_sample=True)
        seed_everything(seed)
        os.makedirs(outdir, exist_ok=True)

        if sampler == 'plms':
            sampler = PLMSSampler(model)
        else:
            sampler = DDIMSampler(model)

        model_wrap = CompVisDenoiser(model)
        batch_size = n_samples
        prompt = prompt
        assert prompt is not None
        data = [batch_size * [prompt]]

        init_latent = None
        if init_latent is not None:
            init_latent = init_latent
        elif init_sample is not None:
            init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_sample))
        elif init_image != None and init_image != '':
            init_image = load_img(init_image, shape=(W, H)).to(device)
            init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
            init_latent = model.get_first_stage_encoding(model.encode_first_stage(init_image))  # move to latent space

        sampler.make_schedule(ddim_num_steps=steps, ddim_eta=ddim_eta, verbose=False)

        t_enc = int((1.0-strength) * steps)

        start_code = None
        if fixed_code and init_latent == None:
            start_code = torch.randn([n_samples, C, H // f, W // f], device=device)

        callback = make_callback(sampler=sampler,
                                dynamic_threshold=dynamic_threshold,
                                static_threshold=static_threshold)

        results = []
        precision_scope = autocast if precision == "autocast" else nullcontext
        with torch.no_grad():
            with precision_scope("cuda"):
                with model.ema_scope():
                    for n in range(n_samples):
                        for prompts in data:
                            uc = None
                            if scale != 1.0:
                                uc = model.get_learned_conditioning(batch_size * [""])
                            if isinstance(prompts, tuple):
                                prompts = list(prompts)
                            c = model.get_learned_conditioning(prompts)

                            if init_c != None:
                              c = init_c

                            if sampler in ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral"]:
                                shape = [C, H // f, W // f]
                                sigmas = model_wrap.get_sigmas(steps)
                                if use_init:
                                    sigmas = sigmas[len(sigmas)-t_enc-1:]
                                    x = init_latent + torch.randn([n_samples, *shape], device=device) * sigmas[0]
                                else:
                                    x = torch.randn([n_samples, *shape], device=device) * sigmas[0]
                                model_wrap_cfg = CFGDenoiser(model_wrap)
                                extra_args = {'cond': c, 'uncond': uc, 'cond_scale': scale}
                                if sampler=="klms":
                                    samples = sampling.sample_lms(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                                elif sampler=="dpm2":
                                    samples = sampling.sample_dpm_2(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                                elif sampler=="dpm2_ancestral":
                                    samples = sampling.sample_dpm_2_ancestral(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                                elif sampler=="heun":
                                    samples = sampling.sample_heun(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                                elif sampler=="euler":
                                    samples = sampling.sample_euler(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                                elif sampler=="euler_ancestral":
                                    samples = sampling.sample_euler_ancestral(model_wrap_cfg, x, sigmas, extra_args=extra_args, disable=False, callback=callback)
                            else:

                                if init_latent != None:
                                    z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(device))
                                    samples = sampler.decode(z_enc, c, t_enc, unconditional_guidance_scale=scale,
                                                            unconditional_conditioning=uc,)
                                else:
                                    if sampler == 'plms' or sampler == 'ddim':
                                        shape = [C, H // f, W // f]
                                        samples, _ = sampler.sample(S=steps,
                                                                        conditioning=c,
                                                                        batch_size=n_samples,
                                                                        shape=shape,
                                                                        verbose=False,
                                                                        unconditional_guidance_scale=scale,
                                                                        unconditional_conditioning=uc,
                                                                        eta=ddim_eta,
                                                                        x_T=start_code,
                                                                        img_callback=callback)

                            if return_latent:
                                results.append(samples.clone())

                            x_samples = model.decode_first_stage(samples)
                            if return_sample:
                                results.append(x_samples.clone())

                            x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                            if return_c:
                                results.append(c.clone())

                            for x_sample in x_samples:
                                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                                image = Image.fromarray(x_sample.astype(np.uint8))
                                results.append(image)






        sample, image = results[0], results[1]

        filename = f"{timestring}_{frame_idx:05}.png"
        image.save(os.path.join(outdir, filename))
        output_images[-1].append(Image.fromarray(x_sample.astype(np.uint8)))
        if not using_vid_init:
            prev_sample = sample

#        display.clear_output(wait=True)
#        display.display(image)

        seed = next_seed(seed, seed_behavior)

    return output_images



anim_interface = gr.Interface(
    dream_anim,
    inputs=[
        gr.Textbox(label='Animation Prompts',  placeholder="\"a beautiful forest by Asher Brown Durand, trending on Artstation\"", lines=5),
        gr.Textbox(label='Prompts',  placeholder="0: \"a beautiful apple, trending on Artstation\"", lines=5),
        gr.Dropdown(label='Animation Mode', ["None", "2D", "Video Input", "Interpolation"]),
        gr.Slider(minimum=1, maximum=1000, step=1, label='Max frames', value=1),
        gr.Dropdown(label='Border', ["wrap", "replicate"]),
        gr.Checkbox(label='KeyFrames', value=True, visible=False),
        gr.Dropdown(label='Spline Interpolation', ["Linear", "Quadratic", "Cubic"]),
        gr.Textbox(label='Angles',  placeholder="0:(0)", lines=1),
        gr.Textbox(label='Zoom',  placeholder="0: (1.04)", lines=1),
        gr.Textbox(label='Translation X',  placeholder="0: (0)", lines=1),
        gr.Textbox(label='Translation Y',  placeholder="0: (0)", lines=1),
        gr.Dropdown(label='Color Coherence', ["None", "MatchFrame0"]),
        gr.Slider(minimum=0.01, maximum=1.00, step=0.01, label='Prev Frame Noise', value=0.02),
        gr.Slider(minimum=0.01, maximum=1.00, step=0.01, label='Prev Frame Strength', value=0.65),
        gr.Textbox(label='Video init path',  placeholder='/content/video_in.mp4', lines=1),
        gr.Slider(minimum=1, maximum=100, step=1, label='Extract n-th frame', value=1),
        gr.Slider(minimum=1, maximum=25, step=1, label='Interpolate n frames', value=4),
        gr.Textbox(label='Batch Name',  placeholder="Batch_001", lines=1),
        gr.Textbox(label='Output Dir',  placeholder="/content/", lines=1),
        gr.Checkbox(label='Save Grid', value=False, visible=False),
        gr.Checkbox(label='Save Settings', value=True, visible=True),
        gr.Checkbox(label='Save Samples', value=True, visible=True),
        gr.Checkbox(label='Display Samples', value=True, visible=False),
        gr.Slider(minimum=1, maximum=4, step=1, label='Samples (keep on 1)', value=1),
        gr.Slider(minimum=256, maximum=1024, step=64, label='Width', value=512),
        gr.Slider(minimum=256, maximum=1024, step=64, label='Height', value=512),
        gr.Textbox(label='Init Image link',  placeholder="https://cdn.pixabay.com/photo/2022/07/30/13/10/green-longhorn-beetle-7353749_1280.jpg", lines=5),
        gr.Textbox(label='Seed',  placeholder="-1", lines=1, value='-1'),
        gr.Radio(label='Sampler', ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral","plms", "ddim"], value="klms"),
        gr.Slider(minimum=1, maximum=100, step=1, label='Steps', value=10),
        gr.Slider(minimum=1, maximum=25, step=1, label='Scale', value=7),
        gr.Slider(minimum=0, maximum=1.0, step=0.1, label='DDIM ETA', value=0.0),
        gr.Dropdown(label='Seed Behavior', ["iter", "fixed", "random"], value="iter"),
        gr.Slider(minimum=1, maximum=25, step=1, label='Number of Batches', value=1),
    ],
    outputs=[
        gr.Gallery(),
    ],
    title="Stable Diffusion Animation",
    description="",
)



demo = gr.TabbedInterface(interface_list=[anim_interface], tab_names=["Anim"])

demo.launch(share=True)






#if anim_animation_mode == '2D':
#    render_animation(args, anim_args)
#elif anim_animation_mode == 'Video Input':
#    render_input_video(args, anim_args)
#elif anim_animation_mode == 'Interpolation':
#    render_interpolation(args, anim_args)
#else:
#    render_image_batch(args)
