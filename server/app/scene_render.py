"""Low-level building blocks shared by the single-video route (app/routes/videos.py) and the
multi-scene story pipeline (app/routes/stories.py): fitting two TTS lines to a target length,
picking the correct (possibly mirrored) source composite, and running the LTX video.json graph.

Plus render_scene_clip(): the whole per-scene pipeline (composite -> 2x TTS -> LTX clip) used by
the story renderer.
"""
import copy
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image

from app import comfy_client, db, ollama_client
from app.compose import ensure_composite
from app.config import FFMPEG_PATH, MEDIA_DIR, STORIES_DIR, WORKFLOWS_DIR
from app.ltx_prompt import build_prompt
from app.routes.voice_lines import generate_voice_line

_VIDEO_TEMPLATE = json.loads((WORKFLOWS_DIR / "video.json").read_text(encoding="utf-8"))

# Real silence written between the two lines, so the moment the speaker changes is an actual
# feature of the audio and lands exactly where the prompt says it does. This is not extra silence:
# the old code left the same pause as dead air at the END of the clip (duration budgeted a gap that
# the concatenated audio never contained), which told LTX nothing.
_LINE_GAP_SEC = 0.6
_MIN_CLIP_SEC = 4
# 1.6x made a cloned voice sound rushed and gave LTX speech it could not lip-sync to. Only squeeze
# gently; dialogue that still doesn't fit its scene gets shortened (condense_line) instead.
_MAX_SPEEDUP = 1.3


def scene_seconds(story: dict, position: int) -> int:
    """This scene's share of the user's 총 길이, in whole seconds (LTX's frame count needs an
    integer duration), handed out so the scenes add up to exactly the total -- 30s over 4 scenes
    gives 8, 7, 7, 8 rather than four clips that each follow their own audio length."""
    n = max(1, story["scene_count"])
    total = story["target_total_sec"]
    pos = min(max(position, 0), n - 1)
    return max(_MIN_CLIP_SEC, round((pos + 1) * total / n) - round(pos * total / n))
# how hard the first LTX sampling pass is pinned to the static source composite (identity anchor)
_SOURCE_STRENGTH = 0.90


def time_compress_audio(src: Path, factor: float) -> Path:
    """Write a copy of src sped up by `factor` (pitch preserved) and return its path."""
    dst = src.with_name(f"_fit{factor:.2f}_{src.name}")
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", str(src), "-filter:a", f"atempo={factor:.4f}", "-vn", str(dst)],
        check=True, capture_output=True,
    )
    return dst


def composite_source_image(composite: dict, char1_id: int) -> Path:
    """LTX always animates the LEFT face during the first audio segment and the RIGHT face during
    the second. The composite was rendered with composite['character1_id'] on the left, but
    composite lookup is order-independent, so if the current dialogue's speaker-1 is the one who
    ended up on the right, mirror the image so the first-audio speaker is on the left again."""
    src = MEDIA_DIR / composite["image_path"]
    if char1_id == composite["character1_id"]:
        return src
    flipped = Image.open(src).transpose(Image.FLIP_LEFT_RIGHT)
    tmp = Path(tempfile.gettempdir()) / f"flipped_{uuid4().hex}.png"
    flipped.save(tmp)
    return tmp


def build_dialogue_track(audio1: Path, audio2: Path, dest: Path) -> None:
    """Write line1 + _LINE_GAP_SEC of real silence + line2 as one mono track.

    The graph used to hand LTX two clips joined by AudioConcatenate with no pause at all, while the
    clip length budgeted for one -- so the prompt's "first N seconds / remaining M seconds" markers
    never lined up with where the voice actually changed, and LTX had no acoustic cue that the
    speaker had switched. Building the track ourselves makes that switch exact."""
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", str(audio1), "-i", str(audio2),
         "-f", "lavfi", "-t", str(_LINE_GAP_SEC), "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
         "-filter_complex",
         "[0:a]aresample=24000,aformat=channel_layouts=mono[a0];"
         "[2:a]aformat=channel_layouts=mono[g];"
         "[1:a]aresample=24000,aformat=channel_layouts=mono[a1];"
         "[a0][g][a1]concat=n=3:v=0:a=1[out]",
         "-map", "[out]", "-ar", "24000", "-ac", "1", str(dest)],
        check=True, capture_output=True,
    )


