import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

MEDIA_DIR = BASE_DIR / "media"
CHARACTERS_DIR = MEDIA_DIR / "characters"
BACKGROUNDS_DIR = MEDIA_DIR / "backgrounds"
COMPOSITES_DIR = MEDIA_DIR / "composites"
VOICES_DIR = MEDIA_DIR / "voices"
VOICELINES_DIR = MEDIA_DIR / "voicelines"
VIDEOS_DIR = MEDIA_DIR / "videos"
STORIES_DIR = MEDIA_DIR / "stories"

FRONTEND_DIR = BASE_DIR / "frontend"
WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

COMFYUI_URL = "http://127.0.0.1:8188"
# Comfy Desktop moves to the next free port when 8188 is taken (a second instance, a leftover
# process), and then the app talked to nobody. These are probed in order.
COMFYUI_PORTS = [8188, 8189, 8190, 8191]
# ComfyUI Desktop's shared input/output folders (%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared). With another
# ComfyUI install, set COMFYUI_INPUT_DIR / COMFYUI_OUTPUT_DIR to its input / output folders.
_COMFY_SHARED = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Comfy-Desktop" / "ComfyUI-Shared"
COMFYUI_INPUT_DIR = Path(os.environ.get("COMFYUI_INPUT_DIR", str(_COMFY_SHARED / "input")))
COMFYUI_OUTPUT_DIR = Path(os.environ.get("COMFYUI_OUTPUT_DIR", str(_COMFY_SHARED / "output")))

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:12b"

DEFAULT_VIDEO_DURATION_SEC = 20
MANUAL_DIALOGUE_KEYWORD = "직접 입력"


def _tool(name: str) -> str:
    """ffmpeg / ffprobe: env FFMPEG_PATH / FFPROBE_PATH, else C:\\ffmpeg\\bin, else whatever is on PATH."""
    env = os.environ.get(f"{name.upper()}_PATH")
    if env:
        return env
    fixed = Path(r"C:\ffmpeg\bin") / f"{name}.exe"
    return str(fixed) if fixed.exists() else (shutil.which(name) or name)


FFPROBE_PATH = _tool("ffprobe")
FFMPEG_PATH = _tool("ffmpeg")

# multi-scene story pipeline
KOREAN_FONT_PATH = r"C:\Windows\Fonts\malgunbd.ttf"  # for ffmpeg drawtext captions/title cards
STORY_SCENE_COUNT_CHOICES = [3, 4, 6, 8]
STORY_TOTAL_SEC_CHOICES = [60, 80, 120]
STORY_MAX_SCENES = 10

ALL_MEDIA_DIRS = [
    DATA_DIR,
    CHARACTERS_DIR,
    BACKGROUNDS_DIR,
    COMPOSITES_DIR,
    VOICES_DIR,
    VOICELINES_DIR,
    VIDEOS_DIR,
    STORIES_DIR,
]


def ensure_dirs() -> None:
    for d in ALL_MEDIA_DIRS:
        d.mkdir(parents=True, exist_ok=True)
