"""유튜브 따라하기: re-create a short reference clip cut by cut.

The reference video (the user's own local file) is trimmed to a short window (10s to start),
split at its real camera cuts (ffmpeg scene detection), and long shots are split again so every
cut is 1-2 seconds. Each cut is then re-made as ONE person on screen, talking to someone off-screen:

  cast member (character photo + costume) -> one still per cast member (Qwen edit, cached)
  cut line -> that cast member's voice (TTS) -> fitted to the cut's length
  prompt (built from labelled parts the UI explains) -> LTX clip -> trimmed to the cut's length

One speaker per clip is also the lip-sync fix: the two-shot story clips had one mono track and
only text telling LTX who speaks when, so the second speaker's mouth drifted. Here the only face
in frame is the one speaking.
"""
import copy
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageFilter

from app import comfy_client, db, ollama_client
from app.config import FFMPEG_PATH, FFPROBE_PATH, MEDIA_DIR, WORKFLOWS_DIR
from app.routes.voice_lines import generate_voice_line
from app.scene_render import _speed_tuning as _scene_speed, run_ltx_batch

FOLLOW_DIR = MEDIA_DIR / "follow"
_COMPOSE_TEMPLATE = json.loads((WORKFLOWS_DIR / "compose.json").read_text(encoding="utf-8"))

MIN_CUT_SEC = 1.0
MAX_CUT_SEC = 2.0
_SCENE_THRESHOLD = 0.30
# sketch dialogue is fast, but 1.45x sped-up speech sounded machine-made (user, 2026-09-22). Lines are
# no longer shortened to fit either (user 2026-09-23: "대사를 다 쳐내는" -- "야 보드게임 뭐 이런 거
# 재밌나?" became "보드게임 재밌나?"): the whole line is said, sped up 1.2x at most, and a line that
# still does not fit makes its cut (and the video) longer.
_MAX_SPEEDUP = 1.2
_HARD_SPEEDUP = _MAX_SPEEDUP
_DEFAULT_CPS = 7.3

DEFAULT_SETTING = (
    "a cozy Korean board-game cafe at night: dark brown brick walls with light mortar lines, warm "
    "amber ceiling lights, a colorful board-game poster on the wall, shelves of game boxes and other "
    "customers softly blurred in the background"
)


def project_dir(pid: int) -> Path:
    d = FOLLOW_DIR / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def rel(p: Path) -> str:
    return str(p.relative_to(MEDIA_DIR)).replace("\\", "/")


def _ff(args: list[str]) -> None:
    subprocess.run([FFMPEG_PATH, "-y", "-hide_banner", *args], check=True, capture_output=True)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out or 0)


# ---------------------------------------------------------------- cutting the reference

def trim_reference(src: Path, dest: Path, start: float, length: float) -> float:
    """Re-encode [start, start+length] of the source at 24fps so cut times are frame-exact."""
    _ff(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
         "-vf", "fps=24,scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest)])
    return probe_duration(dest)


def detect_shot_changes(video: Path) -> list[float]:
    proc = subprocess.run(
        [FFMPEG_PATH, "-hide_banner", "-i", str(video), "-vf",
         f"select='gt(scene,{_SCENE_THRESHOLD})',showinfo", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)})