def fit_two_lines(voice_line1: dict, voice_line2: dict, target_duration_sec: float,
                  exact_sec: int | None = None):
    """Measure the two synthesized lines and decide the clip length.

    The clip is never made shorter than its own audio: TrimAudioDuration would cut the second
    speaker off mid-sentence, which is exactly what wrecked the lip-sync. If the pair overruns the
    target we compress both a little (<= _MAX_SPEEDUP) and otherwise just let the clip run long.

    Returns (voice_line1, voice_line2, t1, t2, duration_sec) where t1/t2 are the REAL spoken
    seconds -- the numbers the prompt quotes -- and the voice_line dicts may point at compressed
    copies."""
    d1 = comfy_client.audio_duration_seconds(MEDIA_DIR / voice_line1["audio_path"])
    d2 = comfy_client.audio_duration_seconds(MEDIA_DIR / voice_line2["audio_path"])

    budget = max(1.0, target_duration_sec - _LINE_GAP_SEC)
    if d1 + d2 > budget:
        factor = min(_MAX_SPEEDUP, (d1 + d2) / budget)
        if factor > 1.01:
            fit1 = time_compress_audio(MEDIA_DIR / voice_line1["audio_path"], factor)
            fit2 = time_compress_audio(MEDIA_DIR / voice_line2["audio_path"], factor)
            voice_line1 = {**voice_line1, "audio_path": str(fit1.relative_to(MEDIA_DIR)).replace("\\", "/")}
            voice_line2 = {**voice_line2, "audio_path": str(fit2.relative_to(MEDIA_DIR)).replace("\\", "/")}
            d1, d2 = d1 / factor, d2 / factor

    t1, t2 = round(d1, 1), round(d2, 1)
    if exact_sec:
        # story scenes: the clip is exactly its share of the total the user picked
        duration_sec = exact_sec
    else:
        duration_sec = max(_MIN_CLIP_SEC, math.ceil(d1 + _LINE_GAP_SEC + d2))
    return voice_line1, voice_line2, t1, t2, duration_sec


