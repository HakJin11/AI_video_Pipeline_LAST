import copy
import json
import random
import re
import subprocess
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app import comfy_client, db, stt
from app.config import FFMPEG_PATH, MEDIA_DIR, VOICELINES_DIR, WORKFLOWS_DIR
from app.schemas import VoiceLineCreate

router = APIRouter(prefix="/api/voice-lines", tags=["voice_lines"])

_TEMPLATE = json.loads((WORKFLOWS_DIR / "tts.json").read_text(encoding="utf-8"))

# Past this length, this Qwen3 voice-clone model starts inserting a lot of extra unnatural
# mid-sentence pauses into a single TTS call (measured: ~20-30% silence in a ~60-char line vs.
# 40%+ in a ~100-char line) -- the longer the target-duration dialogue lines get, the choppier a
# single call sounds. So instead of one long TTS call, split on sentence boundaries and stitch
# the pieces back together; each piece stays short enough to come out clean.
_MAX_TTS_CHARS = 75

# Confirmed via production case: a 67-char line came out as 87s of audio (0.76 chars/sec) for a
# voice whose other 5 samples all measured 3.1-5.3 chars/sec. Librosa silence analysis showed two
# internal dead-air gaps over 4s each -- the model occasionally goes into a runaway-pause state for
# a given (text, seed) pair, not a real reflection of the voice's pace. Retrying with a fresh random
# seed reliably avoids it, so treat an implausibly slow result as a failed attempt and retry.
_MIN_PLAUSIBLE_CHARS_PER_SEC = 1.8
_CHECKED_TRIES = 3
_CHECK_MAX_MISMATCH = 0.25  # share of letters Whisper heard differently; clean takes measured 0.0-0.12
_MAX_SYNTHESIS_ATTEMPTS = 3


def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]
    if not sentences:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _concat_audio(paths: list[Path], dest: Path) -> None:
    if len(paths) == 1:
        paths[0].replace(dest)
        return
    list_file = dest.with_suffix(".txt")
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in paths), encoding="utf-8")
    try:
        subprocess.run(
            [FFMPEG_PATH, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(dest)],
            check=True, capture_output=True,
        )
    finally:
        list_file.unlink(missing_ok=True)
        for p in paths:
            p.unlink(missing_ok=True)