def plan_cuts(shot_changes: list[float], total: float) -> list[tuple[float, float]]:
    """Shots from the real camera cuts, then every shot longer than 2s split evenly into 1-2s cuts.
    A shot shorter than 1s is merged into its neighbour (1-2s per generated clip)."""
    bounds = [0.0] + [t for t in shot_changes if 0.0 < t < total] + [total]
    shots: list[list[float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if shots and (b - a) < MIN_CUT_SEC:
            shots[-1][1] = b
        else:
            shots.append([a, b])
    if len(shots) > 1 and shots[0][1] - shots[0][0] < MIN_CUT_SEC:
        shots[1][0] = shots[0][0]
        shots.pop(0)
    cuts: list[tuple[float, float]] = []
    for a, b in shots:
        n = max(1, math.ceil((b - a) / MAX_CUT_SEC - 1e-6))
        step = (b - a) / n
        for i in range(n):
            cuts.append((round(a + i * step, 3), round(a + (i + 1) * step, 3)))
    return cuts


def ref_for_length(project: dict, seconds: float) -> str:
    """A copy of the preset's reference trimmed to `seconds`, cached next to it. A 10s run pointed at
    the 30s preset file, so the page played 30 seconds of original next to a 10-second remake."""
    ref = MEDIA_DIR / project["ref_video_path"]
    if seconds >= project["length_sec"] - 0.05:
        return project["ref_video_path"]
    dest = ref.with_name(f"ref_{int(round(seconds))}s.mp4")
    if not dest.exists():
        _ff(["-t", f"{seconds:.3f}", "-i", str(ref), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-movflags", "+faststart", str(dest)])
    return rel(dest)


def detect_people_and_shot(thumb: Path) -> tuple[int, str]:
    """Ask the local vision model (gemma) how many people the original cut shows and how tight the
    shot is. One person -> a single portrait still; two or more -> both in one frame."""
    import base64

    import httpx

    from app.config import OLLAMA_MODEL, OLLAMA_URL
    try:
        img = base64.b64encode(thumb.read_bytes()).decode()
        r = ollama_client.gemma_post(f"{OLLAMA_URL}/api/generate", timeout=120, json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json", "keep_alive": "2m",
            "prompt": "Look at this frame from a video. Count only people clearly in the foreground "
                      "(ignore blurred background extras). Answer JSON only: "
                      '{"people": <number>, "shot": "closeup"|"medium"|"wide"}',
            "images": [img]})
        data = json.loads(r.json().get("response", "{}"))
        people = max(1, min(int(data.get("people", 1)), 4))
        shot = data.get("shot") if data.get("shot") in FRAMINGS else "medium"
        return people, shot
    except Exception:  # noqa: BLE001 - detection is a convenience
        return 1, "medium"


def detect_camera(clip: Path) -> str:
    """Which of CAMERAS the original cut uses, from frame-to-frame global shift.

    The board-game original turned out to be locked off on a tripod while our prompts said
    "handheld with a slight sway" -- the opposite of the source."""
    try:
        work = FOLLOW_DIR / "_motion"
        work.mkdir(parents=True, exist_ok=True)
        for f in work.glob("*.png"):
            f.unlink()
        _ff(["-i", str(clip), "-vf", "fps=6,scale=96:54", str(work / "m%03d.png")])
        frames = [list(Image.open(f).convert("L").getdata()) for f in sorted(work.glob("*.png"))]
    except Exception:  # noqa: BLE001 - detection is a convenience, never fail an import over it
        return "locked"
    if len(frames) < 2:
        return "locked"
    w, h, r = 96, 54, 5
    dx_total = dy_total = wobble = 0
    for a, b in zip(frames, frames[1:]):
        best = (0, 0, 1e18)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                tot = cnt = 0
                for y in range(r, h - r, 2):
                    ay, by = y * w, (y + dy) * w
                    for x in range(r, w - r, 2):
                        tot += abs(a[ay + x] - b[by + x + dx])
                        cnt += 1
                if tot / cnt < best[2]:
                    best = (dx, dy, tot / cnt)
        dx_total += best[0]
        dy_total += best[1]
        wobble += abs(best[0]) + abs(best[1])
    wobble /= max(1, len(frames) - 1)
    if abs(dx_total) >= 4 and abs(dx_total) >= abs(dy_total):
        return "pan_right" if dx_total < 0 else "pan_left"
    if wobble >= 1.2:
        return "handheld"
    return "locked"


def extract_ref_cut(ref: Path, start: float, end: float, clip_dest: Path, thumb_dest: Path) -> None:
    _ff(["-ss", f"{start:.3f}", "-i", str(ref), "-t", f"{end - start:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-movflags", "+faststart",
         str(clip_dest)])
    _ff(["-ss", f"{(start + end) / 2:.3f}", "-i", str(ref), "-frames:v", "1", "-vf", "scale=360:-2",
         str(thumb_dest)])


# ---------------------------------------------------------------- prompts (shown + explained in the UI)

def _who(character: dict | None) -> str:
    if not character:
        return "a young Korean person"
    g = character.get("gender")
    base = "a young Korean man" if g == "male" else "a young Korean woman" if g == "female" else "a young Korean person"
    # the library description often names clothes ("wearing a black blazer"); the costume part decides
    # clothes here, so keep only the face/hair part or the two would contradict each other
    desc = re.split(r",?\s+(?:wearing|dressed in|who wears)\b", (character.get("description") or "").strip())[0]
    # hair is part of the costume now ("a chin-length bob"), so drop the photo's own hair words,
    # otherwise the prompt asks for long black hair AND a bob in the same breath
    keep = [seg for seg in desc.split(",") if not re.search(r"hair|bangs|ponytail|braid", seg, re.I)]
    desc = ", ".join(seg.strip() for seg in keep).strip(" ,.")
    return desc if desc.lower().startswith(("a ", "an ")) else f"{base}, {desc}" if desc else base


def who_sentence(character: dict | None) -> str:
    return f"The person is {_who(character)}."


def _pron(character: dict | None) -> str:
    g = (character or {}).get("gender")
    return "he" if g == "male" else "she" if g == "female" else "the person"


# (x0, y0) of a 64% crop of the background photo, per role. With one photo for everybody every cut had the
# identical backdrop (run 127, 2026-09-30: "the camera framing never changes"); real coverage shows a different
# part of the room behind each person, and the same part every time that person is on screen.
_BG_VIEWS = {"A": (0.00, 0.12), "B": (0.36, 0.12), "C": (0.18, 0.20), "D": (0.08, 0.06), "E": (0.28, 0.24)}


# how much of the background photo a shot shows: a closer shot sees a smaller piece of the room. With 64% for
# every shot the person was drawn small and far away in the "chest up" cuts (runs 132/133, 2026-09-30).
_BG_CROP = {"wide": 0.64, "medium": 0.46, "closeup": 0.36}


def _bg_view(bg: dict, cast: dict, framing: str = "medium") -> Path:
    fx, fy = _BG_VIEWS.get(cast.get("label") or "", (0.18, 0.18))
    size = _BG_CROP.get(framing, 0.46)
    # the role's corner of the photo, scaled so the smaller crops still differ from role to role
    x0, y0 = fx / 0.36 * (1 - size), min(fy / 0.36, 1.0) * (1 - size) * 0.7
    dest = FOLLOW_DIR / "faces" / f"bgview_{bg['id']}_{int(x0 * 100)}_{int(y0 * 100)}_{int(size * 100)}.png"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(MEDIA_DIR / bg["image_path"]) as im:
            im = im.convert("RGB")
            w, h = im.size
            cw, ch = int(w * size), int(h * size)
            box = (int(x0 * w), int(y0 * h), int(x0 * w) + cw, int(y0 * h) + ch)
            im.crop(box).resize((w, h), Image.LANCZOS).save(dest)
    return dest


def _bg_sentence(image: str | None, single: bool = False) -> str:
    """A background PHOTO is passed to Qwen as `image`; without saying so it was only a loose hint."""
    if not image:
        return ""
    if single:
        return (f"Behind the person is the place shown in {image} -- the same room, furniture, colours and light, "
                f"softly out of focus. The person stands close to the camera and is as large in the frame as "
                f"the shot size says; the place is only the backdrop. ")
    return (f"The place is exactly the one shown in {image}: the same room, layout, furniture, colours and "
            f"light, seen from a natural eye-level angle, with the people standing in front of it. ")


_HEADWEAR = re.compile(r"\b(cap|hat|beanie|hood|helmet|headband|visor)\b", re.I)


def still_prompt(character: dict | None, costume: str, setting: str, framing: str = "medium",
                 bg_image: str | None = None, face_in_image3: bool = True) -> str:
    shot = FRAMINGS.get(framing, FRAMINGS["medium"])[2]
    # The outfit comes right after the framing. It used to come last, after "take ONLY the face ... not its
    # hat", and Qwen dropped headwear: role E's backwards cap never appeared (2026-09-30, 30s test cut 21);
    # with this order the cap, hair colour and tee all show (checked on E, close-up and medium).
    return (
        f"Photograph the person from image1 as a candid still from a drama scene, set in {setting or DEFAULT_SETTING}. "
        f"{shot.capitalize()}, 16:9 frame, the person slightly left of center. " + _bg_sentence(bg_image, True) +
        f"The person's {'hair, headwear' if _HEADWEAR.search(costume or '') else 'hair'} and outfit are exactly "
        f"this, clearly visible: "
        f"{costume or 'casual everyday clothes'}. "
        # The look of run 86 (2026-09-22, the user's reference on 2026-09-30): head a little to the side, eyes on
        # the friend, NOT on the camera. The "face toward the camera, ~15 degrees" wording that replaced it made
        # every cut a frontal ID-photo. 25 degrees is said so the whole mouth still shows (at ~45 the lips hid).
        # 2026-10-01: "eyes look off-camera ... not into the lens" still gave camera-staring pictures for some
        # faces (은서: 3 of 3, 2 of 2); naming where the pupils sit gave 4 of 4 off-camera. But "at someone standing
        # outside the frame on the right" made Qwen draw that someone: a stranger's back / shoulder at the edge
        # (4 of 6 stills, one a full second man) that LTX turned into a face (run 143 cut 6). "past the right edge
        # of the picture" + "the right side shows only the room": 0 of 6 with anyone near, but some faces came out
        # straight-on (은서 close-up, 성준) and LTX then talked into the lens (run 146). "whole head turned well
        # ... clear three-quarter view, never straight-on" + "listening to someone out of the picture": 은서
        # three-quarter 3 of 4 (was 1 of 4), nobody drawn at the edge; _draw_checked redraws the rest.
        f"The whole head is turned well toward the RIGHT side of the frame (about 35 degrees), so the face is seen "
        f"in a clear three-quarter view, never straight-on: the far side of the face is partly hidden, yet both "
        f"eyes, the nose and the whole mouth stay visible, not a side profile. The eyes look even further right, "
        f"past the right edge of the picture: the pupils sit in the right corners of the eyes, clearly away from "
        f"the camera. "
        # in a knees-up wide still "listening to someone" put that someone in the picture -- a second copy of the
        # speaker facing her (run 148 cut 1); there is room for nobody else only in the closer shots
        + ("A candid moment, about to speak, the lips slightly parted. " if framing == "wide" else
           "A candid moment in a conversation, listening to someone out of the picture and about to answer, the "
           "lips slightly parted. ")
        + f"The person is alone in the picture except for a few small, blurred people far away in the background; "
        f"the right side of the picture shows only the room.\n\n"
        + ("Preserve the exact facial identity and skin tone from image1 and image3 (the same person). It must be "
           "the SAME real person, instantly recognizable, with the same face width, cheeks, jaw and skin texture -- "
           "not slimmer, not younger, not smoother: keep" if bg_image and face_in_image3 else
           "Preserve the exact facial identity and skin tone from image1. It must be the SAME real person as "
           "image1, instantly recognizable: keep")
        + f" the exact face shape, jawline, eye shape and eyelids, eyebrows, "
        f"nose, lips, skin texture and age. Do not beautify, slim, smooth or change the face in any way. "
        f"From image1 take only the face itself; its pose, hands, hair, clothes and anything held are not used "
        f"-- hair, headwear and clothes are the ones described above. Shallow depth of field, the background "
        f"softly blurred, warm indoor lighting falling on the face the same way as on the background, natural "
        f"relaxed expression, mouth empty, hands down and out of frame, realistic proportions, no subtitles or "
        f"text anywhere."
    )


_STILL_NEGATIVE = (
    "looking at camera, eye contact with viewer, direct gaze into lens, frontal pose, second person, "
    "two people, studio lighting, id photo, passport photo, stiff pose, floating cutout, mismatched "
    "lighting, extra limbs, distorted face, blurry, watermark, subtitle, caption, logo, text"
)

_LOOK = {"right": "right", "left": "left"}

# The 말투 library lives in the DB (table voice_styles): label, the Qwen3-TTS instruction, the line its
# sample says, the face wording for the video prompt and the Korean re-spelling hint. These are the
# built-in defaults, used only if the table is empty.
def tone_styles() -> list[dict]:
    rows = db.list_voice_styles()
    return rows or [{"key": k, "label": v[1], "face": v[4], "respell": v[0], "instruct": "", "sample_text": ""}
                    for k, v in TONES.items()]


def tone_style(key: str) -> dict:
    return db.get_voice_style(key or "") or {"key": key or "", "label": "", "face": "", "respell": "",
                                             "instruct": "", "sample_text": ""}


TONES = {
    "": ("", "평범하게", 1.0, 1.0, ""),
    "excited": ("신나서 들뜬 말투로, 빠르고 높게", "신남", 1.08, 1.03, "with a bright, excited energy"),
    "annoyed": ("짜증 섞인 말투로, 끝을 늘어뜨리며", "짜증", 0.97, 0.98, "in an annoyed, fed-up way"),
    "tired": ("지치고 귀찮은 말투로, 느리고 낮게", "지침", 0.93, 0.97, "in a tired, reluctant way"),
    "surprised": ("깜짝 놀란 말투로, 높고 급하게", "놀람", 1.1, 1.05, "in a surprised, startled way"),
    "aegyo": ("애교 섞인 말투로, 콧소리를 내며 늘어뜨리며", "애교", 0.98, 1.04, "in a playful, wheedling way"),
    "flat": ("담담하고 무심한 말투로", "무심", 1.0, 0.99, "in a flat, deadpan way"),
}

# Shot size, matched to the original cut. The still is framed to match, so a wide original does
# not come back as a close-up.
FRAMINGS = {
    "closeup": ("A tight close-up, the face filling much of the frame.",
                "얼굴 클로즈업", "head-and-shoulders close-up, the face filling much of the frame"),
    "medium": ("A medium close-up from the chest up.",
               "가슴 위 (미디엄)", "medium close-up from the chest up"),
    # Knees-up, not head-to-toe. A full-body establishing shot matched the original's framing but its
    # tiny faces cost the lip-sync (LTX walked the speaker up to the camera, or the clip had to be made
    # silent and dubbed); lip-sync comes first (user, 2026-09-22). See FULL_BODY_WIDE.
    # Worded as what the still shows. "Wide shot ... plenty of the room" made LTX cut to a wider angle at
    # the end of a knees-up clip (run 78, cut 2).
    "wide": ("A medium-wide two-shot: the people are seen from the knees up.",
             "넓게 (무릎 위)",
             "medium-wide shot, the people seen from the knees up"),
}

# True = wide cuts are full-body establishing shots (zoomed-out still, silent LTX pass + dubbed voice,
# end frame pinned). Off: they are knees-up two-shots rendered with the voice, so the lips sync.
FULL_BODY_WIDE = False


def _pin_end_wide(framing: str) -> bool:
    return FULL_BODY_WIDE and framing == "wide"


def _shot_key(framing: str) -> str:
    """File-name part for a cached still; the knees-up wide still must not reuse a full-body one."""
    return "wideknee" if framing == "wide" and not FULL_BODY_WIDE else framing


# What the camera itself does during the cut. Detected from the original clip, editable after.
CAMERAS = {
    "locked": ("Camera: locked off on a tripod, completely still, no zoom, no pan, no cut.", "고정 (삼각대)"),
    "handheld": ("Camera: handheld with a very slight natural sway, no zoom, no pan, no cut.", "핸드헬드 (살짝 흔들림)"),
    "push_in": ("Camera: slowly pushing in toward the person, a gentle continuous move, no cut.", "천천히 다가가기"),
    "pan_left": ("Camera: panning slowly to the left, following the person, no cut.", "왼쪽으로 팬"),
    "pan_right": ("Camera: panning slowly to the right, following the person, no cut.", "오른쪽으로 팬"),
}

# what the camera/body does; the user picks one per cut
MOTIONS = {
    "": ("", "고정"),
    "lean": ("The person leans in slightly toward the friend while speaking, the face staying three-quarters "
             "toward the camera.", "몸 기울이기"),
    # 2026-09-30: the stills already look at the friend (run 86 look), so "turns toward the friend" turned the
    # head to full profile or away (131 cut 5) and hid the mouth; "step" walked off in 7 of 9
    # 2026-10-02: with the stills now turned ~35 degrees to the friend, any head TURN (toward the friend, or
    # "back toward the camera and then toward the friend") ended on the back of the head (runs 148, 150 cut 4,
    # same seed); a tilt kept the face three-quarters to the end (run 152), as did no motion (153)
    "turn": ("The person tilts the head slightly to one side and back, the face staying three-quarters toward "
             "the camera, on the same spot.", "고개 갸웃하기"),
    "step": ("The person gently rocks the shoulders from side to side, feet planted, the face staying "
             "three-quarters toward the camera.", "몸 흔들기"),
    "zoom": ("The handheld camera drifts slowly closer, a gentle push-in.", "카메라 천천히 다가가기"),
    "shake": ("The handheld camera sways and wobbles a little more than usual.", "카메라 흔들림"),
}


# the action already says where the person looks ("looks up at the shelves", "glances at the counter"):
# then that wins over the default "eyes on the friend" (user 2026-10-01: look where the situation says,
# not just at the camera)
_GAZE = re.compile(r"\b(look|looks|looking|glance|glances|glancing|gaze|gazes|stare|stares|staring|watch|watches|"
                   r"eyes? (?:on|at|toward|towards|to|down|up|off)|peeks?)\b", re.I)

# A held mouth shape in the action ("holds her mouth slightly agape in shock", "parts his lips") fights the voice:
# LTX kept the mouth hanging open after the line ended (run 147 cut 4, runs 120-125 cuts 4-5). On speaking cuts
# the voice decides the mouth, so those phrases are dropped and the rest of the gesture kept.
_MOUTH_HOLD = re.compile(
    r"(,\s*|\s+and\s+)?\b(?:holds? (?:their|his|her) mouth slightly (?:agape|open)|opens? (?:their|his|her) mouth "
    r"slightly|(?:with )?(?:the |their |his |her )?mouth slightly (?:agape|open)|parts? (?:their |his |her )?lips"
    r"(?: slightly)?|lips part)\b(\s+and\s+)?", re.I)


# The friend is off-screen. Actions that name "the friend on the right" or touch the friend made LTX bring a second
# person into the frame (30 s run 185 cut 3: a stranger's face at the edge) or turn the speaker around to reach
# them (cut 14 "nudges her friend's shoulder"); "looks toward the friend on the left" on a still that already faces
# the friend turned her further, to the back of the head (cuts 5, 18). 2026-10-06.
_TOUCH_FRIEND = re.compile(r"\b(nudg|tap|pat|pok|grab|touch|hug|punch|push|shov|elbow|slap|hit)\w*\s+(her|his|their|the)\s+"
                           r"friend'?s?(\s+(on the\s+)?(shoulder|arm|back|hand|side)s?)?", re.I)
_FRIEND_SIDE = re.compile(r"\b(the|her|his|their)\s+friend\s+(on|to)\s+the\s+(left|right)\b", re.I)
# "looks away" / "turns away" ended with the back of the head to the camera (30 s run 194 cut 19, 2026-10-06)
_LOOK_AWAY = re.compile(r"\b(look|turn|glanc)\w*\s+away(\s+from\s+(the|her|his|their)\s+\w+)?\b", re.I)
_LOOK_AT_FRIEND = re.compile(r"\b(and\s+|then\s+|while\s+|as\s+(she|he|they)\s+)?(look|glanc|turn|gaz|star)\w*\s+(\w+\s+){0,3}(at|toward|towards|to)\s+"
                             r"(the|her|his|their)\s+friend(\s+just\s+off-screen(\s+to\s+the\s+(left|right))?)?", re.I)


def _tidy_action(action: str) -> str:
    action = _TOUCH_FRIEND.sub("gestures playfully toward the friend", action)
    action = _FRIEND_SIDE.sub("the friend", action)
    action = _LOOK_AWAY.sub("glances aside for a moment", action)
    # the still already faces the friend: "looks toward the friend" only turned the head further away
    out = _LOOK_AT_FRIEND.sub("", action)
    out = re.sub(r"\s{2,}", " ", re.sub(r"\s+,", ",", out)).strip(" ,")
    return out or action


_HANDS = re.compile(r"\b(hands?|arms?|palms?|fingers?|fists?|thumbs?|wrists?|elbows?|claps?|clapping|waves?|waving|"
                    r"points?|pointing|gestures?|gesturing|beckons?|salutes?|covers?)\b", re.I)


def closeup_action(action: str, pron: str = "they") -> str:
    """A face close-up has no room for hands: "waves her hands broadly" put a hand right over the mouth for the whole
    line (run 200 cut 8, 2026-10-07) although the paragraph said the hands stay below the frame -- LTX follows the
    action line. Keep the parts about the face, head and body; drop the hand / arm ones (but keep their expression)."""
    if not action:
        return action
    parts = [re.sub(r"^(and|then|while)\s+", "", x.strip()) for x in
             re.split(r",\s*|\s+and\s+|\s+while\s+|\s+then\s+", action) if x.strip()]
    parts = [x for x in parts if x]
    keep = [x for x in parts if not _HANDS.search(x)]
    if len(keep) == len(parts):
        return action
    poss = {"she": "her", "he": "his"}.get(pron, "their")
    # "presses palms together with a cute facial expression" -> the expression stays
    face = next((m.group(0) for x in parts if _HANDS.search(x)
                 for m in [re.search(r"with (a|an) [\w\s-]*?(expression|look|smile|grin|face)", x)] if m), "")
    if not keep:
        keep = [f"lifts {poss} chin and nods expressively"]
    out = " and ".join(keep)
    return f"{out} {face}".strip() if face and face not in out else out


def _drop_mouth_hold(action: str) -> str:
    def repl(m: re.Match) -> str:
        return " and " if (m.group(1) or "").strip() == "and" and m.group(2) else ""
    out = _MOUTH_HOLD.sub(repl, action)
    return re.sub(r"\s{2,}", " ", out).strip(" ,")


def prompt_parts(cut: dict, cast: dict | None, character: dict | None, setting: str,
                 photo_frame: bool = False, hold: bool = False, alone: bool = False) -> list[dict]:
    """The prompt in LTX-2.3's own shape -- a describing paragraph, then scene / character / action /
    audio / camera lines. Each part carries the Korean label and note the page explains it with."""
    side = _LOOK.get(cut.get("look"), "right")
    pron = _pron(character)
    subject = pron.capitalize()
    costume = (cast or {}).get("costume") or "casual everyday clothes"
    line = (cut.get("line") or "").strip()
    action = (cut.get("action") or "").strip()
    # the gesture was described from the ORIGINAL performer ("widens his eyes" on a woman's cut)
    if pron == "she":
        action = re.sub(r"\bhimself\b", "herself", re.sub(r"\b(his|him)\b", "her", action))
    elif pron == "he":
        action = re.sub(r"\bherself\b", "himself", action)
    action = _tidy_action(action)
    if line:
        action = _drop_mouth_hold(action)
    dur = round(cut["end_sec"] - cut["start_sec"], 1)
    framing = cut.get("framing") or "medium"
    if framing == "closeup" and not photo_frame:
        action = closeup_action(action, pron)
    shot_phrase = FRAMINGS.get(framing, FRAMINGS["medium"])[2]
    two = (cut.get("people") or 1) >= 2
    tone_key = cut.get("tone") or ""
    tone_phrase = tone_style(tone_key).get("face") or ""
    motion_key = cut.get("motion") or ""
    motion_phrase = MOTIONS.get(motion_key, MOTIONS[""])[0]
    # a place cleaned up by ✨ is one sentence ending in a period ("A rooftop terrace ..., night atmosphere."): it
    # went into the prompt as "is alone in A rooftop ... atmosphere., speaking" (run 194)
    place = (setting or DEFAULT_SETTING).strip().rstrip(".")
    cam = CAMERAS.get(cut.get("camera") or "locked", CAMERAS["locked"])
    if photo_frame:
        # "원본 사진으로 바로": the first frame is the bare photo (plain backdrop). Describing the cafe
        # and a wide shot made LTX hard-cut from the photo to a cafe scene halfway through the clip.
        place = "the same place as in the first frame, its background unchanged"
        shot_phrase = "shot that keeps exactly the framing of the first frame"
        framing, two = "medium", False

    speaks = (
        f"{subject} speaks one short line out loud, lips moving in exact time with the voice in the audio and "
        f"closing the moment it stops"
        if line else
        f"{subject} says nothing and reacts with the face only, mouth closed"
    )
    if two:
        obj = {"he": "him", "she": "her"}.get(pron, "them")
        # Both people are already in the first frame. "a friend just off-screen" contradicted that, and in
        # a wide two-shot (run 72, cut 1) LTX walked a second, larger copy of the speaker into the
        # foreground to have a face for the voice.
        # Written as what happens, not what must not: LTX ignores "nobody walks in" (CFG 1), and in runs
        # 83/84 the two-person cuts ended with one of them walking off or the camera pushing onto one face.
        # Who speaks is told by who they are, not by left/right: the shared two-person still is mirrored
        # for cuts that look the other way.
        cast2 = db.get_follow_cast(cut["cast_id2"]) if cut.get("cast_id2") else None
        ch2 = db.get_character(cast2["character_id"]) if cast2 and cast2.get("character_id") else None
        kind = lambda ch: {"male": "man", "female": "woman"}.get((ch or {}).get("gender"), "person")  # noqa: E731
        listener = f"the {kind(ch2)}" if kind(ch2) != kind(character) else "the friend"
        paragraph = (
            f"A {dur}-second {shot_phrase} from a candid drama scene filmed on a cinema camera, showing two "
            f"people standing face to face in {place.split(':')[0]}: {_who(character)} and {_who(ch2)}. Both of "
            f"them stay in the frame from the first frame to the last, standing on the same spot and moving "
            f"only their heads, faces and hands. {speaks}, looking at {listener} with the face turned enough "
            f"that the mouth stays clearly visible. {listener.capitalize()} listens quietly with the mouth "
            f"closed. The camera keeps both of them in the frame."
        )
    else:
        obj = {"he": "him", "she": "her"}.get(pron, "them")
        # The wording of the user's favourite video ("24_캐스팅그대로", run 87, 2026-09-22), chosen as the
        # recommendation on 2026-09-29. (Run 93 on 09-23 once walked a second copy of the speaker in with
        # "a friend just off-screen" and this had been reworded; the 24 wording rendered 39 of 39 single
        # cuts cleanly in the 20/30s tests of 2026-09-29.) Face mostly toward the camera, eyes on the
        # friend beside it: "keeps looking off-camera" turned heads to profile and the lips hid (09-22).
        if alone and not photo_frame:
            # a place the viewer chose / a wide single: "talking with a friend standing just off-screen" made
            # LTX walk that friend through the frame (runs 132/133 cut 5, 2026-09-30) -- nobody is mentioned
            spot = re.sub(r"^(A|An|The)\b", lambda m: m.group(1).lower(), place.split(':')[0].split(",")[0].strip())
            spot = spot if spot.lower().startswith(("inside ", "in ", "at ", "on ")) else f"in {spot}"
            where = f"{subject} is alone {spot}, speaking toward the {side} side"
            eyes = f"the eyes look off-camera to the {side}"
            if action and _GAZE.search(action):
                eyes = "the eyes go wherever the action below says"
        else:
            where = f"{subject} is in {place.split(':')[0]}, talking with a friend standing just off-screen to the {side}"
            eyes = "the eyes glance at the friend standing just beside the camera"
            if action and _GAZE.search(action):
                eyes = "where the eyes go follows the action below, and between lines they come back to the friend"
        # the gesture said in the paragraph too, not only on the action line (test flag, 2026-10-06: gestures were
        # small in the gym run 189 with guide 0.5)
        # A silent cut said "While talking" too (run 194 cut 23). In a close-up, arm gestures filled the frame and
        # covered or blurred the face (run 194 cuts 9, 17, 21): there the gesture is carried by the head, face and
        # shoulders, the hands kept below the frame.
        lead = "While talking" if line else f"As {pron} reacts"
        how = ("shown with the head, face and shoulders while the hands stay low, below the bottom of the frame, so "
               "the face is never covered or blurred" if framing == "closeup" else "a clear, visible movement")
        gesture = (f" {lead}, {pron} {action.rstrip('.')}, {how}."
                   if action and _tuning().get("action_in_paragraph") else "")
        paragraph = (
            f"A {dur}-second {shot_phrase} from a candid drama scene filmed on a cinema camera. {where}. {speaks}."
            f"{gesture} "
            f"{subject} keeps the face turned mostly toward the camera, only slightly to the {side}, so the mouth "
            f"stays clearly visible; {eyes}."
            # Plain single cuts keep run 86 / video 24's wording exactly. Where a cut is held in place (wide single,
            # "몸 흔들기", a place the viewer chose, photo mode -- see _end_guide) one more sentence says so:
            # without it the friend walked into 6 of 9 wide shots (2026-09-30). Said as what happens; LTX
            # ignores "nobody enters".
            + ((" " + (
                ((f"From the first frame to the last {pron} stays standing on the same spot, "
                  f"{'talking' if line else 'reacting'} with the head, face and shoulders, which move freely and "
                  f"expressively, the face always sharp and fully in view; "
                  if framing == "closeup" else
                  f"From the first frame to the last {pron} stays standing on the same spot, "
                  f"{'talking' if line else 'reacting'} with the whole "
                  f"upper body -- head, shoulders, arms and hands move freely and expressively; ")
                 if _tuning().get("hold_relaxed") else
                 f"From the first frame to the last {pron} stays standing on the same spot, moving only the head, "
                 f"face and hands; ")
                + (f"{pron} is alone in front of the same plain, empty studio backdrop as in the first frame, "
                   f"filling the frame exactly as in the first frame."
                   if photo_frame else
                   f"{pron} is the only person near the camera, and everyone else stays small and far away in the "
                   f"background, never beside {obj}."))) if (hold or photo_frame) else "")
        )

    parts = [
        ("paragraph", "장면 설명", "LTX는 먼저 짧은 문단으로 전체 그림을 받습니다. 누가·어디서·무엇을 하는지 한 번에 적습니다.",
         paragraph),
        ("scene", "scene (장소)", "장소를 한 줄로. 모든 컷에 같은 문장을 써야 배경이 이어집니다.",
         f"scene: {place}, shallow depth of field" + (", no cut to another place" if photo_frame else "")),
        ("character", "character (인물)", "얼굴은 넣어 주는 사진이 정하고, 여기에는 머리·옷차림을 적습니다.",
         # "원본 사진으로 바로": the photo decides the look. Describing the role's outfit here made LTX hard-cut
         # to a different person who matched it (4 of 8 cuts, 2026-09-30).
         "character: the same person as in the first frame, with exactly the same face, hair and clothes"
         if photo_frame else
         f"character: {_who(character)}, {costume}" + (", and the friend standing with them in the frame" if two else "")),
        ("action", "action (행동)", "몸짓·표정·말투를 한 줄로. 한 컷에는 움직임 하나만 넣어야 자연스럽습니다.",
         "action: " + ", ".join(x for x in [
             "mouth opening and closing clearly with every syllable" if line and not _pin_end_wide(framing) else "",
             (action.rstrip(".") + (" while staying on the same spot" if two else "")) if action
             else "one small natural movement only",
             tone_phrase.replace("with a ", "").replace("in a ", "").replace("in an ", "").strip() if tone_phrase else "",
             motion_phrase.rstrip(".").replace("The person ", "").replace("The handheld camera ", "camera ") if motion_phrase else "",
             "hands stay away from the face",
             # 2026-09-23 two-person shots: smooth movement and gestures close to the original (not in the
             # single-person 24 wording, which is the recommendation since 2026-09-29)
             "the movement is smooth and continuous from start to end, no sudden jumps" if two else "",
         ] if x)),
        ("audio", "audio (소리·립싱크)", "입 모양은 함께 넣는 음성 파일이 맞춥니다. 대사 글자를 프롬프트에 적으면 AI가 자막으로 그려 버립니다.",
         "audio: one voice speaking; the lips follow that voice exactly and stay closed when it stops"
         + (", the other person stays silent" if two else "")),
        ("camera", "camera (카메라)", "원본 컷의 카메라 움직임을 재서 같은 값을 적습니다.",
         "camera: " + cam[0].replace("Camera: ", "").rstrip(".") + f", {shot_phrase}"
         + (", both people in the frame for the whole shot" if two else "")),
    ]
    return [{"key": k, "label": lab, "note": note, "text": text} for k, lab, note, text in parts]


def join_prompt(parts: list[dict]) -> str:
    """Paragraph first, then the labelled lines -- the structure LTX-2.3's own examples use.

    With Korean speech in the audio, the word "Korean" (and even "no subtitles" -- CFG 1 cannot
    negate) made LTX burn in subtitle-like text, so both stay out of the prompt.
    """
    by_key = {p["key"]: p["text"] for p in parts}
    head = by_key.pop("paragraph", "")
    body = "\n".join(by_key[k] for k in ("scene", "character", "action", "audio", "camera") if k in by_key)
    extra = "\n".join(v for k, v in by_key.items() if k not in ("scene", "character", "action", "audio", "camera"))
    out = head + "\n\n" + body + ("\n" + extra if extra else "")
    return out.replace("Korean ", "")



# ---------------------------------------------------------------- rendering

def line_char_limit(cut: dict, voice: dict | None) -> int:
    cps = (voice or {}).get("observed_chars_per_sec") or _DEFAULT_CPS
    return max(2, int((cut["end_sec"] - cut["start_sec"]) * _MAX_SPEEDUP * cps * 0.9))


def _wide_canvas() -> Path:
    path = FOLLOW_DIR / "canvas_16x9.png"
    if not path.exists():
        FOLLOW_DIR.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1344, 756), (128, 128, 128)).save(path)
    return path


_FACE_CLEAN_PROMPT = (
    "Close-up passport-style headshot of the SAME person from image1: exactly the same face, face shape, eyes, "
    "eyebrows, nose, lips, skin and age, facing the camera with a neutral relaxed expression and a closed empty "
    "mouth. Plain light-grey background, plain grey t-shirt, soft even light. Nothing near the face: no hands, "
    "no objects, no hat."
)


def face_reference(character: dict) -> Path:
    """A clean headshot of the character, made once per photo and cached.

    Library photos are whole snapshots -- an orange t-shirt, a V-sign, a tattoo, a cigarette in the mouth --
    and the costume still copied all of it (B kept his orange shirt, C kept the cigarette, E kept the V-sign
    and lost the cap). Crop to the head, then one Qwen pass that keeps the face and strips everything else."""
    faces = FOLLOW_DIR / "faces"
    faces.mkdir(parents=True, exist_ok=True)
    stem = Path(character["image_path"]).stem
    dest = faces / f"char{character['id']}_{stem}.png"
    if dest.exists():
        return dest
    im = Image.open(MEDIA_DIR / character["image_path"]).convert("RGB")
    w, h = im.size
    side = int(min(w, h * 0.6))
    left, top = (w - side) // 2, int(h * 0.02)
    crop = faces / f"crop_{dest.name}"
    im.crop((left, top, left + side, top + side)).save(crop)

    graph = copy.deepcopy(_COMPOSE_TEMPLATE)
    img = comfy_client.copy_to_comfy_input(crop)
    for node in ("41", "83", "195"):
        graph[node]["inputs"]["image"] = img
    graph["170:151"]["inputs"]["prompt"] = _FACE_CLEAN_PROMPT
    graph["170:149"]["inputs"]["prompt"] = _STILL_NEGATIVE
    graph["170:169"]["inputs"]["seed"] = int(hashlib.sha256(
        graph["170:151"]["inputs"]["prompt"].encode("utf-8")).hexdigest()[:12], 16)
    graph["170:168"]["inputs"]["value"] = True
    outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
    comfy_client.save_output_to(outputs, dest, media_keys=["images", "image"])
    crop.unlink(missing_ok=True)
    return dest


def face_tile(character: dict) -> Path:
    """The clean headshot cut down to the face: no t-shirt, no shoulders, little hair below the chin.

    The headshot itself is made in a plain grey t-shirt, and on 2026-09-22 that grey shirt came back in
    the stills instead of the costume's black one (and a long-haired headshot beat a "bob haircut"
    costume). What the still should copy is the face only, so that is all it is shown."""
    head = face_reference(character)
    dest = head.with_name(f"{head.stem}_tile.png")
    if not dest.exists():
        im = Image.open(head).convert("RGB")
        w, h = im.size
        im.crop((int(w * 0.16), int(h * 0.02), int(w * 0.84), int(h * 0.76))).save(dest)
    return dest


def _zoom_prompt(setting: str, people: int) -> str:
    who = ("the same two people -- same faces, same hair, same clothes --" if people >= 2 else
           "the same single person -- same face, same hair, same clothes --")
    only = ("" if people >= 2 else
            " There is only this one person in the foreground; do not add a copy of them anywhere.")
    return (
        f"Zoom the camera far out. Show the same scene from much farther back across the room: {who} now small "
        f"in the middle distance and seen head to toe, legs and shoes visible, standing on the floor.{only} Most "
        f"of the 16:9 frame is now the room around them -- {setting or DEFAULT_SETTING}. Same lighting, same "
        f"poses and gaze directions, nobody looks at the camera, no text.")


# one person: first a few steps back to waist-up, then the far zoom. Straight from a chest-up still the
# far zoom failed 3 of 3 tries (it either changed nothing or added a small copy of the person).
_STEP_BACK_PROMPT = (
    "Pull the camera back a few steps: show the same single person -- same face, hair and clothes -- from the "
    "waist up in the middle of the frame, with more of the room visible around them. Only this one person in "
    "the foreground, no copies. Same lighting, same gaze direction, not looking at the camera, no text.")


def _qwen_edit(src: Path, prompt: str, dest: Path, seed: int | None = None) -> Path:
    """One Qwen edit of a finished 16:9 still (the still is image1, 2 and 3)."""
    name = comfy_client.copy_to_comfy_input(src)
    graph = copy.deepcopy(_COMPOSE_TEMPLATE)
    for node in ("41", "83", "195"):
        graph[node]["inputs"]["image"] = name
    graph["170:151"]["inputs"]["prompt"] = prompt
    graph["170:149"]["inputs"]["prompt"] = _STILL_NEGATIVE
    graph["170:169"]["inputs"]["seed"] = seed if seed is not None else int(hashlib.sha256(
        graph["170:151"]["inputs"]["prompt"].encode("utf-8")).hexdigest()[:12], 16)
    graph["170:168"]["inputs"]["value"] = True
    graph["900"] = {"class_type": "LoadImage", "inputs": {"image": comfy_client.copy_to_comfy_input(_wide_canvas())}}
    graph["901"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["900", 0]}}
    graph["170:156"]["inputs"]["pixels"] = ["901", 0]
    outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
    comfy_client.save_output_to(outputs, dest, media_keys=["images", "image"])
    return dest


