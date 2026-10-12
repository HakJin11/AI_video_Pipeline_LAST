"""Draw fictional people for the character library with Z-Image-Turbo on this PC (2026-10-12).

Not real people: every face is generated from a text description. Each gets one of the designed (AI-made)
voices. usage: python scripts/make_characters.py   (prints the new character ids)
"""
import hashlib
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import comfy_client, db  # noqa: E402
from app.config import CHARACTERS_DIR  # noqa: E402
from app.local_images import _graph  # noqa: E402

# (name, gender, look for the picture and for the prompts, designed voice name)
PEOPLE = [
    ("지수", "female", "a young Korean woman in her early twenties with long straight black hair and see-through bangs, "
                       "wearing a light blue oxford shirt", "지아"),
    ("예린", "female", "a young Korean woman in her mid twenties with shoulder-length wavy light-brown hair, "
                       "wearing a cream knit sweater", "서연"),
    ("소윤", "female", "a young Korean woman in her late twenties with a short chin-length dark-brown bob, "
                       "wearing a mustard yellow cardigan over a white t-shirt", "하은"),
    ("도현", "male", "a young Korean man in his early twenties with short wavy dark-brown hair parted in the middle, "
                     "wearing a navy hoodie", "민재"),
    ("태민", "male", "a young Korean man in his mid twenties with a short black buzz cut and round wire-rimmed glasses, "
                     "wearing a grey crew-neck sweatshirt", "현우"),
    ("건우", "male", "a Korean man in his late twenties with short black hair swept back and light stubble, "
                     "wearing a black denim jacket over a white t-shirt", "도윤"),
]


def portrait_prompt(look: str) -> str:
    return (f"A realistic head-and-shoulders portrait photograph of {look}. A fictional person. Facing the camera "
            "straight on, relaxed natural expression with the mouth closed, both eyes and ears visible, hair not "
            "covering the face. Soft even studio lighting, plain light-grey background, sharp focus on the face, "
            "natural skin texture, shot on a 50mm lens. No text, no watermark, no other people.")


def main():
    voices = {v["name"]: v["id"] for v in db.list_voices()}
    have = {c["name"] for c in db.list_characters()}
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    for name, gender, look, voice in PEOPLE:
        if name in have:
            print("already there:", name)
            continue
        prompt = portrait_prompt(look)
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12], 16)
        g = _graph(prompt, seed, 896, 1152)
        g["10"]["inputs"]["filename_prefix"] = "character/zimage"
        out = comfy_client.run_prompt_and_wait(g, timeout=600, poll_interval=2)
        dest = CHARACTERS_DIR / f"zchar_{uuid4().hex}.png"
        comfy_client.save_output_to(out, dest, media_keys=["images"])
        c = db.create_character(name, look, gender, f"characters/{dest.name}", voices.get(voice))
        print("made", c["id"], name, dest.name, "voice", voice)


if __name__ == "__main__":
    main()