def run_ltx_clip(source_image: Path, dialogue_track: Path, prompt_text: str,
                 duration_sec: int, dest: Path, fast: bool = False, negative: str | None = None,
                 pin_end: bool | float = False, seed: int | None = None) -> Path:
    """Wire and run the LTX video.json graph for one talking two-shot, save the result to dest.

    video.json is a two-stage graph: stage 1 samples at half resolution (node 340:291, 8 steps),
    then a 2x latent upscaler + a 3-step refine pass at full res (340:295 -> 340:310) before the
    VAE decode. Measured on this 16GB card, stage 2 is where nearly all the time goes (~44s/step
    at full res, plus the upscaler and a full-res tiled VAE decode) while stage 1 runs ~6s/step.

    `fast=True` decodes stage 1 directly (640x360 instead of 1280x720) by pointing the video and
    audio decoders at 340:309 -- the separated stage-1 latent -- instead of 340:311. ComfyUI then
    prunes the whole upscale branch, so a short clip comes back in about a minute instead of five.
    Use it while iterating on scenes; turn it off for the final render.
    """
    import random

    graph = copy.deepcopy(_VIDEO_TEMPLATE)
    graph["269"]["inputs"]["image"] = comfy_client.copy_to_comfy_input(source_image)
    # we assemble the two lines + the pause ourselves, so bypass the graph's AudioConcatenate (351)
    graph["276"]["inputs"]["audio"] = comfy_client.copy_to_comfy_input(dialogue_track)
    graph["340:332"]["inputs"]["audio"] = ["276", 0]
    # the second LoadAudio and the concat are unused now; leaving them in made ComfyUI try to open
    # PLACEHOLDER_VOICE2.mp3 on every render (the WARNING in its log)
    graph.pop("350", None)
    graph.pop("351", None)
    graph["340:319"]["inputs"]["value"] = prompt_text
    if negative:
        graph["340:314"]["inputs"]["text"] = negative
    graph["340:331"]["inputs"]["value"] = duration_sec
    # our prompt already carries the exact dialogue quotes + motion; skip LTX's internal rewrite
    graph["340:349"]["inputs"]["value"] = False
    graph["340:325"]["inputs"]["strength"] = _SOURCE_STRENGTH
    seed = random.randint(0, 2**48 - 1) if seed is None else seed
    graph["340:285"]["inputs"]["noise_seed"] = seed
    graph["340:286"]["inputs"]["noise_seed"] = (seed + 1) % 2**48

    if fast:
        graph["340:316"]["inputs"]["samples"] = ["340:309", 0]
        graph["340:303"]["inputs"]["samples"] = ["340:309", 1]
        # Preview quality: the stage-1 schedule spends its first four steps between sigma 1.0 and
        # 0.975 (almost no change). Dropping those and halving the base resolution again cuts the
        # sampling time roughly in half for 1-2 second clips.
        graph["340:308"]["inputs"]["sigmas"] = "1.0, 0.975, 0.909375, 0.725, 0.421875, 0.0"
        graph["340:330"]["inputs"]["value"] = 1024
        graph["340:324"]["inputs"]["value"] = 576
    if pin_end:
        _pin_end_frame(graph, strength=None if pin_end is True else float(pin_end))

    # generous budget for this clip's length and quality; past it the job is cancelled (see
    # comfy_client.wait_for_outputs) instead of blocking the GPU for an hour
    budget = 600 + duration_sec * (60 if fast else 240)
    outputs = comfy_client.run_prompt_and_wait(graph, timeout=min(3600, budget), poll_interval=2)
    comfy_client.save_output_to(outputs, dest, media_keys=["video", "videos", "gifs", "images"])
    return dest