def _foreground_people(img: Path, free: bool = True) -> dict | None:
    """What gemma sees in a still: {"people": close to the camera, "twice": same person twice, "eyes": both
    eyes of the main person visible (False = side profile / turned away)}; None when it gave no answer.
    People count read correctly on 6 of 6 test stills, the profile on 1 of 1 (2026-09-30)."""
    import base64

    import httpx

    from app.config import OLLAMA_MODEL, OLLAMA_URL
    try:
        # Qwen (19 GB) is still resident after the still job: loading gemma on top of it, then LTX, ran the PC
        # out of memory and took ComfyUI down (2026-09-30 22:31). Free ComfyUI first, as every other gemma
        # call during a render does.
        if free:
            comfy_client.free_vram()
        r = ollama_client.gemma_post(f"{OLLAMA_URL}/api/generate", timeout=240, json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json",
            # hand the card straight back to ComfyUI -- unless more pictures are checked right after (a batch)
            "keep_alive": 0 if free else "1m",
            "options": ollama_client._gpu_options({"temperature": 0}),
            "prompt": "Look at this photo. Answer JSON only: {\"foreground_people\": <number of people standing "
                      "close to the camera; ignore small blurred people far in the background>, "
                      "\"same_person_twice\": true|false, \"both_eyes_visible\": true|false (for the main "
                      "person: can you see BOTH of their eyes? false if the head is in side profile or turned "
                      "away), \"looking_into_camera\": true|false (true if the main person's eyes look straight "
                      "into the camera lens at the viewer; false if they look to the side, past the camera, or at "
                      "someone or something else), \"other_person_near\": true|false (true if ANY part of a "
                      "second person -- a shoulder, an arm, a back, the back or side of a head, a body seen from "
                      "behind -- is visible close to the camera or cut off by the left or right edge of the "
                      "photo, or a second person stands large in the middle distance; false if the only other "
                      "people are small and far away in the background), \"image_broken\": true|false (true if "
                      "the picture is corrupted or glitched: scrambled rectangular pixel blocks, stripes, heavy "
                      "digital noise or a face broken into pieces; false for a normal clean photo, even if the "
                      "background is blurred), \"head_turn\": \"frontal\"|\"three-quarter\"|\"profile\" (main "
                      "person; frontal = the face points straight at the camera, both cheeks equally visible; "
                      "three-quarter = clearly turned to one side, one cheek more visible; profile = side view)}",
            "images": [base64.b64encode(img.read_bytes()).decode()]})
        data = json.loads(r.json().get("response", "{}"))
        return {"people": int(data.get("foreground_people")), "twice": bool(data.get("same_person_twice")),
                "eyes": bool(data.get("both_eyes_visible", True)),
                "camera": bool(data.get("looking_into_camera", False)),
                "near": bool(data.get("other_person_near", False)),
                "broken": bool(data.get("image_broken", False)),
                "turn": str(data.get("head_turn", "three-quarter"))}
    except Exception:  # noqa: BLE001 - no answer: keep the picture rather than fail the cut
        return None


_STILL_TRIES = 4
_STILL_FULL_SCORE = 5


# finished still files are put in place / copied to the shared cache under this lock (two threads can make the
# same still at once: the ahead-of-time pass and a render)
_STILL_FILES = threading.RLock()


def _still_score(seen: dict | None, people: int) -> int:
    """How good a still is, 0-5 (-1 = broken picture), from what gemma saw in it."""
    if seen is None:
        return _STILL_FULL_SCORE  # could not be checked: as good as a pass
    # the 4-step Qwen sometimes returns a picture scrambled into pixel blocks (edge test 2026-10-01, 1 of 18);
    # gemma called it broken and 5 clean stills fine
    if seen["broken"]:
        return -1
    # 2026-10-01: Qwen often drew the speaker staring into the lens (user: "not just looking at the camera --
    # look where the situation says"); gemma judged 6 of 6 stills the way a person would. A stranger's back or
    # shoulder at the frame edge (single stills) became a face in the video (run 143 cut 6), so it counts as a
    # wrong people count. A straight-on single still made LTX talk into the lens for the whole cut (run 146 cuts
    # 6, 8) even when the eyes in the still looked aside.
    single = people == 1
    alone_ok = not single or not seen["near"]
    return ((2 if seen["people"] == people and not seen["twice"] and alone_ok else 0)
            + (1 if not single or seen["eyes"] else 0) + (0 if seen["camera"] else 1)
            + (1 if not single or seen["turn"] == "three-quarter" else 0))


# Drawn into a background PHOTO, Qwen kept the reference's straight-on face: karaoke run 154 had 4 of 6 stills
# looking into the lens after all 4 tries. Editing the finished still -- head and eyes only -- turns it away with
# the place and face kept. The angle matters: "25-35 degrees" made close-ups side profiles (0 of 6 usable), "only
# the eyes" kept them staring (0 of 6); "slightly, about 15 degrees ... almost from the front" gave 4 of 6 perfect
# and 5 of 6 three-quarter (karaoke/store close-ups + a medium, 2026-10-02).
_GAZE_FIX_PROMPT = (
    "Keep this exact photo: the same person with exactly the same face and identity, the same hair, clothes, "
    "place, framing and light. Change only the head and the eyes: the head turns slightly, about 15 degrees, "
    "toward the RIGHT side of the frame, so the face is still seen almost from the front with both eyes, the nose "
    "and the whole mouth clearly visible; the eyes look to the right, past the right edge of the picture, clearly "
    "away from the camera. Nobody else is added.")
_GAZE_FIX_TRIES = 2

# A single still with a stranger standing beside the person: in the rainy cafe of 20 s run 197 (2026-10-06) Qwen drew
# a second young man next to the speaker in all 3 drawings, the "least bad" one was used, and the cut showed two
# people looking into the lens. Removing the extra person from the best drawing is one picture job, and a new
# drawing in that place only drew another.
_ALONE_PROMPT = (
    "Keep this exact photo, but with only one person in it: {who}, the person who is largest and nearest to the "
    "camera. Remove every other person completely and fill the space where they were with the same background, so "
    "it looks as if nobody else was ever there. Keep that person's face, identity, hair, clothes, pose, position, "
    "the place, the framing and the light exactly the same. Nobody else is added.")


def _draw_checked(graph: dict, dest: Path, people: int) -> dict | None:
    """Run the still graph up to _STILL_TRIES times and keep the best picture: the right number of people,
    nobody twice, and (single shots) both eyes showing. Qwen sometimes draws the speaker twice, a stray figure
    behind them, or a full side profile (runs 133, 2026-09-30); wording alone did not stop it, so every new
    still is looked at. Stills are cached per person + place, so this is paid once."""
    seed = graph["170:169"]["inputs"]["seed"]
    best = None  # (score, path, seen)
    # straight after an LTX job the first Qwen still died with cudaErrorMemoryAllocation and ComfyUI hung until it
    # was killed (2026-10-01 23:19); hand ComfyUI an empty card before switching to Qwen
    comfy_client.free_vram()

    def consider(path: Path, seen: dict | None) -> int:
        nonlocal best
        score = _still_score(seen, people)
        if best is None or score > best[0]:
            if best:
                best[1].unlink(missing_ok=True)
            best = (score, path, seen)
        else:
            path.unlink(missing_ok=True)
        return score

    def check_all(paths: list[Path]) -> bool:
        """gemma looks at a batch; True as soon as one is perfect (the rest are dropped unchecked)."""
        for i, p in enumerate(paths):
            if consider(p, _foreground_people(p)) >= _STILL_FULL_SCORE:
                for rest in paths[i + 1:]:
                    rest.unlink(missing_ok=True)
                return True
        return False

    # Drawn two at a time, then both checked: every gemma check pushes the 20 GB Qwen model off the card and the
    # next drawing loads it again (each Qwen job took ~45 s, most of it loading; 16 jobs for the 6 stills of a
    # new place = 16 minutes, 2026-10-02). Pairs halve the reloads.
    def gaze_only(b) -> bool:
        """Right person, right count, nobody else -- only the face points the wrong way."""
        s_ = b[2]
        return bool(people == 1 and s_ and b[0] >= 0 and s_["people"] == 1 and not s_["twice"] and not s_["near"]
                    and (s_["camera"] or s_["turn"] != "three-quarter"))

    # A still that is right except for where the face points gets its head and eyes turned (an edit of the same
    # picture) before any new drawing. In a place from a background photo nearly every drawing faced the lens and
    # every still took all 6 jobs (분식집 run, 2026-10-03: 5 min each); now 1 drawing + 2 edits when that happens.
    attempt, done, fix_round = 0, False, 0
    # The same still can be made by the ahead-of-time pass and by a render at once (run 169, 2026-10-03: both used
    # the same temp names and one renamed the other's file away -> "file not found"). Own names per call, and
    # whoever finishes second just finds the still there.
    tag = uuid4().hex[:6]
    for size in (1, 2, 1):
        if attempt >= _STILL_TRIES or done:
            break
        if dest.exists():  # made meanwhile by the other one
            if best:
                best[1].unlink(missing_ok=True)
            return
        batch = []
        for _ in range(size):
            graph["170:169"]["inputs"]["seed"] = (seed + attempt * 7919) % 2 ** 48
            outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
            tmp = dest.with_name(f"{dest.stem}_try{attempt}_{tag}.png")
            comfy_client.save_output_to(outputs, tmp, media_keys=["images", "image"])
            batch.append(tmp)
            attempt += 1
        done = check_all(batch)
        if not done and fix_round < 2 and gaze_only(best):
            # one edit, checked; a second only if needed (the 15-degree edit was right 4 of 6 times on its own)
            for k in range(_GAZE_FIX_TRIES):
                src = best[1]  # the best so far (the drawing, or a first edit that already improved it)
                fixed = dest.with_name(f"{dest.stem}_gaze{fix_round}{k}_{tag}.png")
                _qwen_edit(src, _GAZE_FIX_PROMPT, fixed, seed=(seed + 104729 * (k + 1 + 2 * fix_round)) % 2 ** 48)
                done = check_all([fixed])
                if done or not gaze_only(best):
                    break
            fix_round += 1
    if best[0] < 0:
        best[1].unlink(missing_ok=True)
        raise RuntimeError("인물 사진이 깨져서 나왔어요 (4번 다시 그려도 같음). 잠시 뒤 다시 만들어 주세요")
    with _STILL_FILES:
        if dest.exists():  # the other one finished first: keep that picture
            best[1].unlink(missing_ok=True)
            return None
        best[1].replace(dest)
    return best[2]


user_busy = lambda: False  # noqa: E731 -- set by app/prefetch.py: the viewer used ✨ in the last few seconds


