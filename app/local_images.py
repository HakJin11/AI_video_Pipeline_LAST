"""Background pictures drawn on this PC from a text prompt -- free, no API key.

Z-Image-Turbo (text-to-image, 6B, 8-9 steps) was already in ComfyUI-Shared/models: diffusion_models/
z_image_turbo_bf16, text_encoders/qwen_3_4b (CLIP type lumina2), vae/ae. The user asked for this instead
of the paid OpenAI image API (2026-09-30).
"""
import hashlib
from pathlib import Path
from uuid import uuid4

from app import comfy_client
from app.config import BACKGROUNDS_DIR
from app.openai_images import background_prompt

MODEL_NAME = "Z-Image-Turbo (이 PC)"


def _graph(prompt: str, seed: int, width: int = 1344, height: int = 768) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
            "seed": seed, "steps": 9, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple",
            "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "background/zimage"}},
    }


def generate_background(place_en: str, seed: int | None = None) -> Path:
    """One 1344x768 picture of the place, saved under media/backgrounds; path returned."""
    prompt = background_prompt(place_en)
    if seed is None:
        seed = int(hashlib.sha256(f"{prompt}|{uuid4().hex}".encode("utf-8")).hexdigest()[:12], 16)
    outputs = comfy_client.run_prompt_and_wait(_graph(prompt, seed), timeout=600, poll_interval=2)
    BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKGROUNDS_DIR / f"zimage_{uuid4().hex}.png"
    comfy_client.save_output_to(outputs, dest, media_keys=["images"])
    return dest