def render_scene_clip(scene: dict, story: dict, char1: dict, char2: dict, bg: dict) -> dict:
    """Full per-scene pipeline for a story: ensure the composite (identity-locked to the original
    character photos), synthesize both voice lines, fit them to the per-scene time budget, build
    the LTX prompt, render the clip. Persists composite_id/voice_line*/clip_path on the scene row
    and returns the updated scene."""
    started = time.monotonic()
    fast = bool(story.get("fast_preview", 1))
    db.update_scene(scene["id"], status="rendering", error_message=None)
    try:
        composite = ensure_composite(char1["id"], char2["id"], bg["id"])

        clip_sec = scene_seconds(story, scene["position"])

        # keep the TTS model resident across the pair, drop it before LTX takes the card
        voice_line1 = generate_voice_line(scene["line1"], story["voice1_id"], unload_after=False)
        voice_line2 = generate_voice_line(scene["line2"], story["voice2_id"], unload_after=True)

        # If the voiced lines can't fit this scene's seconds even sped up, don't stretch the clip
        # (that broke the user's 총 길이) and don't cut the second speaker off -- shorten the lines,
        # keep the meaning, save them so the card shows what is actually said, and voice them again.
        d1 = comfy_client.audio_duration_seconds(MEDIA_DIR / voice_line1["audio_path"])
        d2 = comfy_client.audio_duration_seconds(MEDIA_DIR / voice_line2["audio_path"])
        room = (clip_sec - _LINE_GAP_SEC) * _MAX_SPEEDUP
        if d1 + d2 > room and scene.get("lines_by_user"):
            # Lines the user typed are never rewritten: every word gets said and this one scene
            # runs longer instead. (The editor shows the per-line limit so this is a choice.)
            clip_sec = math.ceil((d1 + d2) / _MAX_SPEEDUP + _LINE_GAP_SEC)
        elif d1 + d2 > room:
            ratio = room / (d1 + d2) * 0.95
            new1 = ollama_client.condense_line(scene["line1"], max(4, int(len(scene["line1"]) * ratio)))
            new2 = ollama_client.condense_line(scene["line2"], max(4, int(len(scene["line2"]) * ratio)))
            db.update_scene(scene["id"], line1=new1, line2=new2, ltx_prompt=None)
            scene = {**scene, "line1": new1, "line2": new2, "ltx_prompt": None}
            voice_line1 = generate_voice_line(new1, story["voice1_id"], unload_after=False)
            voice_line2 = generate_voice_line(new2, story["voice2_id"], unload_after=True)

        # `fast` only changes render quality; length is always this scene's share of the total
        voice_line1, voice_line2, t1, t2, duration_sec = fit_two_lines(
            voice_line1, voice_line2, clip_sec, exact_sec=clip_sec
        )

        # a hand-edited prompt saved on the scene wins; otherwise author one now and keep it, so
        # the user can see exactly what LTX was told and fix it in place before re-rendering
        prompt_text = (scene.get("ltx_prompt") or "").strip()
        # Trust a stored prompt only if it quotes exactly the lines about to be voiced. A prompt
        # left over from before the script was regenerated told LTX one dialogue while the audio
        # carried another -- and those scenes came out with no lip-sync at all.
        if prompt_text and not (scene["line1"] in prompt_text and scene["line2"] in prompt_text):
            prompt_text = ""
        if not prompt_text:
            prompt_text = build_prompt(
                scene["situation"] or story["premise"], char1, char2, bg,
                scene["line1"], scene["line2"], t1, t2,
            )
            db.update_scene(scene["id"], ltx_prompt=prompt_text)

        story_dir = STORIES_DIR / str(story["id"])
        story_dir.mkdir(parents=True, exist_ok=True)
        track = story_dir / f"track_{scene['id']}_{uuid4().hex}.mp3"
        build_dialogue_track(
            MEDIA_DIR / voice_line1["audio_path"],
            MEDIA_DIR / voice_line2["audio_path"],
            track,
        )
        dest = story_dir / f"scene{scene['position']:02d}_{scene['id']}_{uuid4().hex}.mp4"
        run_ltx_clip(
            composite_source_image(composite, char1["id"]),
            track, prompt_text, duration_sec, dest, fast,
        )
        track.unlink(missing_ok=True)

        # The user may have edited this scene while it was rendering. Then this clip says the old
        # lines: never stamp it 완료 under the new text. Throw it away and leave the scene 렌더 안 됨;
        # if they also asked to render again, the queue puts it straight back in line.
        now = db.get_scene(scene["id"])
        if now and (now["line1"], now["line2"], now["background_id"]) != \
                (scene["line1"], scene["line2"], scene["background_id"]):
            dest.unlink(missing_ok=True)
            return db.update_scene(scene["id"], status="pending", clip_path=None,
                                   render_sec=None, error_message=None)

        rel = str(dest.relative_to(MEDIA_DIR)).replace("\\", "/")
        return db.update_scene(
            scene["id"],
            composite_id=composite["id"],
            voice_line1_id=voice_line1["id"],
            voice_line2_id=voice_line2["id"],
            clip_path=rel,
            render_sec=round(time.monotonic() - started, 1),
            status="done",
            error_message=None,
        )
    except Exception as exc:  # noqa: BLE001 - persist per-scene failure, let the caller continue
        db.update_scene(scene["id"], status="error", error_message=str(exc),
                        render_sec=round(time.monotonic() - started, 1))
        raise