def _draw_many(items: list[dict], progress=None, note=None, cancelled=None) -> None:
    """Several new stills at once, model by model: every first drawing with the picture model loaded once, then all
    checked with gemma loaded once, then the head-and-eyes fixes together, and so on. One still at a time swapped the
    20 GB picture model out for gemma and back for every picture -- 45 s per drawing instead of ~27 s, and 12 min for
    the 6 stills of a new place (2026-10-06). Each item: graph, dest, people, finish(). Same rules as
    _draw_checked; a still that ends up broken is left undrawn (its cut draws it again on its own)."""
    def gaze_only(it) -> bool:
        b = it["best"]
        if not b or b[0] < 0 or it["people"] != 1:
            return False
        s_ = b[2]
        return bool(s_ and s_["people"] == 1 and not s_["twice"] and not s_["near"]
                    and (s_["camera"] or s_["turn"] != "three-quarter"))

    def extra_person(it) -> bool:
        b = it["best"]
        s_ = b[2] if b else None
        return bool(s_ and b[0] >= 0 and it["people"] == 1 and (s_["people"] > 1 or s_["twice"] or s_["near"]))

    def consider(it, path: Path, seen) -> None:
        score = _still_score(seen, it["people"])
        if it["best"] is None or score > it["best"][0]:
            if it["best"]:
                it["best"][1].unlink(missing_ok=True)
            it["best"] = (score, path, seen)
        else:
            path.unlink(missing_ok=True)
        it["done"] = it["best"][0] >= _STILL_FULL_SCORE

    def check(made: list) -> None:
        if not made:
            return
        comfy_client.free_vram()  # the picture model out once, gemma in once
        for it, path in made:
            consider(it, path, _foreground_people(path, free=False))

    for it in items:
        it.update(best=None, done=False, tries=0, fixes=0, alone=0, tag=uuid4().hex[:6],
                  seed0=it["graph"]["170:169"]["inputs"]["seed"])
    comfy_client.free_vram()
    # Each round: every picture job of the round with the picture model loaded once (new drawings for wrong
    # pictures AND head-and-eyes fixes together), then all checks with gemma loaded once. Drawings and fixes used to
    # be separate rounds -> 6 picture jobs + 4 model swaps for 3 people in a new place (8 min, 2026-10-06). At most
    # 3 rounds; a picture that is only off in where it looks is kept after 2 fixes instead of drawing again.
    # the viewer is pressing ✨ right now: each clean-up made the 20 GB picture model leave the card and come back
    # (+15-20 s per picture; beach run 191: pictures 5 min instead of 2, clean-ups 118 s instead of 15). Let the
    # clean-ups go first while they keep coming, then draw. Checked before EVERY picture job: checking only at the
    # start of a round still let 5 pictures of one round and 4 clean-ups take turns (30 s run 194: 229 s of ✨).
    waited = [0.0]

    def let_user_go() -> None:
        # also wait while something else holds the GPU: a picture job queued behind a ✨ clean-up went in the moment
        # it ended, and the viewer's next ✨ then waited for that whole picture
        t0 = time.time()
        while (user_busy() or comfy_client._comfy_lock.locked()) and waited[0] + time.time() - t0 < 240:
            time.sleep(1)
        waited[0] += time.time() - t0

    stop = False  # the run moved on (another place / person picked): leave the rest undrawn
    for _round in range(3):
        if cancelled and cancelled():
            stop = True
            break
        work = []
        for it in items:
            if it["done"] or it["dest"].exists():
                continue
            if it["best"] is not None and gaze_only(it):
                if it["fixes"] < 2:
                    work.append(("fix", it))
            elif it["best"] is not None and extra_person(it) and it["alone"] < 1:
                work.append(("alone", it))
            elif it["tries"] < _STILL_TRIES:
                work.append(("draw", it))
        if not work:
            break
        made = []
        for k, (kind, it) in enumerate(work):
            let_user_go()
            if cancelled and cancelled():
                stop = True
                break
            if note:  # what this picture job is, for the "만드는 중" bar (5 min of "준비 중" looked stuck)
                done_n = sum(1 for x in items if x["done"])
                what = {"draw": "그리는 중", "fix": "시선 다듬는 중", "alone": "옆 사람 지우는 중"}[kind]
                note(f"인물 사진 그리는 중 ({done_n}/{len(items)}장 완성) · {_round + 1}번째 {what} {k + 1}/{len(work)}")
            if kind == "draw":
                g = it["graph"]
                g["170:169"]["inputs"]["seed"] = (it["seed0"] + it["tries"] * 7919) % 2 ** 48
                outputs = comfy_client.run_prompt_and_wait(g, timeout=600, poll_interval=2)
                tmp = it["dest"].with_name(f"{it['dest'].stem}_try{it['tries']}_{it['tag']}.png")
                comfy_client.save_output_to(outputs, tmp, media_keys=["images", "image"])
                it["tries"] += 1
            elif kind == "alone":
                tmp = it["dest"].with_name(f"{it['dest'].stem}_alone_{it['tag']}.png")
                _qwen_edit(it["best"][1], _ALONE_PROMPT.format(who=it.get("who") or "the main person"), tmp,
                           seed=(it["seed0"] + 15485863) % 2 ** 48)
                it["alone"] += 1
            else:
                tmp = it["dest"].with_name(f"{it['dest'].stem}_gaze{it['fixes']}_{it['tag']}.png")
                _qwen_edit(it["best"][1], _GAZE_FIX_PROMPT, tmp,
                           seed=(it["seed0"] + 104729 * (it["fixes"] + 1)) % 2 ** 48)
                it["fixes"] += 1
            made.append((it, tmp))
        if stop:
            for _it, tmp in made:
                tmp.unlink(missing_ok=True)
            break
        check(made)
        if progress:
            progress(sum(1 for it in items if it["done"]), len(items))
    for it in items:
        b = it["best"]
        if not b:
            continue
        if b[0] < 0 or (stop and not it["done"]):  # stopped early: keep only pictures that passed the checks
            b[1].unlink(missing_ok=True)
            continue
        with _STILL_FILES:
            if it["dest"].exists():
                b[1].unlink(missing_ok=True)
            else:
                b[1].replace(it["dest"])
        it["finish"](b[2])


def _shows_full_body(img: Path) -> bool:
    """Did the zoom-out work? Asked concretely -- are the feet visible, is anyone duplicated. Asking gemma
    for the shot size answered "medium" for every picture, wide ones included; these two questions sorted
    all 8 test stills correctly on 2026-09-22."""
    import base64

    import httpx

    from app.config import OLLAMA_MODEL, OLLAMA_URL
    try:
        r = ollama_client.gemma_post(f"{OLLAMA_URL}/api/generate", timeout=180, json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json",
            "keep_alive": 0,  # hand the card straight back to ComfyUI
            "options": ollama_client._gpu_options({}),
            "prompt": "Look at this photo. Answer JSON only: "
                      '{"feet_visible": true|false (can you see the shoes/feet of the main person or people '
                      'standing on the floor?), "duplicate": true|false (does the same person appear twice?)}',
            "images": [base64.b64encode(img.read_bytes()).decode()]})
        data = json.loads(r.json().get("response", "{}"))
        return bool(data.get("feet_visible")) and not data.get("duplicate")
    except Exception:  # noqa: BLE001 - no answer: keep the picture rather than fail the cut
        return True


def _zoom_out(base: Path, dest: Path, setting: str, people: int) -> None:
    """A wide shot made by pulling the camera back from a closer still.

    Drawing a wide shot straight from face photos never worked: Qwen keeps the people as large as in
    their reference photos, so "wide establishing shot" came back chest-up (cuts 1-2 of run 66). Editing
    the finished still with "zoom the camera far out" does give head-to-toe people in the room -- a
    two-shot in one step, one person in two steps (see _STEP_BACK_PROMPT), not every time. Each try is
    checked; if none passes, the closer still is used (a tighter shot beats a duplicated person)."""
    tmp = dest.with_name(f"_try_{dest.name}")
    step = dest.with_name(f"_step_{dest.name}")
    for _ in range(3 if people >= 2 else 2):
        src = _qwen_edit(base, _STEP_BACK_PROMPT, step) if people < 2 else base
        _qwen_edit(src, _zoom_prompt(setting, people), tmp)
        if _shows_full_body(tmp):
            tmp.replace(dest)
            step.unlink(missing_ok=True)
            return
    tmp.unlink(missing_ok=True)
    step.unlink(missing_ok=True)
    dest.write_bytes(base.read_bytes())


def _who_key(cast: dict) -> str:
    """Which person in which outfit a cached still shows. The close-up / wide / two-person stills were
    cached by role id only, so after a role was recast (or someone uploaded their own photo) those
    cuts kept showing the previous person (found 2026-09-22)."""
    costume = hashlib.md5((cast.get("costume") or "").encode("utf-8")).hexdigest()[:6]
    return f"c{cast.get('character_id') or 0}{costume}"


_SHARED_STILLS = FOLLOW_DIR / "_stills"
# bump when a still prompt changes, so old shared stills are not reused. 2026-09-22b: face almost frontal --
# a head turned 45 degrees (run 86, role C) kept its mouth in profile and the lips barely moved in the video
_STILL_VERSION = "2026-10-01-gaze4"
_BG_STILL_VERSION = "2026-10-05-face3"


def shared_still_key(cast: dict, project: dict, framing: str = "medium") -> str:
    return _shared_still(f"s{_who_key(cast)}_{_shot_key(framing)}", project).stem


def _remember_still(shared: Path) -> None:
    db.put_follow_still(shared.stem, rel(shared))


def _shared_still(key: str, project: dict) -> Path:
    """Where a still is kept for every run that has the same person, outfit, place and shot size.

    Stills were made per run: every viewer run of the preset spent ~2 minutes re-drawing the same
    two-person / close-up stills of the same default cast (2026-09-22, fast 10s run: 127s of 533s)."""
    _SHARED_STILLS.mkdir(parents=True, exist_ok=True)
    return _SHARED_STILLS / f"{key}_{_place_key(project)}.png"


def _place_key(project: dict) -> str:
    """Short hash of the place (setting text + background photo + still version). Per-run still files carry it
    too: they were named by person only, so after the place changed the wide / close-up / two-person cuts kept
    the old place's still (2026-09-30: subway run showed the cafe in cuts 1, 2 and 8)."""
    place = f"{project.get('setting') or ''}|{project.get('background_id') or 0}|{_STILL_VERSION}"
    if project.get("background_id"):
        place += f"|{_BG_STILL_VERSION}"  # only places from a photo changed: the cafe's stills stay valid
    return hashlib.md5(place.encode('utf-8')).hexdigest()[:8]


def _derive_shots(project: dict) -> bool:
    """A place from a background photo (the viewer's own or a picked one): draw one picture per person only."""
    return bool(project.get("background_id")) and bool(project.get("use_still", 1)) and not project.get("match_reference")


