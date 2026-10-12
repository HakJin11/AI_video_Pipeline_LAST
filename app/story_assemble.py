"""Assemble a story's rendered per-scene clips into one final mp4 with ffmpeg: burn-in captions,
scene transitions (cut / crossfade), optional intro & outro title cards, and an optional
background-music bed mixed under the dialogue. No GPU -- pure ffmpeg, ~10-30s.
"""
import json
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from app.config import FFMPEG_PATH, FFPROBE_PATH, KOREAN_FONT_PATH, MEDIA_DIR, STORIES_DIR

_XFADE_SEC = 0.5
_CARD_SEC = 3.0
_CAPTION_SEC = 2.5
_FPS = 24

# scene-to-scene transition presets: name -> (ffmpeg xfade transition, seconds).
# "cut" is a hard concat (no entry here). "fade" is a soft dissolve; "blackout" dips the
# picture and sound through black for ~1s so a jump in time/place actually reads as one.
_TRANSITIONS = {
    "fade": ("fade", 0.6),
    "blackout": ("fadeblack", 1.0),
}


def _probe_wh(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def _font_arg() -> str | None:
    p = Path(KOREAN_FONT_PATH)
    if not p.exists():
        return None
    # ffmpeg filter syntax: escape the drive colon and use forward slashes
    return str(p).replace("\\", "/").replace(":", "\\:")


def _run(args: list[str]) -> None:
    subprocess.run([FFMPEG_PATH, "-y", *args], check=True, capture_output=True)


def _normalize_scene(src: Path, w: int, h: int, caption: str | None, work: Path) -> Path:
    """Re-encode one scene clip to a uniform w/h/fps/SAR so later concat/xfade steps are safe,
    burning in the caption over the first few seconds if the scene has one."""
    dst = work / f"norm_{uuid4().hex}.mp4"
    vf = [f"scale={w}:{h}:force_original_aspect_ratio=decrease",
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", f"fps={_FPS}", "format=yuv420p"]

    font = _font_arg()
    if caption and caption.strip() and font:
        tf = work / f"cap_{uuid4().hex}.txt"
        tf.write_text(caption.strip(), encoding="utf-8")
        tfp = str(tf).replace("\\", "/").replace(":", "\\:")
        vf.append(
            f"drawtext=fontfile='{font}':textfile='{tfp}':fontcolor=white:fontsize={max(28, h // 22)}:"
            f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h-text_h-{h // 12}:"
            f"enable='between(t,0,{_CAPTION_SEC})'"
        )

    _run(["-i", str(src), "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dst)])
    return dst


def _make_card(text: str, w: int, h: int, work: Path) -> Path:
    dst = work / f"card_{uuid4().hex}.mp4"
    font = _font_arg()
    tf = work / f"card_{uuid4().hex}.txt"
    tf.write_text(text.strip(), encoding="utf-8")
    tfp = str(tf).replace("\\", "/").replace(":", "\\:")
    draw = (
        f"drawtext=textfile='{tfp}':fontcolor=white:fontsize={max(40, h // 12)}:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=12"
    )
    if font:
        draw = f"drawtext=fontfile='{font}':" + draw[len("drawtext="):]
    _run(["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:d={_CARD_SEC}:r={_FPS}",
          "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
          "-vf", f"{draw},setsar=1,format=yuv420p", "-t", str(_CARD_SEC), "-c:v", "libx264",
          "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2",
          "-shortest", str(dst)])
    return dst


def _join(acc: Path, nxt: Path, transition: str, work: Path) -> Path:
    """Append nxt to acc: a hard cut (concat), or an xfade transition (dissolve / dip-to-black)
    per the _TRANSITIONS preset, with the audio crossfaded over the same span."""
    dst = work / f"join_{uuid4().hex}.mp4"
    preset = _TRANSITIONS.get(transition)
    if preset:
        xname, xdur = preset
        # Hold the outgoing clip's last frame for the length of the transition and dissolve out of
        # that, instead of overlapping the two clips. Overlapping shaved xdur off the total for
        # every transition (so 30s came out ~28s) and faded over the end of the last spoken line.
        # Audio is a plain back-to-back concat, so every word of both clips is heard in full.
        off = _probe_duration(acc)
        fc = (
            f"[0:v]tpad=stop_mode=clone:stop_duration={xdur}[v0];"
            f"[v0][1:v]xfade=transition={xname}:duration={xdur}:offset={off}[v];"
            f"[0:a][1:a]concat=n=2:v=0:a=1[a]"
        )
        _run(["-i", str(acc), "-i", str(nxt), "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(dst)])
    else:
        lst = work / f"cat_{uuid4().hex}.txt"
        lst.write_text(f"file '{acc.as_posix()}'\nfile '{nxt.as_posix()}'\n", encoding="utf-8")
        _run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-preset",
              "veryfast", "-crf", "20", "-c:a", "aac", str(dst)])
    return dst


def _mix_music(video: Path, music: Path, gain: float, work: Path) -> Path:
    dst = work / f"music_{uuid4().hex}.mp4"
    fc = (
        f"[1:a]aloop=loop=-1:size=2e9,volume={gain}[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]"
    )
    _run(["-i", str(video), "-i", str(music), "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
          "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst)])
    return dst


def assemble_story(story: dict, scenes: list[dict]) -> str:
    """scenes: ordered, each with a 'clip_path' (media-relative) and 'caption'/'transition'.
    Returns the media-relative path of the final mp4."""
    clips = [s for s in scenes if s.get("clip_path")]
    if not clips:
        raise ValueError("no rendered scene clips to assemble")

    first = MEDIA_DIR / clips[0]["clip_path"]
    w, h = _probe_wh(first)

    out_dir = STORIES_DIR / str(story["id"])
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)

        norm = [_normalize_scene(MEDIA_DIR / c["clip_path"], w, h, c.get("caption"), work) for c in clips]

        segments: list[tuple[Path, str]] = []
        if (story.get("intro_text") or "").strip():
            segments.append((_make_card(story["intro_text"], w, h, work), "cut"))
        for c, n in zip(clips, norm):
            segments.append((n, c.get("transition") or "cut"))
        if (story.get("outro_text") or "").strip():
            segments.append((_make_card(story["outro_text"], w, h, work), "fade"))

        acc = segments[0][0]
        for seg_path, trans_into in segments[1:]:
            acc = _join(acc, seg_path, trans_into, work)

        if (story.get("music_path") or "").strip():
            music = MEDIA_DIR / story["music_path"]
            if music.exists():
                acc = _mix_music(acc, music, story.get("music_gain") or 0.25, work)

        final = out_dir / f"final_{uuid4().hex}.mp4"
        _run(["-i", str(acc), "-c", "copy", str(final)])

    return str(final.relative_to(MEDIA_DIR)).replace("\\", "/")