# Nodes that hold the heavy shared state: loaded once per job no matter how many clips are in it.
# Everything else is per-clip and gets duplicated.
_SHARED_NODES = {
    "340:317",  # checkpoint (LTXAV)
    "340:318",  # gemma text encoder
    "340:313",  # latent upscaler
    "340:335",  # audio VAE
    "340:293",  # distilled LoRA on the model
    "340:314",  # negative prompt encode
    "340:288", "340:298",  # sampler selects
    "340:289", "340:308",  # sigma schedules
    "340:305",  # bypass flag
    "340:323", "340:304",  # fps
    "340:330", "340:324",  # base width / height
    "340:299", "340:301",  # half-res math
    "340:333",  # solid mask
    "340:349",  # internal-rewrite flag
}


_PIN_END_STRENGTH = 0.6  # 1.0 froze the picture, 0.4 let the speaker walk off


def _pin_end_frame(graph: dict, sfx: str = "", strength: float | None = None) -> None:
    """Give LTX the source still as the LAST frame too, so the shot ends where it started.

    In a wide shot the speaker's face is tiny and LTX walked her up to the camera to show the lips
    (3 of 4 test renders on 2026-09-22, "stay in place" in the prompt did not hold). An end keyframe
    keeps the people where the still has them. LTXVAddGuide appends the keyframe to the stage-1 latent;
    the graph's LTXVCropGuides (340:292) then strips it again before the upscale / decode."""
    n = lambda i: i if i in _SHARED_NODES else f"{i}{sfx}"  # noqa: E731
    gid = f"900{sfx}"
    graph[gid] = {"class_type": "LTXVAddGuide", "inputs": {
        "positive": [n("340:307"), 0], "negative": [n("340:307"), 1], "vae": [n("340:317"), 2],
        "latent": [n("340:325"), 0], "image": [n("340:334"), 0], "frame_idx": -1,
        "strength": strength or _PIN_END_STRENGTH}}
    graph[n("340:326")]["inputs"]["video_latent"] = [gid, 2]
    for node in ("340:315", "340:292"):
        graph[n(node)]["inputs"]["positive"] = [gid, 0]
        graph[n(node)]["inputs"]["negative"] = [gid, 1]
    graph[n("340:295")]["inputs"]["samples"] = [n("340:292"), 2]
    if graph[n("340:316")]["inputs"]["samples"][0] == n("340:309"):  # fast mode decodes stage 1
        graph[n("340:316")]["inputs"]["samples"] = [n("340:292"), 2]