def ensure_still(project: dict, cast: dict, framing: str = "medium", plan: list | None = None) -> Path:
    """One still per cast member and shot size, looking toward screen-right. Cuts that look left use
    a mirrored copy. The medium one is cached on the cast row (that is the card thumbnail); the other
    shot sizes are cached next to it by file name."""
    # Every shot size is cached by person + outfit + shot + place/still-version. The medium one used to be
    # trusted from cast.still_path alone: a run copied the preset's path and kept the preset's OLD picture
    # after the still wording changed (run 131 medium cuts, 2026-09-30).
    dest = project_dir(project["id"]) / (f"still_cast{cast['id']}_{_who_key(cast)}_{_shot_key(framing)}"
                                         f"_{_place_key(project)}.png")
    if dest.exists():
        if framing == "medium" and cast.get("still_path") != rel(dest):
            db.update_follow_cast(cast["id"], still_path=rel(dest))
        return dest
    if plan is None:
        with _still_lock(f"s{_who_key(cast)}_{_shot_key(framing)}_{_place_key(project)}"):
            return _ensure_still(project, cast, framing, None, dest)
    return _ensure_still(project, cast, framing, plan, dest)


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _still_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _ensure_still(project: dict, cast: dict, framing: str, plan: list | None, dest: Path) -> Path:
    if dest.exists():
        return dest
    character = db.get_character(cast["character_id"]) if cast.get("character_id") else None
    if not character:
        raise ValueError(f"'{cast['label']}' 인물 사진을 먼저 고르세요")
    shared = _shared_still(f"s{_who_key(cast)}_{_shot_key(framing)}", project)
    if shared.exists():
        shutil.copy(shared, dest)
        if framing == "medium":
            db.update_follow_cast(cast["id"], still_path=rel(dest))
        return dest
    if framing != "medium" and _derive_shots(project) and plan is not None:
        return dest  # prewarm_stills derives it after the medium pictures are drawn
    if framing != "medium" and _derive_shots(project):
        # 2026-10-06 (user: "why is it drawing person pictures again, 1 cut took 10 min"): in a place the viewer picked,
        # every person was drawn 2-3 times (wide, medium, close-up), 1-4 min each. Now only the medium picture is
        # drawn; the close-up is cut from it and wide cuts use it as it is. Twins came almost only from wide drawings.
        medium = ensure_still(project, cast, "medium")
        im = Image.open(medium).convert("RGB")
        if framing == "closeup":
            w, h = im.size
            cw, ch = int(w * 0.66), int(h * 0.66)
            # around the face: a fixed spot (40 % from the left) cut E's face in half where Qwen had drawn him right
            # of center (30 s classroom run 199 cut 21); gemma's face position was on the face in 6 of 6 stills
            fx, fy = _face_center(medium)
            x0 = max(0, min(w - cw, int(w * fx) - cw // 2))
            y0 = max(0, min(h - ch, int(h * fy - ch * 0.42)))
            im = im.crop((x0, y0, x0 + cw, y0 + ch)).resize((w, h), Image.LANCZOS)
        with _STILL_FILES:
            im.save(dest)
            shutil.copy(dest, shared)
        _remember_still(shared)
        return dest

    graph = copy.deepcopy(_COMPOSE_TEMPLATE)
    char_img = comfy_client.copy_to_comfy_input(face_tile(character))
    bg = db.get_background(project["background_id"]) if project.get("background_id") else None
    bg_img = comfy_client.copy_to_comfy_input(_bg_view(bg, cast, framing)) if bg else char_img
    graph["41"]["inputs"]["image"] = char_img
    graph["83"]["inputs"]["image"] = bg_img
    # with a background photo the face used to be in image1 only (photo in 2 and 3): faces came out slimmer, younger and
    # smoother (beach run 174, 2026-10-05). The face in image3 too kept acne, face width and jaw in 4 of 4 tests.
    # ...but in a WIDE (knee-up) still the second face reference drew the person twice (run 184 cut 1: 4 of 4; test
    # 2026-10-06: 2 of 3 with the face, 0 of 3 with the place in image3) -- wide keeps the place there.
    wide_bg = bool(bg) and framing == "wide"
    graph["195"]["inputs"]["image"] = bg_img if wide_bg else char_img
    graph["170:151"]["inputs"]["prompt"] = still_prompt(character, cast.get("costume", ""),
                                                       project.get("setting", ""), framing,
                                                       "image2" if bg else None, face_in_image3=not wide_bg)
    graph["170:149"]["inputs"]["prompt"] = _STILL_NEGATIVE
    graph["170:169"]["inputs"]["seed"] = int(hashlib.sha256(
        graph["170:151"]["inputs"]["prompt"].encode("utf-8")).hexdigest()[:12], 16)
    graph["170:168"]["inputs"]["value"] = True  # 4-step Lightning
    # The sampled latent normally takes image1's shape, so a portrait face photo gave a portrait still
    # and LTX's 16:9 center crop cut the head off. Sample on a blank 16:9 canvas instead (denoise is 1,
    # only its size matters); the face photo is still the identity reference.
    graph["900"] = {"class_type": "LoadImage", "inputs": {"image": comfy_client.copy_to_comfy_input(_wide_canvas())}}
    graph["901"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["900", 0]}}
    graph["170:156"]["inputs"]["pixels"] = ["901", 0]
    def finish(best_seen: dict | None = None) -> None:
        if framing == "wide" and best_seen and (best_seen["people"] != 1 or best_seen["twice"] or best_seen["near"]):
            # no wide picture with just this one person after every try: a closer shot beats a twin (run 184 cut 1)
            medium = ensure_still(project, cast, "medium")
            with _STILL_FILES:
                shutil.copy(medium, dest)
        with _STILL_FILES:
            shutil.copy(dest, shared)
        _remember_still(shared)
        if framing == "medium":
            db.update_follow_cast(cast["id"], still_path=rel(dest))

    if plan is not None and not _pin_end_wide(framing):
        plan.append({"graph": graph, "dest": dest, "people": 1, "finish": finish,  # drawn by _draw_many
                     "who": _who(character)})
        return dest
    if _pin_end_wide(framing):
        outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
        base = dest.with_name(f"{dest.stem}_base.png")
        comfy_client.save_output_to(outputs, base, media_keys=["images", "image"])
        _zoom_out(base, dest, project.get("setting", ""), people=1)
        finish()
    else:
        finish(_draw_checked(graph, dest, people=1))
    return dest


def photo_as_frame(character: dict) -> Path:
    """The character photo itself, padded to 16:9, as LTX's first frame.

    Skips the Qwen still entirely ("LTX로 바로"): no cafe background, no costume, no gaze setup --
    the video starts from the photo as it is. Faster the first time, less faithful to the scene."""
    faces = FOLLOW_DIR / "faces"
    faces.mkdir(parents=True, exist_ok=True)
    dest = faces / f"frame{character['id']}_{Path(character['image_path']).stem}_crop.png"
    if dest.exists():
        return dest
    im = Image.open(MEDIA_DIR / character["image_path"]).convert("RGB")
    w, h = im.size
    # Cropped to 16:9 around the head and shoulders. It used to be padded with a blurred, stretched copy
    # of the photo; LTX drew people into those blurry side margins (2 of 8 cuts, 2026-09-30). The crop is
    # centred at 42% of the height: the mouth stays in frame for all 11 library photos.
    if w / h < 16 / 9:
        th = round(w * 9 / 16)
        y0 = int(min(max(0, 0.42 * h - th / 2), h - th))
        im = im.crop((0, y0, w, y0 + th))
    else:
        tw = round(h * 16 / 9)
        x0 = (w - tw) // 2
        im = im.crop((x0, 0, x0 + tw, h))
    im.save(dest)
    return dest


def ensure_pair_still(project: dict, cast_a: dict, cast_b: dict, framing: str = "medium",
                      plan: list | None = None) -> Path:
    """One still holding BOTH people, for cuts whose original frame shows two. Cached per pair."""
    pdir = project_dir(project["id"])
    first, second = sorted((cast_a, cast_b), key=lambda m: m["id"])
    dest = pdir / (f"still_pair{first['id']}_{second['id']}_{_who_key(first)}_{_who_key(second)}_{_place_key(project)}"
                   f"_{_shot_key(framing)}.png")
    if dest.exists():
        return dest
    ch_a = db.get_character(cast_a["character_id"]) if cast_a.get("character_id") else None
    ch_b = db.get_character(cast_b["character_id"]) if cast_b.get("character_id") else None
    if not (ch_a and ch_b):
        raise ValueError("두 사람 모두 인물 사진이 필요합니다")
    by_label = sorted((cast_a, cast_b), key=lambda m: m.get("label") or "")
    shared = _shared_still(f"p{_who_key(by_label[0])}_{_who_key(by_label[1])}_{_shot_key(framing)}", project)
    if shared.exists():
        shutil.copy(shared, dest)
        return dest

    graph = copy.deepcopy(_COMPOSE_TEMPLATE)
    graph["41"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(face_tile(ch_a))
    graph["83"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(face_tile(ch_b))
    bg = db.get_background(project["background_id"]) if project.get("background_id") else None
    graph["195"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(
        MEDIA_DIR / bg["image_path"]) if bg else graph["83"]["inputs"]["image"]
    shot = FRAMINGS.get(framing, FRAMINGS["medium"])[2]
    graph["170:151"]["inputs"]["prompt"] = (
        f"Put the person from image1 and the person from image2 together into one candid photograph, "
        f"set in {project.get('setting') or DEFAULT_SETTING}. {shot.capitalize()}, 16:9 frame, the two "
        # run 86's wording (the user's reference): a real two-shot, the pair turned to each other
        f"standing near each other and facing each other in three-quarter profile, looking at each other while "
        f"talking, neither looking at the camera. " + _bg_sentence("image3" if bg else None)
        + "\n\n"
        f"Keep each face exactly as in its own reference photo -- same face shape, eyes, nose, "
        f"lips, skin and age -- and keep the two faces clearly distinct. Person 1 hair and outfit: "
        f"{cast_a.get('costume') or 'casual clothes'}. Person 2 hair and outfit: "
        f"{cast_b.get('costume') or 'casual clothes'}. Match the light of the place on both, shallow depth "
        f"of field, natural relaxed expressions, no text.")
    graph["170:149"]["inputs"]["prompt"] = _STILL_NEGATIVE.replace("second person, ", "").replace("two people, ", "")
    graph["170:169"]["inputs"]["seed"] = int(hashlib.sha256(
        graph["170:151"]["inputs"]["prompt"].encode("utf-8")).hexdigest()[:12], 16)
    graph["170:168"]["inputs"]["value"] = True
    graph["900"] = {"class_type": "LoadImage", "inputs": {"image": comfy_client.copy_to_comfy_input(_wide_canvas())}}
    graph["901"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["900", 0]}}
    graph["170:156"]["inputs"]["pixels"] = ["901", 0]
    def finish(best_seen: dict | None = None) -> None:
        with _STILL_FILES:
            shutil.copy(dest, shared)
        _remember_still(shared)

    if plan is not None and not _pin_end_wide(framing):
        plan.append({"graph": graph, "dest": dest, "people": 2, "finish": finish})  # drawn by _draw_many
        return dest
    if _pin_end_wide(framing):
        outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
        base = dest.with_name(f"{dest.stem}_base.png")
        comfy_client.save_output_to(outputs, base, media_keys=["images", "image"])
        _zoom_out(base, dest, project.get("setting", ""), people=2)
    else:
        _draw_checked(graph, dest, people=2)
    finish()
    return dest


def _clean_gesture(text: str) -> str:
    """Keep the real acting, drop the invented part: "...leans forward while holding the microphone" ->
    "...leans forward". The vision model adds props that are not in the cafe and sometimes says the person
    looks into the lens, and whatever stays in the text gets drawn."""
    parts = []
    for part in text.split(","):
        kept = [seg.strip() for seg in re.split(r"\s+while\s+", part) if seg.strip() and not _GESTURE_JUNK.search(seg)]
        if kept:
            parts.append(" while ".join(kept))
    return ", ".join(parts).strip(" ,")


def _still_for_look(still: Path, look: str) -> Path:
    if look != "left":
        return still
    flipped = still.with_name(still.stem + "_left.png")
    if not flipped.exists():
        Image.open(still).transpose(Image.FLIP_LEFT_RIGHT).save(flipped)
    return flipped


_TRIM_MARGIN_DB = 22.0  # measured: 20-22 dB keeps the words, 23+ lets a noisy recording's floor back in


def _peak_db(src: Path) -> float:
    proc = subprocess.run([FFMPEG_PATH, "-hide_banner", "-i", str(src), "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", proc.stderr)
    return float(m.group(1)) if m else 0.0


_VOICE_PEAK_DB = -3.0


def _level_gain(src: Path) -> float:
    """dB to bring a take's peak to the same level as every other take. The voices came out anywhere from
    -37 to -17 dB average (run 86: A -30, C -18), so the finished video jumped in loudness at each cut."""
    return max(-12.0, min(18.0, _VOICE_PEAK_DB - _peak_db(src)))


def _levels(src: Path) -> tuple[float, float]:
    """(pause level, loudest 20 ms) of a take in dB. The pause level is the 15th percentile of 20 ms
    loudness -- what the take sounds like between words (TTS: about -70 dB; the phone-recorded voice
    a noisy phone recording: about -42 dB). Pure Python: the app's venv has no numpy."""
    import array
    import math

    raw = subprocess.run([FFMPEG_PATH, "-v", "error", "-i", str(src), "-ac", "1", "-ar", "24000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    pcm = array.array("h", raw[: len(raw) // 2 * 2])
    levels = []
    for i in range(0, len(pcm) - 480, 480):
        chunk = pcm[i:i + 480]
        rms = math.sqrt(sum(x * x for x in chunk) / 480) / 32768
        levels.append(20 * math.log10(rms + 1e-6))
    if not levels:
        return -90.0, 0.0
    levels.sort()
    return levels[int(len(levels) * 0.15)], levels[-1]


def _tighten(src: Path) -> Path:
    """Trim the silence before/after the line and shorten real pauses (> 0.2 s) to 0.1 s, so the cut is
    sized from the speech itself.

    The "silence" level is set from the take's own PAUSE level (+10 dB), never from its peak. It used to be
    peak - 22 dB: the quiet parts of words fell under it and were cut out -- "하자" came out "가자" (the soft
    h removed), the opening "아" vanished, and after a shouted "아 진짜?!" the rest of the line was deleted
    (2026-10-01 voice test: the raw takes were right, our trimmed tracks were broken in 13 of 48)."""
    dst = src.with_name(f"_tight4_{src.name}")
    if not dst.exists():
        floor, peak = _levels(src)
        thr = min(peak - 35.0, max(floor + 10.0, -65.0))
        _ff(["-i", str(src), "-af",
             f"silenceremove=start_periods=1:start_silence=0.1:start_threshold={thr:.1f}dB:"
             f"stop_periods=-1:stop_duration=0.2:stop_silence=0.1:stop_threshold={thr:.1f}dB",
             str(dst)])
    return dst


def _nudge_tone(cast: dict, tone_key: str) -> str:
    """The tempo nudge is only for voices without tone clips; with one, the delivery is already in the take."""
    return "" if tone_key and db.get_voice_tone(cast["voice_id"], tone_key) else tone_key


def _shape_audio(src: Path, tone_key: str) -> Path:
    """Gentle tempo/pitch shaping for the tone. Qwen3-TTS cannot be told a mood, so the delivery is
    nudged afterwards: excited a touch faster and higher, tired slower and lower."""
    if tone_key not in TONES:
        return src
    _, _, tempo, pitch, _ = TONES[tone_key]
    pitch = 1.0  # resampling the pitch made the voices sound processed; the tone lives in the wording now
    if abs(tempo - 1.0) < 0.01 and abs(pitch - 1.0) < 0.01:
        return src
    dst = src.with_name(f"_tone{tone_key}_{src.name}")
    if not dst.exists():
        rate = int(24000 * pitch)
        _ff(["-i", str(src), "-af", f"asetrate={rate},aresample=24000,atempo={1 / pitch * tempo:.4f}",
             str(dst)])
    return dst


_LEAD_SEC = 0.04  # the line starts 40ms into the kept clip
# LTX's first frames are the still itself -- every cut opened on a frozen picture and then started to
# move, which made the cut-to-cut editing feel stiff (user, 2026-09-22). These frames are rendered and
# then cut off; the voice track is shifted by the same amount so the lips stay in sync.
_HEAD_SEC = 0.125
# LTX's decoded audio -- and the lips, which follow the same latent timeline -- come out this much EARLIER
# than the track fed in (measured 57-67ms on 8 of 8 clips, 2026-09-30). Without it the 40ms lead was eaten
# and the first syllable started at, or before, the head trim.
_LTX_AUDIO_SHIFT = 0.066
# 2026-09-30: measured on 78 clips, the mouth moved ~40ms AFTER LTX's decoded audio (in real speech the mouth
# leads). The lips follow the track that was fed in; LTX's own decoded audio is the early one. So the clip now
# carries the clean track that was fed in (see _encode_cut) and no shift is added to it.
# Which of the two the lips follow could not be settled (mouth-motion lag measured +40ms against the decoded
# audio on 78 frontal clips, but +85ms against the clean track on 10 three-quarter clips -- the measure is too
# noisy). The clean track is therefore laid half the difference (33ms) early: at most 33ms off either way,
# below what people notice, instead of a possible 66ms of sound-before-lips.
_AUDIO_ADVANCE = _LTX_AUDIO_SHIFT / 2
_TRACK_DELAY_MS = int(round((_HEAD_SEC + _LEAD_SEC + _AUDIO_ADVANCE) * 1000))
# A last-frame guide pulls the final latent (8 frames, 1/3 s) toward the closed-mouth still -- the mouth shut
# while the voice was still going. Guided cuts are rendered one latent longer and that tail is cut off.
_GUIDE_TAIL = 1 / 3


def _guide_tail(project: dict, cut: dict) -> float:
    return _GUIDE_TAIL if _end_guide(project, cut) else 0.0
_TAIL_SEC = 0.06  # room for the last syllable to finish


def _fit_voice(d: float, target: float) -> tuple[float, float]:
    """(atempo factor, clip length) for a d-second take in a target-second cut.

    The take used to be sped up to fill the cut exactly; the 40ms lead then pushed its last 40ms past
    the trim, so every tight cut lost the end of its last syllable (found 2026-09-22, "…어렵네")."""
    room = max(0.3, target - _LEAD_SEC - _TAIL_SEC)
    factor = min(_HARD_SPEEDUP, d / room) if d > room else 1.0
    return factor, max(target, d / factor + _LEAD_SEC + _TAIL_SEC)


def _render_len(sec: float) -> float:
    """LTX wants 8k+1 frames at 24fps, i.e. a whole number of thirds of a second."""
    return max(1.0, math.ceil(sec * 3 - 1e-6) / 3)


def prepare_line(cut: dict, cast: dict) -> str:
    """Decide the text the TTS will speak, and remember it on the cut.

    This used to happen during the render: synthesize, notice it is too long, shorten, synthesize
    again -- two TTS passes per cut (~20s wasted). The voice's measured pace tells us up front how
    many characters fit, so the line is shortened before anything is synthesized."""
    if cut.get("spoken_line"):
        return cut["spoken_line"]
    line = (cut.get("line") or "").strip()
    if not line:
        return ""
    voice = db.get_voice(cast["voice_id"]) if cast.get("voice_id") else None
    limit = line_char_limit(cut, voice)  # the whole line is said even when longer (the cut grows)
    spoken = line
    tone_key = cut.get("tone") or ""
    style = tone_style(tone_key)
    if tone_key and voice and (db.get_voice_tone(voice["id"], tone_key) or voice.get("design_instruct")):
        pass  # the tone comes from that voice's tone clip; re-spelled words ("아이~" -> odd sounds) not needed
    elif tone_key and style.get("respell"):
        try:
            styled = ollama_client.restyle_line(line, style["respell"])
        except Exception:  # noqa: BLE001 - any gemma failure: plain spelling
            styled = line
        # drawn-out vowels take time to say: a tone must not push the cut past its length
        spoken = styled if len(styled) <= limit else line
    db.update_follow_cut(cut["id"], spoken_line=spoken)
    return spoken


# What a project's render is doing right now, for the "만드는 중" bar on the page:
# {"text": "...", "started": epoch seconds of the whole run}. Lives in this process only.
RENDER_STAGE: dict[int, dict] = {}


def set_stage(pid: int, text: str) -> None:
    st = RENDER_STAGE.setdefault(pid, {"started": time.time()})
    # when this step began: the "남은 시간" subtracts what this step already took. A detail after " · " (which picture
    # job of the round) is not a new step -- restarting the clock there made the time left jump back up.
    if _step_key(st.get("text") or "") != _step_key(text):
        st["at"] = time.time()
    st["text"] = text


def _step_key(text: str) -> str:
    """Which step a stage text belongs to. Every picture-drawing text with the same number of finished pictures is one
    step ("인물 사진 3장 그리는 중 (...)" -> "인물 사진 그리는 중 (0/3장 완성) · 1번째 그리는 중 2/3")."""
    if text.startswith("인물 사진"):
        m = re.search(r"\((\d+)/", text)
        return f"stills {m.group(1) if m else 0}"
    return text.split(" · ")[0]


_PREWARM_LOCKS: dict[int, threading.Lock] = {}

# ---------------------------------------------------------------- 검증된 동작 (2026-10-07)
# The user: "motion should come out well, keep the prompts saved". Each line is a fixed English action (no gemma
# rewording, so the same chip gives the same result every time), written the way that moved people in the
# 2026-10-05/06 tests: one concrete hand/arm/shoulder move at chest height + the face, the hands below the chin.
# scripts/test_motions.py renders each on one cut with one seed and stores the result (db.motion_library).
# (key, 한국어 이름, action line, kind, works in a face close-up)
MOTION_LIBRARY = [
    # 2026-10-07 user: short motion lines did not come out -> written out step by step (which hand, how high, how many
    # times, when it comes down), the face kept toward the camera and the hands kept below the chin so the mouth shows.
    ("wave", "손 흔들기",
     "lifts the right hand up to shoulder height with the palm open toward the friend, waves it side to side three "
     "times with a bright, friendly smile, then lowers it back to the chest; the hand never rises above the shoulder",
     "hands", False),
    ("clap", "박수",
     "brings both hands together in front of the chest and claps them quickly two or three times, laughing with the "
     "shoulders bouncing, then lets the hands drop to waist height", "hands", False),
    ("shrug", "어깨 으쓱",
     "raises both shoulders up toward the ears with the elbows bent and both palms turned up at waist height, "
     "eyebrows lifted, then lets the shoulders drop again", "hands", False),
    ("fold_arms", "팔짱 끼기",
     "crosses both arms firmly over the chest, the forearms resting on each other, lets the shoulders sink with a "
     "slow sigh and tilts the head a little, unimpressed", "hands", False),
    ("point", "손가락으로 가리키기",
     "raises the right arm to chest height and points the index finger straight toward the friend, jabbing it "
     "forward twice with a teasing grin, then pulls the hand back", "hands", False),
    ("wave_off", "손사래",
     "lifts one hand to chest height with the palm facing out and flaps it quickly side to side in front of the "
     "chest as if saying no, shaking the head slightly at the same time, then drops the hand", "hands", False),
    ("hands_up", "양손 번쩍",
     "throws both hands up to the sides of the shoulders with the palms open and the fingers spread, eyes wide and "
     "eyebrows raised in disbelief, holds them there for a moment, then lowers them", "hands", False),
    ("fist", "주먹 불끈",
     "clenches the right hand into a fist and pumps it down twice at chest height with an excited, determined "
     "grin, the elbow bending sharply each time", "hands", False),
    ("please", "두 손 모아 부탁",
     "presses both palms flat together low in front of the middle of the chest, well below the chin, fingers "
     "pointing up, and bobs the joined hands up and down twice while pleading with big, round eyes and the head "
     "tilted; the face stays fully visible", "hands", False),
    ("thumbs", "엄지 척",
     "raises the right hand to chest height with a clenched fist and the thumb pointing straight up, pushes it "
     "toward the friend once with a big, proud smile, then lowers it", "hands", False),
    ("one_finger", "손가락 하나 들기",
     "holds the right index finger straight up at chest height and wags it gently as if saying 'just one more', "
     "eyebrows raised and a coaxing smile", "hands", False),
    ("scratch", "머리 긁적",
     "reaches the right hand up behind the head and scratches the back of the head, the elbow pointing out to the "
     "side, with a shy, embarrassed smile, the face staying up and turned toward the camera the whole time", "hands",
     False),
    ("nod", "고개 끄덕",
     "nods big and slow two times: the whole head drops forward until the chin almost touches the chest, then comes "
     "all the way back up, eyebrows raised, a clear and exaggerated agreeing nod", "face", True),
    ("head_shake", "고개 젓기",
     "shakes the whole head firmly from side to side three times, turning it clearly left and right, with "
     "half-closed eyes and a flat, unimpressed look, the shoulders still", "face", True),
    ("laugh", "웃음 터짐",
     "bursts out laughing: the eyes squeeze almost shut, the head tips back a little and the shoulders shake up and "
     "down, then the face comes back toward the friend still smiling", "face", True),
    ("lean_in", "앞으로 쑥",
     "leans the whole upper body far forward toward the camera side, the head and shoulders coming clearly closer "
     "and getting bigger in the frame, the eyes opening wide with curiosity, then stays leaning in", "body", True),
    ("surprised", "깜짝 놀람",
     "jumps back in surprise: the head and shoulders jerk clearly backward, the eyes go very wide and the eyebrows "
     "shoot up, one hand flies up to chest height, then the body settles back", "face", True),
    ("smirk", "피식 웃기",
     "breaks into a small sideways smirk and lets out a short laugh through the nose, the shoulders giving one quick "
     "bounce, the head tilting to one side and the eyes rolling slowly to the side and back", "face", True),
]


def ensure_motion_library() -> None:
    for i, (key, label, en, kind, closeup_ok) in enumerate(MOTION_LIBRARY):
        db.put_motion(key, label, en, kind, closeup_ok, i)


def _people_place_sig(pid: int) -> tuple:
    """What the stills of a run depend on: its place and who plays each role in which outfit."""
    p = db.get_follow_project(pid) or {}
    return (p.get("setting"), p.get("background_id"), p.get("use_still"), p.get("match_reference"),
            tuple((m["id"], m.get("character_id"), m.get("costume")) for m in db.list_follow_cast(pid)))


def prewarm_stills(pid: int, only: list[int] | None = None, quiet: bool = False, progress=None) -> dict:
    """One pass per run at a time: the pictures started right after a new place (see routes/follow.py) and a
    render pressed meanwhile would otherwise both draw them; the second just waits and finds them made."""
    with _LOCKS_GUARD:
        lock = _PREWARM_LOCKS.setdefault(pid, threading.Lock())
    if not quiet and lock.locked():
        # the pictures started when the place was picked are still being drawn: say so instead of "준비 중"
        # (2026-10-07: a render pressed meanwhile showed "준비 중" for minutes)
        try:
            n = _stills_needed(db.get_follow_project(pid), [c for c in db.list_follow_cuts(pid)
                                                           if c["cast_id"] and not c["clip_path"]])
        except Exception:  # noqa: BLE001 - only a label
            n = 0
        set_stage(pid, f"인물 사진 {n}장 그리는 중 (배경을 고를 때 시작한 그림)" if n else "인물 사진 마무리 중")
    with lock:
        return _prewarm_stills(pid, only, quiet, progress)


def _prewarm_stills(pid: int, only: list[int] | None = None, quiet: bool = False, progress=None) -> dict:
    """Every still the cuts need, before any video: drawing a still between two video jobs swapped the 20 GB
    picture model and the video model in and out of memory for every cut (2026-10-05, user's beach run: 1-5 min of
    "still" per 1.3 s cut on top of 2 min of video)."""
    seen, need = set(), []
    for c in db.list_follow_cuts(pid):
        if not c["cast_id"] or (only is not None and c["id"] not in only) or (only is None and c["clip_path"]):
            continue
        k = (c["cast_id"], c.get("cast_id2") if (c.get("people") or 1) >= 2 else None, c.get("framing") or "medium")
        if k not in seen:
            seen.add(k)
            need.append(c)
    project = db.get_follow_project(pid)
    derived = []
    if _derive_shots(project):
        singles = [c for c in need if not ((c.get("people") or 1) >= 2 and c.get("cast_id2"))]
        derived = [c for c in singles if (c.get("framing") or "medium") != "medium"]
        have = {c["cast_id"] for c in singles if (c.get("framing") or "medium") == "medium"}
        need = [c for c in need if c not in derived]
        for c in derived:
            if c["cast_id"] not in have:
                have.add(c["cast_id"])
                need.append({**c, "framing": "medium"})
    plan: list[dict] = []
    sig0 = _people_place_sig(pid)
    for c in need:
        cast = db.get_follow_cast(c["cast_id"])
        character = db.get_character(cast["character_id"]) if cast and cast.get("character_id") else None
        if not (cast and character):
            continue
        if project.get("match_reference") or not project.get("use_still", 1):
            _source_frame(project, c, cast, character)
        elif (c.get("people") or 1) >= 2 and c.get("cast_id2") and db.get_follow_cast(c["cast_id2"]):
            ensure_pair_still(project, cast, db.get_follow_cast(c["cast_id2"]), c.get("framing") or "medium",
                              plan=plan)
        else:
            ensure_still(project, cast, c.get("framing") or "medium", plan=plan)
    if plan:
        # a quiet pass (started when the place was picked) still reports once a render of this run is waiting on it
        loud = lambda: not quiet or pid in RENDER_STAGE  # noqa: E731
        if loud():
            set_stage(pid, f"인물 사진 {len(plan)}장 그리는 중 (한꺼번에)")
        if progress:
            progress(0, len(plan))  # the "미리 만들기" box shows "인물 사진 0/N" right away, not only after a round

        def prog(i, n):
            if loud():
                set_stage(pid, f"인물 사진 그리는 중 ({i}/{n}장 완성)")
            if progress:
                progress(i, n)
        # 2026-10-07: picking place A and then place B kept drawing A's people (~5 min) before B's could start
        _draw_many(plan, prog, note=lambda t: loud() and set_stage(pid, t),
                   cancelled=lambda: _people_place_sig(pid) != sig0)
    if _people_place_sig(pid) != sig0:
        return {"stills": len(plan), "cancelled": True}  # the place / people changed meanwhile: the new pass draws
    for c in derived:  # close-up / wide from the medium pictures just drawn (no drawing)
        cast = db.get_follow_cast(c["cast_id"])
        if cast and cast.get("character_id"):
            ensure_still(project, cast, c.get("framing") or "medium")
    return {"stills": len(plan)}


def prewarm_voices(pid: int, only: list[int] | None = None, quiet: bool = False, progress=None) -> dict:
    """Synthesize every line of a project (or just the cuts in `only`) in one sweep, keeping the TTS
    model loaded.

    Rendering cut by cut reloaded that model for each cut (~20s each) and swapped it against the
    video model constantly. Doing the voices first means the video model then stays put too."""
    cuts = [c for c in db.list_follow_cuts(pid) if only is None or c["id"] in only]
    todo = []
    for c in cuts:
        if (only is not None or not c["clip_path"]) and c["cast_id"] and (c["line"] or "").strip():
            cast = db.get_follow_cast(c["cast_id"])
            if cast and cast.get("voice_id"):
                todo.append((c, cast))
    # Two passes. Interleaving "shorten/re-spell with gemma" and "synthesize with the TTS" swapped
    # both models on and off the GPU for every line -- the 30s run spent 17 minutes before its first
    # clip. Text first with gemma loaded, then every voice with the TTS model loaded.
    lines = []
    if any(not db.get_follow_cut(c["id"]).get("spoken_line") for c, _ in todo):
        comfy_client.free_vram()  # gemma on the card: seconds per line instead of 30s+ on the CPU
    for i, (c, cast) in enumerate(todo):
        if not quiet:
            set_stage(pid, f"대사 다듬는 중 ({i + 1}/{len(todo)})")
        spoken = prepare_line(db.get_follow_cut(c["id"]), cast)
        if spoken:
            lines.append((spoken, cast["voice_id"], c.get("tone") or ""))
    comfy_client.free_gpu_from_ollama()  # and hand the card back before the TTS / video models load
    for i, (spoken, voice_id, tone) in enumerate(lines):
        if not quiet:
            set_stage(pid, f"목소리 만드는 중 ({i + 1}/{len(lines)})")
        if progress:
            progress(i, len(lines))
        generate_voice_line(spoken, voice_id, unload_after=(i == len(lines) - 1), tone=tone)
    return {"voices": len(lines)}


def _prepare_cut(cut_id: int) -> dict | None:
    """Everything a cut needs before the GPU video pass: still, voice track, prompt, length."""
    cut = db.get_follow_cut(cut_id)
    project = db.get_follow_project(cut["project_id"])
    cast = db.get_follow_cast(cut["cast_id"]) if cut.get("cast_id") else None
    if not cast:
        raise ValueError("이 컷에 나올 인물을 고르세요")
    character = db.get_character(cast["character_id"]) if cast.get("character_id") else None
    t0 = time.monotonic()
    still = _source_frame(project, cut, cast, character)
    stage = {"still": round(time.monotonic() - t0, 1)}

    pdir = project_dir(project["id"])
    target = cut["end_sec"] - cut["start_sec"]
    clip_len = target
    t_voice = time.monotonic()
    track = pdir / f"track_{cut_id}_{uuid4().hex[:8]}.wav"
    line = (cut.get("line") or "").strip()
    tone_key = cut.get("tone") or ""
    if line:
        if not cast.get("voice_id"):
            raise ValueError(f"'{cast['label']}' 목소리를 먼저 고르세요")
        spoken = prepare_line(cut, cast)
        # prepare_line may shorten the line to fit; re-read it so the later "edited mid-render"
        # check compares against what we actually rendered, not the pre-shortened text
        cut = db.get_follow_cut(cut_id) or cut
        line = cut["line"]
        vl = generate_voice_line(spoken, cast["voice_id"], unload_after=True, tone=tone_key)
        src = _tighten(_shape_audio(MEDIA_DIR / vl["audio_path"], _nudge_tone(cast, tone_key)))
        d = comfy_client.audio_duration_seconds(src)
        factor, clip_len = _fit_voice(d, target)  # a long line lengthens the cut, it is not cut short
        render_len = _render_len(clip_len + _HEAD_SEC + _AUDIO_ADVANCE + _guide_tail(project, cut))
        _ff(["-i", str(src), "-af",
             f"volume={_level_gain(src):.1f}dB,atempo={factor:.4f},adelay={_TRACK_DELAY_MS}|{_TRACK_DELAY_MS},apad",
             "-t", f"{render_len:.3f}", "-ar", "24000", "-ac", "1", str(track)])
    else:
        render_len = _render_len(clip_len + _HEAD_SEC + _AUDIO_ADVANCE + _guide_tail(project, cut))
        _ff(["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
             "-t", f"{render_len:.3f}", str(track)])
    stage["voice"] = round(time.monotonic() - t_voice, 1)

    prompt_text = (cut.get("ltx_prompt") or "").strip()
    if not prompt_text:
        prompt_text = join_prompt(cut_prompt_parts(project, cut, cast, character))
    return {
        "cut": cut, "project": project, "stage": stage, "clip_len": clip_len,
        "source_image": still if project.get("match_reference") else _still_for_look(still, cut.get("look", "right")),
        "track": _silence_like(track, render_len) if _pin_end(project, cut) else track,
        "voice": track,  # the clean track: it becomes the clip's sound (see _encode_cut)
        "prompt": prompt_text, "duration_sec": render_len, "seed": cut["render_seed"],
        "dest": pdir / f"raw_{cut_id}_{uuid4().hex[:8]}.mp4",
        "pin_end": _GUIDE_OVERRIDE.get(cut_id) or _end_guide(project, cut),
        "fast": bool(project.get("fast_preview", 1)),
    }


# How strongly each kind of cut is pulled back to its first frame at the end (LTXVAddGuide on the last
# latent; 0 = free). Read from data/render_tuning.json on every cut so tests can change it without a restart.
# Video 24 (the user's reference, 2026-10-01: "motion free, as the prompt says") had no guide at all. A/B on
# the 24 recipe (runs 139-141): free -> the speaker of cut 6 turned his back; the "alone" wording did not help;
# a LIGHT guide (0.3) kept him facing the camera while heads, looks (cut 5 looking up at the shelves) and
# gestures still moved. Wide singles (the friend walked in, runs 136/137) and "몸 흔들기" (walked off 7 of 9)
# need 0.6. Photo mode needs 0.8 (studio backdrop filled with people).
# 2026-10-06: with the whole upper body moving (hold_relaxed) and bigger gestures, 0.3 let the speaker spin round to
# the back of the head (30 s run 185 cuts 5, 18: 2 of 2); 0.5 kept them facing but damped the gestures (gym run 189).
# With the gesture also said in the paragraph (action_in_paragraph), 0.4 / wide 0.5 gave the clearest gestures with
# nobody turning away (4 of 4 cuts).
_TUNING_DEFAULTS = {"guide_single": 0.4, "guide_wide": 0.5, "guide_step": 0.6, "guide_new_place": 0.0,
                    "guide_photo": 0.8, "alone_single": False, "hold_relaxed": True,
                    "action_in_paragraph": True, "end_check": True}


def _tuning() -> dict:
    path = MEDIA_DIR.parent / "data" / "render_tuning.json"
    try:
        return {**_TUNING_DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(_TUNING_DEFAULTS)


def _new_place(project: dict) -> bool:
    """The run was moved to a place of the viewer's choosing (not the preset's own)."""
    if not project.get("template_id"):
        return False
    t = db.get_follow_project(project["template_id"])
    return bool(t) and (project.get("setting"), project.get("background_id")) != (t.get("setting"), t.get("background_id"))


def _end_guide(project: dict, cut: dict) -> bool | float:
    """Strength of the last-frame guide for this cut (False = none). See _TUNING_DEFAULTS."""
    t = _tuning()
    if _photo_end_guide(project):
        return t["guide_photo"] or False
    if _pin_end(project, cut):
        return True
    if not project.get("use_still", 1) or project.get("match_reference"):
        return False
    if (cut.get("people") or 1) >= 2 and cut.get("cast_id2"):
        return False  # two-person cuts: stable without it (6 of 6) and they need room to move
    vals = [t["guide_single"]]
    if (cut.get("framing") or "medium") == "wide":
        vals.append(t["guide_wide"])
    if cut.get("motion") == "step":
        vals.append(t["guide_step"])
    if _new_place(project):
        vals.append(t["guide_new_place"])
    strength = max(vals)
    return strength if strength > 0 else False


def cut_prompt_parts(project: dict, cut: dict, cast: dict | None, character: dict | None) -> list[dict]:
    """The automatic prompt of a cut, with the flags that depend on the project (place, guide)."""
    guided = bool(_end_guide(project, cut))
    single = (cut.get("people") or 1) < 2 or not cut.get("cast_id2")
    return prompt_parts(cut, cast, character, project.get("setting", ""),
                        photo_frame=not project.get("use_still", 1), hold=guided,
                        alone=_new_place(project) or (guided and (cut.get("framing") or "medium") == "wide")
                        or (single and bool(_tuning().get("alone_single"))))


def _new_place_guide(project: dict, cut: dict) -> bool:
    """A run moved to a place of the viewer's choosing: in an open, empty place LTX walked the "friend just
    off-screen" into 4 of 5 medium cuts (subway / store runs, 2026-09-30). The preset's own place keeps the
    24-video look (no guide) -- only a changed place gets it."""
    if not project.get("template_id") or not project.get("use_still", 1) or (cut.get("people") or 1) >= 2:
        return False
    t = db.get_follow_project(project["template_id"])
    return bool(t) and (project.get("setting"), project.get("background_id")) != (t.get("setting"), t.get("background_id"))


def _wide_end_guide(project: dict, cut: dict) -> bool:
    """One person, knees-up wide shot. With lots of room around them LTX walked people in or off in 7 of
    9 test cuts (2026-09-30) whatever the prompt said; the end keyframe holds the framing."""
    return (bool(project.get("use_still", 1)) and not project.get("match_reference") and
            (cut.get("framing") or "medium") == "wide" and (cut.get("people") or 1) < 2 and not _pin_end(project, cut))


def _photo_end_guide(project: dict) -> bool:
    """"원본 사진으로 바로": the plain library photo is also the LAST-frame guide (voice kept). From a
    studio portrait LTX otherwise cut to a different person in a different room mid-clip, even with the
    prompt saying "same person, same place" (3 of 8 cuts, 2026-09-30)."""
    return not project.get("use_still", 1) and not project.get("match_reference")


def _pin_end(project: dict, cut: dict) -> bool:
    """Wide shots: LTX gets silence and the still as the last frame too; the line is laid on afterwards.

    With the voice in, LTX walked the tiny speaker up to the camera to show the lips (3 of 4 test
    renders). The end keyframe alone was not enough (voice + pin 0.6: a second copy walked in; pin 1.0
    froze the picture); silence + pin 0.6 kept both people in place and moving, 2 of 2. A face that small
    shows no lip-sync anyway."""
    return (not project.get("match_reference") and
            _pin_end_wide(cut.get("framing") or "medium") and bool(project.get("use_still", 1)))


def _silence_like(track: Path, seconds: float) -> Path:
    silent = track.with_name(f"silent_{track.name}")
    _ff(["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=24000", "-t", f"{seconds:.3f}", str(silent)])
    return silent


# Every clip is brought to one loudness with a single fixed gain (no compressor): LTX returned the voice ~3 dB
# louder than it was fed, so clips peaked at up to +2.3 dBFS (crackle; the 10s run 118 had 134 clipped
# samples) and cut-to-cut loudness jumped by 6 LU (-19.3 .. -12.9 LUFS). The ceiling wins over the target.
_CLIP_LUFS = -16.0
_CLIP_CEILING_DB = -1.5


def _clip_gain(src: Path, start: float, length: float, headroom: float = 0.0) -> float | None:
    """dB to apply to [start, start+length] of src's audio, or None when it is silent."""
    out = subprocess.run([FFMPEG_PATH, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
                          "-vn", "-af", "ebur128=peak=sample", "-f", "null", "-"], capture_output=True, text=True).stderr
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", out)
    peak = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", out)
    if not lufs or not peak or peak[-1] == "-inf" or float(lufs[-1]) < -60:
        return None
    return round(min(_CLIP_LUFS - float(lufs[-1]), _CLIP_CEILING_DB + headroom - float(peak[-1])), 2)


def _encode_cut(raw: Path, clip_len: float, dest: Path, voice: Path | None = None) -> None:
    """Cut the LTX output to the cut's length, dropping the frozen first frames (_HEAD_SEC). The sound is
    the clean track that was fed to LTX (`voice`, mono), not LTX's own decoded copy of it (slightly degraded,
    ~66ms early); see _AUDIO_ADVANCE for where it is cut."""
    head = ["-ss", f"{_HEAD_SEC:.3f}"]
    if voice:
        start = _HEAD_SEC + _AUDIO_ADVANCE
        # mono -> stereo at -3 dB per side keeps the loudness; the peak ceiling is met after that step
        gain = _clip_gain(voice, start, clip_len, headroom=3.0)
        af = (f"aformat=sample_fmts=flt,volume={gain or 0:.2f}dB:precision=float,"
              f"pan=stereo|c0=0.7071*c0|c1=0.7071*c0")
        audio_in = ["-ss", f"{start:.3f}", "-i", str(voice), "-map", "0:v", "-map", "1:a"]
    else:
        gain = _clip_gain(raw, _HEAD_SEC, clip_len)
        af = f"volume={gain or 0:.2f}dB"
        audio_in = []
    _ff([*head, "-i", str(raw), *audio_in, "-t", f"{clip_len:.3f}", "-af", af, "-c:v", "libx264", "-preset",
         "veryfast", "-crf", "18", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(dest)])


# ---------------------------------------------------------------- finished-clip cache
# The same cut with the same still, voice track, prompt, seed, length, guide and quality comes out the same, so a
# clip made once (in another run, or ahead of time by app/prefetch.py) is reused at once (2026-10-02, user: "the
# making time should not be felt"). Bump the version when the LTX graph, encoding or audio timing changes.
_CLIP_CACHE_VERSION = "2026-10-02a"
_CLIP_CACHE_DIR = FOLLOW_DIR / "_clips"
_digests: dict[tuple[str, float, int], str] = {}


def _file_digest(p: Path) -> str:
    st = p.stat()
    k = (str(p), st.st_mtime, st.st_size)
    if k not in _digests:
        _digests[k] = hashlib.md5(p.read_bytes()).hexdigest()
    return _digests[k]


_VIDEO_GRAPH_DIGEST = hashlib.md5((WORKFLOWS_DIR / "video.json").read_bytes()).hexdigest()[:12]


def clip_cache_key(job: dict) -> str:
    h = hashlib.sha256()
    for part in (_CLIP_CACHE_VERSION, _VIDEO_GRAPH_DIGEST, job["prompt"], f"{job['duration_sec']:.3f}",
                 f"{job['clip_len']:.3f}", repr(job["pin_end"]), str(job["seed"]), str(job.get("fast")),
                 f"{_HEAD_SEC}|{_AUDIO_ADVANCE}|{_CLIP_LUFS}|{_CLIP_CEILING_DB}",
                 repr(sorted((k, v) for k, v in _scene_speed().items() if k.startswith("ltx_"))),
                 _file_digest(Path(job["source_image"])), _file_digest(Path(job["track"])),
                 _file_digest(Path(job["voice"]))):
        h.update(str(part).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:32]


# keys of clips being made ahead of time right now (app/prefetch.py): a render of the same cut waits for that
# clip instead of making it a second time
_MAKING: set[str] = set()
_making_cv = threading.Condition()


def making(key: str, on: bool) -> None:
    with _making_cv:
        (_MAKING.add if on else _MAKING.discard)(key)
        _making_cv.notify_all()


def wait_if_making(key: str, limit: float = 900.0) -> None:
    deadline = time.monotonic() + limit
    with _making_cv:
        while key in _MAKING and time.monotonic() < deadline:
            _making_cv.wait(2)


def cached_clip(key: str) -> dict | None:
    row = db.get_clip_cache(key)
    if row and (MEDIA_DIR / row["path"]).exists():
        return row
    if row:
        db.delete_clip_cache(key)
    return None


def _store_clip(key: str, clip: Path, clip_sec: float) -> None:
    _CLIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _CLIP_CACHE_DIR / f"{key}.mp4"
    shutil.copy(clip, dest)
    db.put_clip_cache(key, rel(dest), round(clip_sec, 2))


def _drop_job_files(job: dict) -> None:
    for k in ("dest", "track", "voice"):
        if job.get(k):
            Path(job[k]).unlink(missing_ok=True)


def _finish_from_cache(job: dict, hit: dict, started: float) -> dict:
    """The cut's clip already exists: copy it in, no GPU at all."""
    cut, project = job["cut"], job["project"]
    dest = project_dir(project["id"]) / f"cut{cut['position']:02d}_{cut['id']}_{uuid4().hex[:8]}.mp4"
    shutil.copy(MEDIA_DIR / hit["path"], dest)
    _drop_job_files(job)
    stage = dict(job["stage"])
    stage["video"] = 0.0
    total = round(time.monotonic() - started, 1)
    stage["other"] = round(max(0.0, total - sum(stage.values())), 1)
    stage["cached"] = True
    _log_prompt(job, rel(dest), cached=True)
    return db.update_follow_cut(cut["id"], clip_path=rel(dest), clip_sec=hit["clip_sec"], render_sec=total,
                                timing=json.dumps(stage), status="done", error_message=None)


def _log_prompt(job: dict, clip_rel: str, cached: bool) -> None:
    """Keep the prompt that made this clip (see db.prompt_history). Never fails the cut."""
    try:
        cut, project = job["cut"], job["project"]
        db.add_prompt_history(project["id"], cut["id"], cut["position"], project.get("template_id") or project["id"],
                              job["prompt"], cut.get("action") or "", cut.get("motion") or "",
                              cut.get("framing") or "", job.get("seed"), clip_rel, cached)
    except Exception:  # noqa: BLE001
        pass


_SIG_FIELDS = ("line", "cast_id", "cast_id2", "people", "look", "action", "tone", "motion", "framing", "camera",
               "ltx_prompt")


_HARD_CUT_SCORE = 0.2


def _has_hard_cut(clip: Path) -> bool:
    """LTX now and then jumps to another shot inside a clip (school classroom run 168 cut 5, 2026-10-03: the last
    frames showed another angle with other people). ffmpeg's scene score was 0.46 there and at most 0.04 in ~60
    normal clips with head turns, looks and gestures. The first frames (the still easing in) are not counted."""
    out = subprocess.run([FFMPEG_PATH, "-hide_banner", "-i", str(clip), "-vf", "select='gte(scene,0)',metadata=print",
                          "-an", "-f", "null", "-"], capture_output=True, text=True, errors="replace").stderr
    scores = [float(x) for x in re.findall(r"scene_score=([0-9.]+)", out)]
    return max(scores[4:] or [0.0]) > _HARD_CUT_SCORE


_RETRY_GUIDE = 0.6


def _face_center(img: Path) -> tuple[float, float]:
    """Where the main person's face is in a still, as fractions of width / height (gemma; kept next to the file).
    (0.45, 0.3) when it cannot tell."""
    import base64

    import httpx

    from app.config import OLLAMA_MODEL, OLLAMA_URL
    side = img.with_name(f"{img.stem}.face.json")
    try:
        d = json.loads(side.read_text(encoding="utf-8"))
        return float(d["x"]), float(d["y"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        comfy_client.free_vram()  # never gemma on top of a resident picture / video model (2026-09-30 crash)
        r = ollama_client.gemma_post(f"{OLLAMA_URL}/api/generate", timeout=240, json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json", "keep_alive": "1m",
            "options": ollama_client._gpu_options({"temperature": 0}),
            "prompt": "Look at this photo. Answer JSON only: {\"face_center_x\": <horizontal position of the CENTER of "
                      "the main person's face, as a percent of the image width from the LEFT edge, 0-100>, "
                      "\"face_center_y\": <vertical position, percent of the image height from the TOP, 0-100>}",
            "images": [base64.b64encode(img.read_bytes()).decode()]})
        data = json.loads(r.json().get("response", ""))
        x = min(0.9, max(0.1, float(data["face_center_x"]) / 100))
        y = min(0.8, max(0.1, float(data["face_center_y"]) / 100))
    except Exception:  # noqa: BLE001 -- a crop around the usual spot beats no close-up
        return 0.45, 0.3
    try:
        side.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
    except OSError:
        pass
    return x, y


def _turned_away(clip: Path) -> bool:
    """gemma on the last frame: the speaker ended with the back or side of the head to the camera, someone else
    came in, or the picture is a smear. With the light 0.4 end guide this happened in 3 of 23 cuts of the 30 s
    rooftop run 194 (back of the head x2, a camera swing x1); the same cuts at 0.6 all ended facing the camera,
    with smaller gestures. The check flagged all 5 bad clips of that test and 2 of 16 good ones (2026-10-06).
    The caller frees ComfyUI once before a row of checks."""
    # "both eyes not visible" alone also flagged 6 of 16 good cuts of the rainy cafe run 197 -- the stills turn the
    # head ~35 degrees on purpose. A full profile / back of the head, an extra person or a smear is what was wrong in
    # every bad clip; the camera swing (run 194 cut 9) showed as a last frame unlike the first (SSIM 0.47-0.50,
    # good clips 0.80+).
    if _end_similarity(clip) < 0.6:
        return True
    frame = clip.with_name(f"{clip.stem}_end.png")
    try:
        _ff(["-sseof", "-0.08", "-i", str(clip), "-frames:v", "1", str(frame)])
        seen = _foreground_people(frame, free=False)
    except Exception:  # noqa: BLE001 -- a failed check never fails the cut
        return False
    finally:
        frame.unlink(missing_ok=True)
    return bool(seen) and ((not seen["eyes"] and seen["turn"] == "profile") or seen["broken"] or seen["people"] > 1)


def _end_similarity(clip: Path) -> float:
    """SSIM of the clip's last frame against its first (1.0 = same picture); 1.0 when it cannot be measured."""
    out = subprocess.run(
        [FFMPEG_PATH, "-hide_banner", "-i", str(clip), "-sseof", "-0.1", "-i", str(clip), "-lavfi",
         "[0:v]trim=end_frame=1,scale=256:144,setpts=PTS-STARTPTS[a];"
         "[1:v]trim=end_frame=1,scale=256:144,setpts=PTS-STARTPTS[b];[a][b]ssim", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace").stderr
    m = re.search(r"All:([0-9.]+)", out)
    return float(m.group(1)) if m else 1.0


# Cuts of a run waiting for the turned-away check (project id -> {cut id: clip cache key}). The check runs once,
# when the run's last cut is done: checking after every cut swapped the 23 GB video model out for gemma each time
# and every cut took 75 s instead of 47 s (10 s run 195, 2026-10-06).
_END_CHECK: dict[int, dict[int, str]] = {}
_GUIDE_OVERRIDE: dict[int, float] = {}  # cut id -> end guide for its second try


def _wants_end_check(job: dict) -> bool:
    g = job.get("pin_end")
    return (bool(_tuning().get("end_check")) and not isinstance(g, bool) and isinstance(g, (int, float))
            and 0 < g < _RETRY_GUIDE)


def _end_check_if_last(pid: int) -> None:
    """The run's last cut is done: look at the end of every new cut, and make the ones that turned away once more
    with the firmer guide."""
    cuts = db.list_follow_cuts(pid)
    if any(c["status"] in ("queued", "rendering") for c in cuts):
        return
    # 2026-10-07: cuts made one at a time ("이 컷 만들기" / "만들고 다음 컷") each ran this check on their own -- gemma
    # in, then the video model back in for the next cut, ~40 s more per cut. Now the new cuts wait until every cut of
    # the run has a clip and are looked at together, like after "전부 만들기".
    if any(c["cast_id"] and not c["clip_path"] for c in cuts):
        return
    todo = _END_CHECK.pop(pid, {})
    if not todo:
        return
    # The run is not finished while it is being looked at: with every cut already "done", the page and the test
    # flow put the final video together before the remade cut was there, and the lesson translations started
    # gemma on the CPU right next to the remake (LTX 121 s instead of 47 s; 10 s run 196, 2026-10-06). The last
    # cut made stays "making" until the check is over.
    hold = next(reversed(todo))
    db.update_follow_cut(hold, status="rendering")
    bad = []
    try:
        set_stage(pid, "다 만든 컷 확인 중 (뒤돌아선 컷 찾기)")
        comfy_client.free_vram()
        for cid, key in todo.items():
            c = db.get_follow_cut(cid)
            if c and c["status"] in ("done", "rendering") and c.get("clip_path") \
                    and _turned_away(MEDIA_DIR / c["clip_path"]):
                bad.append(cid)
                db.delete_clip_cache(key)
        for cid in bad:
            _GUIDE_OVERRIDE[cid] = _RETRY_GUIDE
            db.update_follow_cut(cid, status="queued")
    finally:
        if hold not in bad:
            db.update_follow_cut(hold, status="done")
    if not bad:
        return
    try:
        render_cut_batch(bad, end_check=False)
    finally:
        for cid in bad:
            _GUIDE_OVERRIDE.pop(cid, None)


def run_ltx_checked(jobs_: list[dict], fast: bool) -> None:
    """run_ltx_batch, then any clip that cut to another shot is made once more with another seed."""
    run_ltx_batch(jobs_, fast=fast)
    again = [j for j in jobs_ if j["dest"].exists() and _has_hard_cut(j["dest"])]
    for j in again:
        j["seed"] = (int(j["seed"] or 0) + 7919) % 2 ** 48
        j["stage"]["recut"] = True
    if again:
        run_ltx_batch(again, fast=fast)


def cut_signature(cut: dict) -> tuple:
    """What the user can change on a cut; a clip made with other values is stale."""
    return tuple(cut.get(k) for k in _SIG_FIELDS)


def _remake(cut_id: int) -> dict:
    """The cut changed while its clip was being made: that clip is stale, so queue the cut again.
    It used to drop back to "아직 안 만듦" silently, which looked like the button had done nothing."""
    from app import render_queue
    row = db.update_follow_cut(cut_id, status="pending", clip_path=None, render_sec=None)
    render_queue.enqueue(cut_id, "cut")
    return row


def _finish_cut(job: dict, started: float) -> dict:
    """Trim the rendered clip to its exact length and store it on the cut."""
    cut, project = job["cut"], job["project"]
    cut_id = cut["id"]
    pdir = project_dir(project["id"])
    dest = pdir / f"cut{cut['position']:02d}_{cut_id}_{uuid4().hex[:8]}.mp4"
    _encode_cut(job["dest"], job["clip_len"], dest, job.get("voice"))
    if job.get("cache_key"):
        _store_clip(job["cache_key"], dest, job["clip_len"])
    job["dest"].unlink(missing_ok=True)
    job["track"].unlink(missing_ok=True)
    if job.get("voice"):
        job["voice"].unlink(missing_ok=True)
    now = db.get_follow_cut(cut_id)
    if now and cut_signature(now) != cut_signature(cut):
        dest.unlink(missing_ok=True)  # edited while it rendered
        return _remake(cut_id)
    total = round(time.monotonic() - started, 1)
    stage = dict(job["stage"])
    stage["other"] = round(max(0.0, total - sum(v for v in stage.values() if not isinstance(v, bool))), 1)
    _log_prompt(job, rel(dest), cached=False)
    return db.update_follow_cut(cut_id, clip_path=rel(dest), clip_sec=round(job["clip_len"], 2),
                                render_sec=total, timing=json.dumps(stage),
                                status="done", error_message=None)


def render_cut_batch(cut_ids: list[int], batch_size: int = 1, end_check: bool = True) -> dict:
    """Render a project's cuts, voices already made. One cut per GPU job by default: measured
    2026-09-23 on the same 8-cut HQ run, one at a time took 644s (first cut after 148s) and 4 per job
    651s (first cut after 346s) -- no faster, and nothing to watch for six minutes."""
    done = 0
    for start in range(0, len(cut_ids), batch_size):
        chunk = cut_ids[start:start + batch_size]
        jobs_: list[dict] = []
        started = time.monotonic()
        for cid in chunk:
            db.update_follow_cut(cid, status="rendering", error_message=None)
            c0 = db.get_follow_cut(cid)
            if c0:
                set_stage(c0["project_id"], f"{c0['position'] + 1}번 컷 인물 사진·목소리 준비 중")
            try:
                job = _prepare_cut(cid)
                job["cache_key"] = clip_cache_key(job)
                if job["cache_key"] in _MAKING:
                    set_stage(c0["project_id"], f"{c0['position'] + 1}번 컷: 미리 만들던 영상이 곧 끝나요")
                    wait_if_making(job["cache_key"])
                hit = cached_clip(job["cache_key"])
                if hit:
                    _finish_from_cache(job, hit, started)
                    if end_check and _wants_end_check(job):
                        _END_CHECK.setdefault(job["project"]["id"], {})[cid] = job["cache_key"]
                    done += 1
                    continue
                jobs_.append(job)
            except Exception as exc:  # noqa: BLE001
                db.update_follow_cut(cid, status="error", error_message=friendly_error(exc))
        if not jobs_:
            continue
        try:
            t_video = time.monotonic()
            nums = [j["cut"]["position"] + 1 for j in jobs_]
            set_stage(jobs_[0]["project"]["id"],
                      f"영상 만드는 중 ({nums[0]}~{nums[-1]}번 컷, {len(nums)}개 한 번에)" if len(nums) > 1
                      else f"영상 만드는 중 ({nums[0]}번 컷)")
            run_ltx_checked(jobs_, fast=bool(jobs_[0]["project"].get("fast_preview", 1)))
            video_sec = round((time.monotonic() - t_video) / len(jobs_), 1)
        except Exception as exc:  # noqa: BLE001
            for job in jobs_:
                db.update_follow_cut(job["cut"]["id"], status="error", error_message=friendly_error(exc))
            continue
        for job in jobs_:
            job["stage"]["video"] = video_sec
            # each cut is charged its own preparation plus its share of the shared GPU job
            job["own_start"] = time.monotonic() - (job["stage"]["still"] + job["stage"]["voice"] + video_sec)
            try:
                _finish_cut(job, job["own_start"])
                if end_check and _wants_end_check(job):
                    _END_CHECK.setdefault(job["project"]["id"], {})[job["cut"]["id"]] = job["cache_key"]
                done += 1
            except Exception as exc:  # noqa: BLE001
                db.update_follow_cut(job["cut"]["id"], status="error", error_message=str(exc))
    if end_check:
        for pid in {c["project_id"] for c in (db.get_follow_cut(i) for i in cut_ids) if c}:
            try:
                _end_check_if_last(pid)
            except Exception:  # noqa: BLE001 -- only a second look; the cuts are already done
                pass
    return {"rendered": done}


def _reference_still(project: dict, cut: dict, cast: dict, character: dict | None) -> Path:
    """Use the actual reference layout rather than inventing a new portrait from a face photo."""
    if not character or not cut.get("ref_clip_path"):
        raise ValueError("원본 구도 재현에는 컷 원본과 인물 사진이 필요합니다")
    clip = MEDIA_DIR / cut["ref_clip_path"]
    second = db.get_follow_cast(cut["cast_id2"]) if cut.get("cast_id2") and cut.get("people", 1) >= 2 else None
    ch2 = db.get_character(second["character_id"]) if second and second.get("character_id") else None
    prompt = (
        "Edit image 1, a film still. Keep its camera angle, shot size, perspective, room layout, "
        "subject positions, body poses, gaze directions and background people. Replace only the main "
        f"speaker ({_who(character)}) with the identity in image 2, wearing {cast.get('costume') or 'the same clothes'}. "
        "Keep the original size of each person in the frame. Keep the whole room visible in wide shots. "
        "Remove the overlaid subtitles and caption boxes. Preserve natural photographic lighting."
    )
    if ch2:
        prompt += (f" Replace the main conversation partner ({_who(ch2)}) with the identity in image 3, "
                   f"wearing {second.get('costume') or 'the same clothes'}. Keep both main people in their original positions.")
    else:
        prompt += " Image 3 repeats the speaker identity reference."
    digest = hashlib.sha256(b"reference-layout-v1" + clip.read_bytes() + prompt.encode("utf-8"))
    for ch in (character, ch2):
        if ch:
            digest.update((MEDIA_DIR / ch["image_path"]).read_bytes())
    key = digest.hexdigest()
    _SHARED_STILLS.mkdir(parents=True, exist_ok=True)
    dest = _SHARED_STILLS / f"reference_{key}.png"
    if dest.exists():
        return dest
    frame = _SHARED_STILLS / f"reference_{key}_source.png"
    _ff(["-i", str(clip), "-frames:v", "1", str(frame)])
    graph = copy.deepcopy(_COMPOSE_TEMPLATE)
    graph["41"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(frame)
    graph["83"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(face_tile(character))
    graph["195"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(face_tile(ch2 or character))
    graph["170:151"]["inputs"]["prompt"] = prompt
    graph["170:149"]["inputs"]["prompt"] = "distorted face, duplicated main subject, changed camera angle, changed composition"
    graph["170:169"]["inputs"]["seed"] = int(key[:12], 16)
    graph["170:168"]["inputs"]["value"] = True
    outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
    comfy_client.save_output_to(outputs, dest, media_keys=["images", "image"])
    _remember_still(dest)
    return dest


def _source_frame(project: dict, cut: dict, cast: dict, character: dict | None) -> Path:
    """The first frame handed to LTX: a two-person still when the original cut shows two people,
    otherwise one portrait -- or the bare photo when the project skips the still step."""
    if project.get("match_reference"):
        return _reference_still(project, cut, cast, character)
    framing = cut.get("framing") or "medium"
    if not project.get("use_still", 1):
        return photo_as_frame(character)
    if (cut.get("people") or 1) >= 2 and cut.get("cast_id2"):
        other = db.get_follow_cast(cut["cast_id2"])
        if other:
            return ensure_pair_still(project, cast, other, framing)
    return ensure_still(project, cast, framing)


def friendly_error(exc: Exception) -> str:
    """Say what actually went wrong. A bare WinError 10061 just reads as noise."""
    text = str(exc)
    if "10061" in text or "Connection refused" in text or "ConnectError" in text:
        return "영상 프로그램(ComfyUI)이 꺼져 있습니다. ComfyUI를 켠 뒤 다시 만들어 주세요."
    if "out of memory" in text.lower() or "OutOfMemory" in text:
        return ("그래픽카드 메모리가 부족했습니다. 잠시 후 다시 만들거나, 고화질 대신 ⚡ 빠른 렌더로 만들어 주세요.")
    if "timed out" in text.lower():
        return f"영상 생성이 시간 초과됐습니다. ComfyUI 상태를 확인해 주세요. ({text})"
    return text


def render_cut(cut_id: int) -> dict:
    """One cut on its own ("이 컷 만들기"): the same steps as a whole run, so it uses the clip cache too."""
    cut = db.get_follow_cut(cut_id)
    try:
        prewarm_voices(cut["project_id"], only=[cut_id])
    except Exception:  # noqa: BLE001 - only a speed-up; _prepare_cut makes the voice itself
        pass
    render_cut_batch([cut_id])
    return db.get_follow_cut(cut_id)


# ---------------------------------------------------------------- assembly

# 0 = hard cuts. A 3-frame dissolve (held last frame + fade) read as a stutter at every cut ("편집이
# 어색함", 2026-09-22); the original sketch cuts hard, and the frozen first frames are gone now instead.
_XFADE_SEC = 0.0
_AFADE_SEC = 0.012  # per-clip audio fade in/out: no click where two clips meet


def assemble(project: dict, cuts: list[dict]) -> tuple[str, str]:
    """Join the cut clips in order and build a side-by-side 원본 | AI comparison for teaching.

    Cuts meet with a 3-frame dissolve (user, 2026-09-22: "편집 자연스럽게, 부드럽게") instead of a hard
    jump. Every clip but the last is held on its last frame for the length of the dissolve, so each
    next clip still starts at its own time and the total stays the chosen 10/20/30s; the sound is
    joined end to end with tiny fades so the lines stay in sync and nothing clicks."""
    pdir = project_dir(project["id"])
    clips = [MEDIA_DIR / c["clip_path"] for c in cuts]
    durs = [probe_duration(c) for c in clips]
    w, h = 1280, 720
    n = len(clips)
    x = _XFADE_SEC if _XFADE_SEC and n > 1 and min(durs) > 3 * _XFADE_SEC else 0.0
    inputs: list[str] = []
    chains = []
    for i, (c, d) in enumerate(zip(clips, durs)):
        inputs += ["-i", str(c)]
        hold = f",tpad=stop_mode=clone:stop_duration={x:.3f}" if x and i < n - 1 else ""
        chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                      f"setsar=1,fps=24,format=yuv420p,trim=duration={d:.3f},setpts=PTS-STARTPTS{hold}[v{i}]")
        chains.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={d:.3f},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={_AFADE_SEC},afade=t=out:st={max(0.0, d - _AFADE_SEC):.3f}:d={_AFADE_SEC}[a{i}]")
    if x:
        prev, offset = "v0", 0.0
        for i in range(1, n):
            offset += durs[i - 1]
            out = "v" if i == n - 1 else f"x{i}"
            chains.append(f"[{prev}][v{i}]xfade=transition=fade:duration={x:.3f}:offset={offset:.3f}[{out}]")
            prev = out
    else:
        chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]")
    chains.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[araw]")
    chains.append("[araw]aresample=48000,adeclick[a]")
    final = pdir / f"final_{uuid4().hex[:8]}.mp4"
    _ff([*inputs, "-filter_complex", ";".join(chains), "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(final)])

    ref = MEDIA_DIR / project["ref_video_path"]
    compare = pdir / f"compare_{uuid4().hex[:8]}.mp4"
    _ff(["-t", f"{project['length_sec']:.3f}", "-i", str(ref), "-i", str(final), "-filter_complex",
         "[0:v]scale=-2:540,setsar=1,fps=24,format=yuv420p,tpad=stop_mode=clone:stop_duration=30[l];[1:v]scale=-2:540,setsar=1,fps=24,format=yuv420p[r];"
         "[l][r]hstack=inputs=2:shortest=1[v]",
         "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac",
         "-shortest", "-movflags", "+faststart", str(compare)])
    return rel(final), rel(compare)


# ---------------------------------------------------------------- admin: import from a video page

def download_section(url: str, start: float, length: float, dest_dir: Path) -> Path:
    """Fetch only [start, start+length+1s] of a video page with yt-dlp (720p max) into dest_dir.
    The file name carries the page title after '__' so the project can be named after it."""
    try:
        import yt_dlp
        from yt_dlp.utils import download_range_func
    except ImportError as exc:
        raise RuntimeError("yt-dlp가 설치되어 있지 않습니다 (pip install yt-dlp)") from exc
    # yt-dlp's partial-download check looks for ffmpeg on PATH only, not at ffmpeg_location
    import os
    ff_dir = str(Path(FFMPEG_PATH).parent)
    if ff_dir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")
    opts = {
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source__%(title).60B.%(ext)s"),
        "download_ranges": download_range_func(None, [(start, start + length + 1)]),
        "force_keyframes_at_cuts": True,
        "ffmpeg_location": str(Path(FFMPEG_PATH).parent),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = sorted(dest_dir.glob("source__*.mp4")) or sorted(dest_dir.glob("source__*"))
    if not files:
        raise RuntimeError("다운로드된 파일을 찾지 못했습니다")
    return files[0]



# ---------------------------------------------------------------- read the original cut's acting

_EMOTION_TO_TONE = {
    "excited": "excited", "happy": "excited", "joyful": "excited", "eager": "excited",
    "annoyed": "annoyed", "irritated": "annoyed", "disgusted": "annoyed", "frustrated": "annoyed", "angry": "annoyed",
    "tired": "tired", "bored": "tired", "reluctant": "tired", "sleepy": "tired",
    "surprised": "surprised", "shocked": "surprised", "startled": "surprised",
    "playful": "aegyo", "pleading": "aegyo", "whiny": "aegyo", "cute": "aegyo",
    "neutral": "flat", "calm": "flat", "deadpan": "flat", "indifferent": "flat",
    "skeptical": "annoyed", "sulky": "tired",
}


# the vision model sometimes invents props (a "small black microphone" in a board-game cafe) or says the
# person looks at the camera; those clauses would be drawn, so they are dropped
_GESTURE_JUNK = re.compile(
    r"\b(microphone|mic|headset|guitar)\b|looking (directly )?at the camera|into the camera", re.I)


def describe_original_cut(clip: Path, line: str = "") -> dict | None:
    """Let the vision model watch two frames of the ORIGINAL cut and say how it is acted: which side
    the speaker looks toward, the emotion, and one concrete movement. This is what gets copied into
    the remake so each cut is acted like the source, not like a generic template."""
    import base64

    import httpx

    from app.config import OLLAMA_MODEL, OLLAMA_URL
    work = FOLLOW_DIR / "_describe"
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("*.jpg"):
        f.unlink()
    try:
        dur = probe_duration(clip)
        for i, frac in enumerate((0.15, 0.5, 0.85)):
            _ff(["-ss", f"{dur * frac:.3f}", "-i", str(clip), "-frames:v", "1", "-vf", "scale=640:-2",
                 str(work / f"d{i}.jpg")])
        imgs = [base64.b64encode(f.read_bytes()).decode() for f in sorted(work.glob("*.jpg"))]
        said = f'While this shot plays, the person says (Korean): "{line}". ' if line else "The person does not speak here. "
        prompt = (
            "These are three frames (start, middle, end) of one short shot from a Korean comedy sketch. Comedians act "
            "big, so read the face carefully and do not answer neutral unless the face is truly blank. "
            + said +
            "Describe the MAIN person in the foreground. Answer JSON only:\n"
            '{"look": "left"|"right"|"camera"  (which side of the FRAME their eyes point toward),\n'
            ' "emotion": one of excited, annoyed, skeptical, disgusted, tired, sulky, surprised, pleading, playful, deadpan,\n'
            ' "gesture": "what the hands, arms, head or upper body do, one short English phrase, e.g. '
            "'waves one hand toward the shelves' or 'leans back and folds his arms'; empty string if they are still\",\n"
            ' "action": "one English sentence naming the facial expression AND the one clearest movement, e.g. '
            "'raises one eyebrow and tilts his chin up, unconvinced' -- concrete, no names\"}"
        )
        r = ollama_client.gemma_post(f"{OLLAMA_URL}/api/generate", timeout=180, json={
            "model": OLLAMA_MODEL, "stream": False, "think": False, "format": "json", "keep_alive": "2m",
            "prompt": prompt, "images": imgs})
        data = json.loads(r.json().get("response", "{}"))
    except Exception:  # noqa: BLE001
        return None
    look = data.get("look") if data.get("look") in ("left", "right") else None
    emotion = str(data.get("emotion", "")).lower().strip()
    action = str(data.get("action", "")).strip().rstrip(".")
    gesture = _clean_gesture(str(data.get("gesture", "")).strip().rstrip("."))
    action = _clean_gesture(action)
    # the user asked for gestures close to the original: keep the hands/arms/body move in the action line
    if gesture and gesture.lower() not in action.lower() and len(gesture) > 4:
        action = f"{action}, {gesture}" if action else gesture
    return {
        "look": look,
        "tone": _EMOTION_TO_TONE.get(emotion, ""),
        "emotion": emotion,
        "action": action,
    }


# ---------------------------------------------------------------- 예상 시간 (2026-10-05)
# The user: show how long making will take, and let it visibly grow (a new place: every still drawn again) or
# shrink (made ahead of time) while choosing. Per-piece times measured on this PC on 2026-10-03/05.
# 2026-10-07: the same numbers also drive the "남은 시간" of a running render. Before, the page counted 90 s per
# waiting cut and nothing else: pictures, voices and the final check were missing, so a 30 s run said 25.8 min and
# took 29.6, and when the final check re-made 2 cuts the bar went 23/23 -> 21/23 with the time jumping back up.
EST_STILL_SEC = 90  # one new still, drawn and checked together with the others (52-108 s each, 2026-10-06/07)
EST_CLIP_SEC = {True: 35, False: 48}  # one cut's video, fast / high quality, on a freshly started ComfyUI (2026-10-06)
EST_START_SEC = 20  # loading the models once
EST_VOICE_SEC = 5  # one line: gemma tidy-up + Qwen3-TTS take + Whisper check (cached lines are instant)
EST_CHECK_SEC = 20  # the final turned-away check: model swap ...
EST_CHECK_PER_CUT = 1.5  # ... + gemma looking at each new cut's last frame (~45 s for 16 cuts, 2026-10-07)
EST_REMAKE_SHARE = 0.07  # cuts the check makes again: 0-1 of 8, 0-2 of 16, 2-3 of 23 (2026-10-06/07)


def _still_ready(project: dict, cut: dict) -> bool:
    if project.get("match_reference") or not project.get("use_still", 1):
        return True  # no Qwen still for these
    cast = db.get_follow_cast(cut["cast_id"]) if cut.get("cast_id") else None
    if not cast:
        return True
    framing = cut.get("framing") or "medium"
    if _derive_shots(project) and not ((cut.get("people") or 1) >= 2 and cut.get("cast_id2")):
        framing = "medium"
    if (cut.get("people") or 1) >= 2 and cut.get("cast_id2"):
        other = db.get_follow_cast(cut["cast_id2"])
        if other:
            a, b = sorted((cast, other), key=lambda m: m.get("label") or "")
            return _shared_still(f"p{_who_key(a)}_{_who_key(b)}_{_shot_key(framing)}", project).exists()
    return _shared_still(f"s{_who_key(cast)}_{_shot_key(framing)}", project).exists()


def _stills_needed(project: dict, cuts: list[dict]) -> int:
    seen, stills = set(), 0
    for c in cuts:
        if not c.get("cast_id"):
            continue
        two = (c.get("people") or 1) >= 2 and c.get("cast_id2")
        k = (c["cast_id"], c.get("cast_id2") if two else None,
             "medium" if _derive_shots(project) and not two else (c.get("framing") or "medium"))
        if k not in seen:
            seen.add(k)
            stills += 0 if _still_ready(project, c) else 1
    return stills


def _n_checked(project: dict, cuts: list[dict]) -> int:
    """How many of these cuts the final turned-away check will look at (see _wants_end_check)."""
    if not _tuning().get("end_check"):
        return 0
    n = 0
    for c in cuts:
        g = _end_guide(project, c)
        n += 1 if (not isinstance(g, bool) and 0 < g < _RETRY_GUIDE) else 0
    return n


def _check_sec(n_checked: int, clip_sec: float) -> float:
    if not n_checked:
        return 0.0
    return EST_CHECK_SEC + EST_CHECK_PER_CUT * n_checked + EST_REMAKE_SHARE * n_checked * clip_sec


def estimate_render(project: dict, ready_cut_ids: list[int]) -> dict:
    """Seconds the "전부 만들기" button would take right now, and what it is made of."""
    todo = [c for c in db.list_follow_cuts(project["id"]) if c["cast_id"] and not c["clip_path"]]
    stills = _stills_needed(project, todo)
    ready = [c for c in todo if c["id"] in ready_cut_ids]
    new = [c for c in todo if c["id"] not in ready_cut_ids]
    fast = bool(project.get("fast_preview", 1))
    voices = sum(1 for c in new if (c.get("line") or "").strip())
    check = _check_sec(_n_checked(project, new), EST_CLIP_SEC[fast])
    sec = (stills * EST_STILL_SEC + voices * EST_VOICE_SEC + len(new) * EST_CLIP_SEC[fast] + check
           + (EST_START_SEC if (stills or new) else 0) + 3 * len(ready))
    return {"sec": round(sec), "stills": stills, "clips": len(new), "ready": len(ready), "todo": len(todo),
            "fast": fast, "check": round(check)}


def _stage_left(text: str, label: str) -> int | None:
    """"목소리 만드는 중 (3/8)" -> 5 left; None when the stage is something else."""
    if not (text or "").startswith(label):
        return None
    m = re.search(r"\((\d+)/(\d+)", text)
    return max(0, int(m.group(2)) - int(m.group(1))) if m else None


def _was_cached(cut: dict) -> bool:
    t = cut.get("timing")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except ValueError:
            t = None
    return bool(t and t.get("cached"))


def render_eta(project: dict, cuts: list[dict]) -> dict | None:
    """Seconds left of a running render, from the same per-piece times as estimate_render, so the number does not
    jump when "전부 만들기" is pressed. Uses this run's own video pace once two cuts are made."""
    active = [c for c in cuts if c["status"] in ("queued", "rendering")]
    if not active:
        return None
    st = RENDER_STAGE.get(project["id"]) or {}
    text = st.get("text") or ""
    in_step = max(0.0, time.time() - (st.get("at") or time.time()))
    fast = bool(project.get("fast_preview", 1))
    clip_sec = float(EST_CLIP_SEC[fast])
    # this run's own pace only when it is clearly slower (ComfyUI slowed down, little free memory): following every
    # small difference made the time left jump up after the 2nd cut (the 1st carries the model loading)
    own = [c["render_sec"] for c in cuts if c["status"] == "done" and c.get("render_sec") and not _was_cached(c)]
    if len(own) >= 3 and sorted(own)[len(own) // 2] > clip_sec * 1.4:
        clip_sec = min(sorted(own)[len(own) // 2], clip_sec * 3)
    checking = text.startswith("다 만든 컷 확인")
    remaking = [c for c in active if c.get("clip_path") and not checking]  # the final check makes these once more
    fresh = [c for c in active if not c.get("clip_path")]
    before_pictures = text in ("", "준비 중")
    sec = 0.0
    # pictures
    left = _stage_left(text, "인물 사진 그리는 중")
    if left is None and text.startswith("인물 사진"):
        m = re.search(r"인물 사진 (\d+)장", text)
        left = int(m.group(1)) if m else None
    if left is not None:
        sec += max(left * EST_STILL_SEC - (in_step if left else 0), 10)
    elif before_pictures:
        sec += _stills_needed(project, fresh) * EST_STILL_SEC
    # voices (a cached line is instant; the whole pass is short)
    lines = sum(1 for c in fresh if (c.get("line") or "").strip())
    v_left = _stage_left(text, "목소리 만드는 중")
    if v_left is not None:
        sec += v_left * EST_VOICE_SEC
    elif before_pictures or text.startswith("인물 사진") or text.startswith("대사 다듬는 중"):
        sec += lines * EST_VOICE_SEC
    # videos (+ loading the video model once, before the first one)
    n_clips = len(fresh) + len(remaking)
    sec += n_clips * clip_sec
    if n_clips and (before_pictures or text.startswith(("인물 사진", "대사 다듬는 중", "목소리 만드는 중"))):
        sec += EST_START_SEC
    if n_clips and text.startswith("영상 만드는 중"):
        sec -= min(in_step, clip_sec * 0.9)
    # the final look at the new cuts, and the few it makes again
    if checking:
        n = len(_END_CHECK.get(project["id"], {})) or len(cuts)
        sec += max(EST_CHECK_SEC + EST_CHECK_PER_CUT * n - in_step, 5) + EST_REMAKE_SHARE * n * clip_sec
    elif not remaking and all(c.get("clip_path") or c["status"] in ("queued", "rendering")
                              for c in cuts if c.get("cast_id")):  # the check runs once the whole run is made
        waiting_check = sum(1 for c in cuts if c["id"] in _END_CHECK.get(project["id"], {}) and c not in fresh)
        sec += _check_sec(_n_checked(project, fresh) + waiting_check, clip_sec)
    return {"sec": round(max(sec, 5)), "clip_sec": round(clip_sec), "checking": checking,
            "remaking": [c["position"] + 1 for c in remaking]}


_SPEED_CACHE: dict = {"at": 0.0, "warn": []}


def _free_ram_gb() -> float | None:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / 2 ** 30
    except Exception:  # noqa: BLE001
        return None


_OWN_PROCS = {"python.exe", "llama-server.exe", "ollama.exe", "ollama app.exe", "memory compression", "system",
              "comfy desktop.exe", "registry", "ffmpeg.exe", "ffprobe.exe"}


def _other_programs_gb() -> list[tuple[str, float]]:
    """Memory held by programs that are not this app / ComfyUI / Ollama, biggest first (by program name)."""
    import csv
    import io
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, encoding="mbcs",
                             errors="replace", timeout=5).stdout
    except Exception:  # noqa: BLE001 - only a hint on the page
        return []
    by: dict[str, float] = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) < 5 or row[0].lower() in _OWN_PROCS:
            continue
        try:
            kb = float(re.sub(r"[^0-9]", "", row[4]) or 0)
        except ValueError:
            continue
        name = re.sub(r"\.exe$", "", row[0], flags=re.I)
        by[name] = by.get(name, 0.0) + kb / 2 ** 20
    return sorted(by.items(), key=lambda x: -x[1])


def speed_warnings() -> list[str]:
    """Why making is slower than it should be right now (2026-10-05: a second ComfyUI from Comfy Desktop and other
    programs left ~2 GB of the 32 GB RAM free; the 24 GB video model then streams from disk -- cuts took 115-130 s
    instead of ~85 s). Checked at most every 20 s."""
    import httpx

    from app.config import COMFYUI_PORTS
    if time.time() - _SPEED_CACHE["at"] < 20:
        return _SPEED_CACHE["warn"]
    warn = []
    ours = comfy_client.base_url()
    others = []
    for port in COMFYUI_PORTS:
        url = f"http://127.0.0.1:{port}"
        if url == ours:
            continue
        try:
            if httpx.get(f"{url}/queue", timeout=1).status_code == 200:
                others.append(port)
        except httpx.HTTPError:
            pass
    if others:
        warn.append("ComfyUI가 하나 더 켜져 있어요 (Comfy Desktop 창). 메모리를 나눠 써서 영상이 느려져요 — 앱으로 만들 땐 "
                    "Comfy Desktop 창을 닫아 주세요")
    from app import comfy_refresh
    step = comfy_refresh._last_stage1_step_sec()
    if step is not None and step > comfy_refresh.SLOW_STEP_SEC:
        warn.append(f"ComfyUI가 느려진 상태예요 (영상 계산 한 걸음 {step:.1f}초, 정상 약 1.5초). 오래 켜 두면 이렇게 돼요 — "
                    "만드는 게 없을 때 자동으로 새로 켜요")
    free = _free_ram_gb()
    if free is not None and free < 2:
        # Most of the 32 GB is normally ComfyUI's own models (+ gemma right after a ✨ clean-up): telling the viewer to
        # close Chrome then was wrong (2026-10-07: 0.8 GB free, other programs used ~3 GB). Warn only when other
        # programs really hold a lot, and name them.
        others = _other_programs_gb()
        if others and sum(gb for _, gb in others[:3]) >= 3:  # Windows itself spreads ~3-6 GB over many small ones
            names = ", ".join(f"{n} {gb:.1f}GB" for n, gb in others[:3])
            warn.append(f"컴퓨터 메모리가 {free:.1f}GB밖에 안 남았어요. 다른 프로그램이 많이 쓰고 있어요 ({names}) — "
                        "닫으면 컷당 30초 이상 빨라져요")
    _SPEED_CACHE.update(at=time.time(), warn=warn)
    return warn