def _synthesize_chunk(text: str, reference_audio_filename: str, unload: bool = True,
                      reference_text: str = "") -> Path:
    best_path: Path | None = None
    best_rate = -1.0
    last_error: Exception | None = None

    for attempt in range(_MAX_SYNTHESIS_ATTEMPTS):
        graph = copy.deepcopy(_TEMPLATE)
        graph["62"]["inputs"]["audio"] = reference_audio_filename
        graph["59"]["inputs"]["target_text"] = text
        # Advanced node: a length cap per line. Qwen3-TTS sometimes never ends a line and keeps generating
        # to 2048 tokens -- one 3-second line took 16 minutes on 2026-09-22. 12 tokens = 1s of audio; allow
        # ~2x a slow speaking pace, then the retry below picks a take that ended properly.
        graph["59"]["class_type"] = "AILab_Qwen3TTSVoiceClone_Advanced"
        graph["59"]["inputs"].update({
            "device": "auto", "precision": "bf16", "attention": "auto",
            # the node takes steps of 256 (256 tokens ~ 21s): the smallest step that fits the line
            "max_new_tokens": min(4096, 256 * max(1, -(-int(12 * (len(text) / 4.0 * 2 + 2)) // 256))),
            "do_sample": True, "top_p": 0.9, "top_k": 50, "temperature": 0.8, "repetition_penalty": 1.05,
        })
        # the words of the reference clip: Qwen3-TTS then copies its rhythm and intonation too, not just
        # the timbre -- without them the lines came out flat and "AI-sounding" (user, 2026-09-22)
        graph["59"]["inputs"]["reference_text"] = reference_text or ""
        # voices without a transcript (목소리 2, 5 -- the 24 video's B/C) clone from the sound alone. The
        # Basic node did this by itself; the Advanced one fails "reference_text is required", so since
        # 2026-09-22 those voices could only replay cached lines (runs 110/111 cuts 12, 19: 2026-09-29).
        graph["59"]["inputs"]["x_vector_only"] = not (reference_text or "").strip()
        graph["59"]["inputs"]["seed"] = random.randint(0, 2**48 - 1)
        # Reloading the TTS model costs ~20-30s, and a scene needs several calls back to back
        # (two lines, each possibly chunked). Keep it resident across them and only drop it on the
        # final call, so the LTX render that follows still gets the whole 16GB card to itself.
        graph["59"]["inputs"]["unload_models"] = unload

        try:
            # 180s was fine for the short curated bank voices, but a longer/rawer personal reference
            # (e.g. a phone recording) can genuinely take well over 3 minutes per chunk -- confirmed
            # via timeout error in production, not just guessed
            outputs = comfy_client.run_prompt_and_wait(graph, timeout=240)
        except comfy_client.ComfyError as exc:
            # A timeout here turned out to be the SAME runaway-pause failure mode as a slow-but-
            # completed result, just more severe (measured: a job that hit this timed out at 467s
            # and produced 163s of audio for a 68-char line) -- more silence to synthesize means more
            # compute time too. Retry with a fresh seed instead of surfacing a bare 500.
            last_error = exc
            continue

        dest = VOICELINES_DIR / f"_chunk_{uuid4().hex}.mp3"
        comfy_client.save_output_to(outputs, dest, media_keys=["audio"])

        duration = comfy_client.audio_duration_seconds(dest)
        rate = len(text) / duration if duration > 0 else 0.0
        if rate > best_rate:
            if best_path is not None:
                best_path.unlink(missing_ok=True)
            best_path, best_rate = dest, rate
        else:
            dest.unlink(missing_ok=True)

        if rate >= _MIN_PLAUSIBLE_CHARS_PER_SEC:
            break

    if best_path is None:
        raise last_error
    return best_path


# A designed tone clip is the reference every later line of that voice+tone copies, so a bad one spoils
# them all. Seen: 지아 "지침" came out 31s for 40 chars (10s of dead air) and 영감's base clip looped to
# 108s. Good clips measured 2.7-7.4 chars/s and at most 17s (125 clips, 2026-09-30).
_TONE_CLIP_CPS = (2.2, 10.0)
_TONE_CLIP_MAX_SEC = 24.0
_TONE_CLIP_MAX_MISMATCH = 0.2  # 295 good stored clips heard 0.0-0.18; the 5 bad ones 0.22-1.2


def _design_tone_clip(voice: dict, style: dict) -> dict | None:
    """Make this voice's sample in that tone with Qwen3-TTS VoiceDesign and keep it, so pressing a 말투
    button works for any voice: the style's instruction goes straight into Qwen3 (user, 2026-09-23)."""
    if not voice.get("design_instruct") or not style.get("instruct"):
        return None
    text = style["sample_text"]
    for attempt in range(3):
        graph = {
            "1": {"class_type": "AILab_Qwen3TTSVoiceDesign_Advanced", "inputs": {
                "text": text, "instruct": f"{voice['design_instruct']} {style['instruct']} "
                "Sounds like a real person talking with a close friend, not a narrator: natural breathing and pauses.",
                "model_size": "1.7B", "device": "auto", "precision": "bf16", "language": "Korean",
                # 512 tokens ~ 42s: room for a slow 15s sample, but a looping take stops much sooner than 1024
                "max_new_tokens": 512, "do_sample": True, "top_p": 0.9, "top_k": 50, "temperature": 0.8,
                "repetition_penalty": 1.05, "attention": "auto", "unload_models": False,
                "seed": (abs(hash((voice["name"], style["key"]))) + attempt * 7919) % (2 ** 31)}},
            "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "voicedesign/tone"}},
        }
        outputs = comfy_client.run_prompt_and_wait(graph, timeout=600, poll_interval=2)
        raw = VOICELINES_DIR / f"_tone_{uuid4().hex}.flac"
        comfy_client.save_output_to(outputs, raw, media_keys=["audio", "audios"])
        wav = MEDIA_DIR / "voices" / f"{uuid4().hex}.wav"
        subprocess.run([FFMPEG_PATH, "-y", "-v", "error", "-i", str(raw), "-ac", "1", "-ar", "24000", str(wav)],
                       check=True)
        raw.unlink(missing_ok=True)
        sec = comfy_client.audio_duration_seconds(wav)
        cps = len(text) / sec if sec > 0 else 0.0
        # The clip is the clone's reference AND its text is the reference_text, so they must match: 5 of 300
        # stored clips did not (2026-10-02 check) -- laughing/sobbing "하... 하..." before the words, or garbled
        # speech ("빨리 지금 안다 뵈드죠?") -- and that is how extra words leaked into lines.
        if (sec <= _TONE_CLIP_MAX_SEC and _TONE_CLIP_CPS[0] <= cps <= _TONE_CLIP_CPS[1]
                and stt.mismatch(text, stt.transcribe(wav)) <= _TONE_CLIP_MAX_MISMATCH):
            db.put_voice_tone(voice["id"], style["key"], f"voices/{wav.name}", text)
            return db.get_voice_tone(voice["id"], style["key"])
        wav.unlink(missing_ok=True)  # looped or full of pauses: try another seed
    return None  # no usable clip: the line is spoken from the voice's own reference instead


def generate_voice_line(text: str, voice_id: int, unload_after: bool = True, tone: str = "") -> dict:
    """Return a cached voice_line for (text, voice_id), generating it via ComfyUI TTS if needed.
    Long lines are split into sentence-sized chunks, synthesized separately, and stitched back
    together so the voice stays clean instead of getting choppy on one long call.

    `unload_after=False` leaves the TTS model in VRAM for a following call -- pass it for every
    line but the last one of a scene."""
    # a tone is spoken by cloning from that voice's reference clip in the same tone: Qwen3-TTS copies the
    # delivery of the reference, and the clone model itself takes no mood (user, 2026-09-22: "톤 조절")
    tone_ref = db.get_voice_tone(voice_id, tone) if tone else None
    if tone and not tone_ref:
        style = db.get_voice_style(tone)
        v = db.get_voice(voice_id)
        if style and v:
            tone_ref = _design_tone_clip(v, style)
    key = f"{text} \u27e8{tone}\u27e9" if tone_ref else text
    cached = db.find_voice_line(key, voice_id)
    if cached:
        return {**cached, "cached": True}

    voice = db.get_voice(voice_id)
    if not voice:
        raise HTTPException(404, "voice not found")
    if tone_ref:
        voice = {**voice, "reference_audio_path": tone_ref["audio_path"], "reference_text": tone_ref["reference_text"]}

    reference_audio_filename = comfy_client.copy_to_comfy_input(MEDIA_DIR / voice["reference_audio_path"])
    chunks = _split_into_chunks(text, _MAX_TTS_CHARS)
    # Each take is heard back (Whisper) and redone when it does not say the line: a tone reference ending in
    # "...힘들어" leaked that word in front of the line, and some takes came out garbled (voice test
    # 2026-10-01). The best of up to _CHECKED_TRIES takes is kept.
    best: tuple[float, Path] | None = None
    # Short lines get a tighter bar: "아 진짜, 딱 한 판만 하자" came out "자, 딱 한판만 하자" (서연 soft, voice test
    # 2026-10-02) -- a whole word gone, yet only 2 of 9 letters = 0.22, under 0.25. One wrong letter in a short
    # line (Whisper's own slips, "코노나" -> "코너나") still passes.
    max_off = 0.15 if len(stt._core(re.sub(r"^\[v\d+\]\s*", "", text))) <= 12 else _CHECK_MAX_MISMATCH
    for attempt in range(_CHECKED_TRIES):
        chunk_paths = [
            _synthesize_chunk(chunk, reference_audio_filename, unload_after and i == len(chunks) - 1,
                              voice.get("reference_text") or "")
            for i, chunk in enumerate(chunks)
        ]
        take = VOICELINES_DIR / f"{voice_id}_{uuid4().hex}.mp3"
        _concat_audio(chunk_paths, take)
        off = stt.mismatch(text, stt.transcribe(take))
        if best is None or off < best[0]:
            if best:
                best[1].unlink(missing_ok=True)
            best = (off, take)
        else:
            take.unlink(missing_ok=True)
        if off <= max_off:
            break
    dest = best[1]

    real_duration = comfy_client.audio_duration_seconds(dest)
    if real_duration > 0:
        rate = len(text) / real_duration
        # Even with the retry-on-anomaly guard in _synthesize_chunk, a multi-chunk line's overall
        # rate could still land low; don't let a still-implausible sample pollute the voice's pace
        # calibration for future dialogue-length targeting.
        if rate >= _MIN_PLAUSIBLE_CHARS_PER_SEC:
            db.record_voice_pace(voice_id, rate)

    row = db.create_voice_line(key, voice_id, f"voicelines/{dest.name}")
    return {**row, "cached": False}


@router.post("")
def create_voice_line(payload: VoiceLineCreate):
    try:
        return generate_voice_line(payload.text, payload.voice_id)
    except comfy_client.ComfyError as exc:
        raise HTTPException(504, f"목소리 생성이 반복적으로 시간 초과됐습니다: {exc}") from exc