def run_ltx_batch(items: list[dict], fast: bool = False) -> list[Path]:
    """Render several 1-2s clips in ONE ComfyUI job.

    Measured per clip: ~13s of sampling but ~20s of loading the checkpoint and the gemma text
    encoder, which a 16GB card cannot keep resident alongside each other. Paying that once for a
    handful of clips is where the time goes. `items` are dicts with source_image, track, prompt,
    duration_sec and dest.
    """
    import random

    graph: dict = {}
    save_nodes: list[str] = []
    template = copy.deepcopy(_VIDEO_TEMPLATE)
    template.pop("350", None)
    template.pop("351", None)
    for node_id, node in template.items():
        if node_id in _SHARED_NODES:
            graph[node_id] = copy.deepcopy(node)

    def remap(ref, idx):
        if isinstance(ref, list) and ref and isinstance(ref[0], str):
            return [ref[0] if ref[0] in _SHARED_NODES else f"{ref[0]}#{idx}", ref[1]]
        return ref

    for idx, item in enumerate(items):
        for node_id, node in template.items():
            if node_id in _SHARED_NODES:
                continue
            new = copy.deepcopy(node)
            new["inputs"] = {k: remap(v, idx) for k, v in new["inputs"].items()}
            graph[f"{node_id}#{idx}"] = new
        g = lambda n: graph[f"{n}#{idx}"]["inputs"]  # noqa: E731
        g("269")["image"] = comfy_client.copy_to_comfy_input(item["source_image"])
        g("276")["audio"] = comfy_client.copy_to_comfy_input(item["track"])
        graph[f"340:332#{idx}"]["inputs"]["audio"] = [f"276#{idx}", 0]
        g("340:319")["value"] = item["prompt"]
        g("340:331")["value"] = item["duration_sec"]
        g("340:325")["strength"] = _SOURCE_STRENGTH
        seed = item.get("seed")
        seed = random.randint(0, 2**48 - 1) if seed is None else seed
        g("340:285")["noise_seed"] = seed
        g("340:286")["noise_seed"] = (seed + 1) % 2**48
        g("341")["filename_prefix"] = f"follow/batch_{uuid4().hex[:8]}_{idx}"
        if fast:
            g("340:316")["samples"] = [f"340:309#{idx}", 0]
            g("340:303")["samples"] = [f"340:309#{idx}", 1]
        if item.get("pin_end"):
            pe = item["pin_end"]
            _pin_end_frame(graph, f"#{idx}", strength=None if pe is True else float(pe))
        save_nodes.append(f"341#{idx}")

    if fast:
        graph["340:308"]["inputs"]["sigmas"] = "1.0, 0.975, 0.909375, 0.725, 0.421875, 0.0"
        graph["340:330"]["inputs"]["value"] = 1024
        graph["340:324"]["inputs"]["value"] = 576
        for node_id, node in graph.items():
            if node["class_type"] == "VAEDecodeTiled":
                node["inputs"]["tile_size"] = 1024  # one tile for a preview-sized frame
                node["inputs"]["overlap"] = 0
    # speed experiments / settings (data/render_tuning.json, read every job; 2026-10-06):
    #   ltx_stage1_steps 5 -> the shorter stage-1 schedule with the full-resolution refine kept
    #   ltx_width / ltx_height -> output size; ltx_tile -> VAE decode tile size
    if not fast:
        tun = _speed_tuning()
        if tun.get("ltx_stage1_steps") == 5:
            graph["340:308"]["inputs"]["sigmas"] = "1.0, 0.975, 0.909375, 0.725, 0.421875, 0.0"
        if tun.get("ltx_sigmas1"):
            graph["340:308"]["inputs"]["sigmas"] = tun["ltx_sigmas1"]
        if tun.get("ltx_sigmas2"):
            graph["340:289"]["inputs"]["sigmas"] = tun["ltx_sigmas2"]
        if tun.get("ltx_width") and tun.get("ltx_height"):
            graph["340:330"]["inputs"]["value"] = int(tun["ltx_width"])
            graph["340:324"]["inputs"]["value"] = int(tun["ltx_height"])
        if tun.get("ltx_tile"):
            for node in graph.values():
                if node["class_type"] == "VAEDecodeTiled":
                    node["inputs"]["tile_size"] = int(tun["ltx_tile"])
    # our prompts already carry the full description; skip LTX's own prompt rewrite
    graph["340:349"]["inputs"]["value"] = False

    budget = 600 + sum(i["duration_sec"] for i in items) * (60 if fast else 240)
    outputs = comfy_client.run_prompt_and_wait(graph, timeout=min(3600, budget), poll_interval=2)
    dests = []
    for node_id, item in zip(save_nodes, items):
        node_out = outputs.get(node_id)
        if not node_out:
            raise comfy_client.ComfyError(f"batch render produced no output for {node_id}")
        comfy_client.save_output_to({node_id: node_out}, item["dest"],
                                    media_keys=["video", "videos", "gifs", "images"])
        dests.append(item["dest"])
    return dests


# 2026-10-06 speed test on 4 cuts (fresh ComfyUI): 8 stage-1 steps 57.5 s/cut, 5 steps 42-46 s with the same look (same
# seed, faces and gestures unchanged); fewer steps, smaller frames, bigger decode tiles or 4 cuts per job gave no more.
_SPEED_DEFAULTS: dict = {"ltx_stage1_steps": 5}


def _speed_tuning() -> dict:
    """LTX speed settings; defaults here, overridable in data/render_tuning.json (read on every job)."""
    try:
        data = json.loads((Path(__file__).resolve().parent.parent / "data" / "render_tuning.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**_SPEED_DEFAULTS, **data}
