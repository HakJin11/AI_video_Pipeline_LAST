"""Background pictures made by OpenAI's image API (ChatGPT's image model) from a place description.

The key lives only on this PC, in data/secrets.json (typed in by the user on the admin screen) or in the
OPENAI_API_KEY environment variable. It is never sent back to the page.
"""
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import BACKGROUNDS_DIR, DATA_DIR

SECRETS = DATA_DIR / "secrets.json"
DEFAULT_MODEL = "gpt-image-2"
_URL = "https://api.openai.com/v1/images/generations"


class OpenAIError(RuntimeError):
    pass


def _secrets() -> dict:
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def api_key() -> str:
    return (_secrets().get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def model() -> str:
    return (_secrets().get("openai_image_model") or DEFAULT_MODEL).strip()


def status() -> dict:
    key = api_key()
    return {"has_key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else "", "model": model()}


def save_settings(key: str | None = None, image_model: str | None = None) -> dict:
    data = _secrets()
    if key is not None:
        data["openai_api_key"] = key.strip()
    if image_model is not None:
        data["openai_image_model"] = image_model.strip() or DEFAULT_MODEL
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return status()


def background_prompt(place_en: str) -> str:
    """The place as an EMPTY set: the people are put in later by the still step. Written as what the picture
    shows -- "space where two actors will stand" made Z-Image draw two actors in 8 of 8 pictures (2026-09-30),
    and it has no negative prompt (cfg 1). No text either: it ends up burned into the video."""
    return (
        f"A realistic photograph of an empty place: {place_en}. The place is completely empty and quiet, "
        f"nobody is there, like early morning before it opens. Eye-level camera at standing height, a "
        f"medium-wide view with clear open floor in the middle of the frame. Natural, believable lighting, "
        f"shot on a cinema camera, 16:9 composition, plain surfaces without any lettering."
    )


def generate_background(place_en: str) -> Path:
    """One 1536x1024 picture for the place; saved under media/backgrounds, path returned."""
    key = api_key()
    if not key:
        raise OpenAIError("OpenAI API 키가 없습니다. 관리자 화면에서 키를 넣어 주세요.")
    body = {"model": model(), "prompt": background_prompt(place_en), "n": 1, "size": "1536x1024",
            "quality": "medium", "output_format": "png"}
    try:
        resp = httpx.post(_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=300)
    except httpx.HTTPError as exc:
        raise OpenAIError(f"OpenAI에 연결하지 못했습니다: {exc}") from exc
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            msg = resp.text
        if resp.status_code == 401:
            msg = "API 키가 맞지 않습니다 (401). 키를 다시 확인해 주세요."
        raise OpenAIError(f"OpenAI 이미지 생성 실패 ({resp.status_code}): {msg[:300]}")
    data = resp.json().get("data") or []
    if not data or not data[0].get("b64_json"):
        raise OpenAIError("OpenAI가 이미지를 돌려주지 않았습니다")
    BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKGROUNDS_DIR / f"gpt_{uuid4().hex}.png"
    dest.write_bytes(base64.b64decode(data[0]["b64_json"]))
    return dest
