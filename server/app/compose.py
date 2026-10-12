import copy
import json
import random
from uuid import uuid4

from app import comfy_client, db
from app.config import COMPOSITES_DIR, MEDIA_DIR, WORKFLOWS_DIR
from app.prompt_builder import build_compose_prompt

_TEMPLATE = json.loads((WORKFLOWS_DIR / "compose.json").read_text(encoding="utf-8"))


def ensure_composite(character1_id: int, character2_id: int, background_id: int) -> dict:
    """Return the cached composite for this character-pair + background, generating one via
    ComfyUI (Qwen Image Edit, 4-step Lightning path) if none exists yet. Identity is anchored to
    each character's ORIGINAL reference photo every time, so re-compositing the same pair into a
    new background keeps their faces consistent across a multi-scene story.

    Shared by the single-video composites route and the multi-scene story pipeline.
    """
    cached = db.find_composite(character1_id, character2_id, background_id)
    if cached:
        return {**cached, "cached": True}

    char1 = db.get_character(character1_id)
    char2 = db.get_character(character2_id)
    bg = db.get_background(background_id)
    if not (char1 and char2 and bg):
        raise ValueError("character or background not found")

    prompt_text = build_compose_prompt(
        char1["name"], char1["description"], char1["gender"],
        char2["name"], char2["description"], char2["gender"],
        bg["description"],
    )

    graph = copy.deepcopy(_TEMPLATE)
    graph["41"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(MEDIA_DIR / char1["image_path"])
    graph["83"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(MEDIA_DIR / char2["image_path"])
    graph["195"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(MEDIA_DIR / bg["image_path"])
    graph["170:151"]["inputs"]["prompt"] = prompt_text
    graph["170:169"]["inputs"]["seed"] = random.randint(0, 2**48 - 1)
    # 4-step Lightning LoRA path (~1.6 min vs ~6.5 min full): faces/lighting hold up, worth it here
    graph["170:168"]["inputs"]["value"] = True

    outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)

    dest = COMPOSITES_DIR / f"{uuid4().hex}.png"
    comfy_client.save_output_to(outputs, dest, media_keys=["images", "image"])

    row = db.create_composite(character1_id, character2_id, background_id, f"composites/{dest.name}")
    return {**row, "cached": False}
