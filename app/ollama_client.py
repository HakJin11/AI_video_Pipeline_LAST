import json
import re

import httpx

from app.config import COMFYUI_URL, OLLAMA_MODEL, OLLAMA_URL
from app.prompt_builder import paired_refs

_client = httpx.Client(base_url=OLLAMA_URL, timeout=180.0)

# ComfyUI (LTX 22B) and Ollama (gemma 12B) share one 16GB GPU. Unloading gemma after every call
# made each call pay a fresh 4-16s load (Ollama log 2026-09-15: 25 loads for 88 calls), and the
# script step fires 1 + N calls back to back. So keep it loaded while ComfyUI is idle, and drop it
# the moment the call returns while ComfyUI is working. comfy_client.run_prompt_and_wait unloads
# it before submitting any job, so a resident gemma never shares the card with a render.
_KEEP_ALIVE_IDLE = "5m"


def _gpu_options(options: dict) -> dict:
    """gemma always runs on the GPU (user, 2026-10-07: "GPU, not CPU" -- said many times). The old num_gpu 0 while
    ComfyUI rendered put gemma on the CPU: 45-68 s per call AND the LTX steps next to it went 1.6 -> 8.7 s
    (run 209 cut 1: 9 min). Every call now goes through gemma_turn(): it waits for the running ComfyUI job, frees
    the card, runs gemma on it, then the next ComfyUI job reloads its model (~10-20 s). That also avoids the
    2026-09-21 OOM (gemma loading onto a card LTX still held)."""
    return {k: v for k, v in options.items() if k != "num_gpu"}


class gemma_turn:
    """`with gemma_turn():` -- the GPU for gemma between two ComfyUI jobs (never next to one, never on the CPU)."""

    def __enter__(self):
        from app import comfy_client
        self._turn = comfy_client.gpu_turn()
        self._turn.__enter__()
        try:
            if not _gemma_on_gpu():
                comfy_client.free_vram(force=True)
        except Exception:  # noqa: BLE001 - freeing is best effort; the call still goes to the GPU
            pass
        return self

    def __exit__(self, *exc):
        self._turn.__exit__(*exc)


def gemma_post(url: str, **kw):
    """httpx.post to Ollama /api/generate on the GPU (see gemma_turn)."""
    js = kw.get("json")
    if isinstance(js, dict) and isinstance(js.get("options"), dict):
        js["options"] = _gpu_options(js["options"])
    with gemma_turn():
        return httpx.post(url, **kw)


def _gemma_client_post(path: str, **kw):
    js = kw.get("json")
    if isinstance(js, dict) and isinstance(js.get("options"), dict):
        js["options"] = _gpu_options(js["options"])
    with gemma_turn():
        return _client.post(path, **kw)


def _comfy_url() -> str:
    from app import comfy_client
    return comfy_client.base_url()


def _keep_alive() -> str | int:
    try:
        from app import comfy_client
        q = httpx.get(f"{comfy_client.base_url()}/queue", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return 0  # ComfyUI mid-render doesn't answer: treat as busy
    return 0 if q.get("queue_running") or q.get("queue_pending") else _KEEP_ALIVE_IDLE

_DEFAULT_TARGET_DURATION_SEC = 20  # whole-video target if the caller doesn't specify one
# Observed Korean TTS pacing for this voice bank -- measured from real generated audio (57 cached
# voice lines: weighted ~6.5 chars/sec, but individual lines ran up to ~7.9). The old 6.25 value
# under-asked gemma for text, so a 30s target reliably came out ~24s of actual speech; raised so a
# 30s target lands closer to 30s while keeping the already-accurate 10s/20s targets roughly unchanged.
_CHARS_PER_SEC = 7.3
_LINE_GAP_SEC = 1  # matches the pause videos.py inserts between the two lines


class OllamaError(RuntimeError):
    pass


def estimate_seconds(text: str, min_sec: int = 3) -> int:
    """Rough speaking-time estimate from character count, for building an LTX prompt before the
    real TTS audio (and its exact duration) exists yet."""
    return max(min_sec, round(len(text) / _CHARS_PER_SEC))


def _per_line_seconds(target_duration_sec: float) -> float:
    """Split a whole-video target between the two lines, leaving ~2s of headroom so the summed TTS
    speech lands a little under the target instead of overshooting it (20s target -> ~18s of
    voice, 10s -> ~8s)."""
    return max(3.0, (target_duration_sec - 2) / 2)


def _length_guidance(target_duration_sec: float, chars_per_sec: float | None = None) -> str:
    # Some cloned voices (e.g. a personal recording) speak meaningfully slower than the bank
    # voices _CHARS_PER_SEC was calibrated on -- if the caller knows which voice will read this
    # line, its own observed pace (db.voices.observed_chars_per_sec) gives a far more accurate
    # target than the global constant.
    rate = chars_per_sec or _CHARS_PER_SEC
    line_seconds = _per_line_seconds(target_duration_sec)
    min_chars = round(line_seconds * rate * 0.6)
    max_chars = round(line_seconds * rate)
    return (
        f"- 이 대사는 영상에서 약 {line_seconds:.1f}초 분량을 채워야 합니다. 소리 내어 자연스럽게 말했을 때 "
        f"{line_seconds:.1f}초 정도 걸리도록 {min_chars}자에서 {max_chars}자 사이로, 한두 문장으로 충분히 풀어서 씁니다."
    )


def _build_opening_prompt(
    keyword: str,
    speaker_name: str,
    speaker_desc: str,
    other_name: str,
    other_desc: str,
    target_duration_sec: float,
    chars_per_sec: float | None = None,
) -> str:
    return f"""당신은 짧은 영상용 한국어 대본을 쓰는 작가입니다.

키워드: {keyword}
말하는 사람: {speaker_name} ({speaker_desc})
상대방: {other_name} ({other_desc})

요구사항:
- {speaker_name}이(가) {other_name}에게 건네는 대사만 씁니다. 아직 상대방은 대답하지 않은 상태입니다.
- 키워드의 상황과 감정이 분명히 드러나는 자연스러운 구어체 한국어 대사로 씁니다.
{_length_guidance(target_duration_sec, chars_per_sec)}
- 다른 설명이나 지문 없이 아래 JSON 형식으로만 답하세요.

{{"line": "{speaker_name}의 대사"}}"""


def _build_reply_prompt(
    keyword: str,
    speaker_name: str,
    speaker_desc: str,
    other_name: str,
    other_desc: str,
    other_line: str,
    target_duration_sec: float,
    chars_per_sec: float | None = None,
) -> str:
    return f"""당신은 짧은 영상용 한국어 대본을 쓰는 작가입니다.

키워드: {keyword}
{other_name} ({other_desc})이(가) 방금 {speaker_name}에게 이렇게 말했습니다: "{other_line}"

말하는 사람: {speaker_name} ({speaker_desc})

요구사항:
- {speaker_name}이(가) 방금 들은 말에 자연스럽게 반응/대답하는 대사를 씁니다.
- 키워드의 상황과 감정이 분명히 드러나는 자연스러운 구어체 한국어 대사로 씁니다.
{_length_guidance(target_duration_sec, chars_per_sec)}
- 다른 설명이나 지문 없이 아래 JSON 형식으로만 답하세요.

{{"line": "{speaker_name}의 대사"}}"""


def _call_gemma_json(prompt: str) -> str:
    last_error: Exception | None = None
    for _ in range(2):
        resp = _gemma_client_post(
            "/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "think": False,
                "keep_alive": _keep_alive(),
                "options": _gpu_options({"temperature": 0.8}),
            },
        )
        if resp.status_code != 200:
            raise OllamaError(f"Ollama /api/generate failed ({resp.status_code}): {resp.text}")
        raw_text = resp.json().get("response", "")
        try:
            data = json.loads(raw_text)
            line = str(data["line"]).strip()
            if not line:
                raise ValueError("empty line in response")
            return line
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            last_error = exc
            continue
    raise OllamaError(f"Failed to parse dialogue line JSON from gemma response: {last_error}")


def generate_opening_line(
    keyword: str,
    speaker_name: str,
    speaker_desc: str,
    other_name: str,
    other_desc: str,
    target_duration_sec: float = _DEFAULT_TARGET_DURATION_SEC,
    chars_per_sec: float | None = None,
) -> str:
    return _call_gemma_json(
        _build_opening_prompt(
            keyword, speaker_name, speaker_desc, other_name, other_desc, target_duration_sec, chars_per_sec
        )
    )


def generate_reply_line(
    keyword: str,
    speaker_name: str,
    speaker_desc: str,
    other_name: str,
    other_desc: str,
    other_line: str,
    target_duration_sec: float = _DEFAULT_TARGET_DURATION_SEC,
    chars_per_sec: float | None = None,
) -> str:
    return _call_gemma_json(
        _build_reply_prompt(
            keyword, speaker_name, speaker_desc, other_name, other_desc, other_line,
            target_duration_sec, chars_per_sec,
        )
    )


# ---------- multi-scene story authoring ----------
#
# Two-phase, scene-by-scene: (1) one call for the plot beats (a concrete situation per scene,
# fitting each scene's assigned background, forming a 기승전결 arc), then (2) one call PER scene
# for that scene's two lines, given every earlier scene's situation + real dialogue verbatim as
# context. The per-scene call reuses the same proven pattern as the single-video
# generate_opening_line/generate_reply_line, just with running story context -- far better
# continuity than asking for all N scenes in one shot.


def per_scene_line_chars(total_sec: float, n_scenes: int, chars_per_sec: float | None = None) -> tuple[int, int]:
    """(min, max) Korean characters for one spoken line so a scene lands near total_sec/n_scenes.

    Pass the speaking voice's own measured pace (db.voices.observed_chars_per_sec). The bank
    average baked into _CHARS_PER_SEC is far faster than most cloned voices actually read -- using
    it made every line come out roughly twice as long in seconds as the target, which then forced
    the renderer to time-compress the speech and wrecked the lip-sync."""
    hi = line_char_limit(total_sec / max(1, n_scenes), chars_per_sec)
    return max(4, round(hi * 0.6)), hi


def line_char_limit(scene_sec: float, chars_per_sec: float | None = None) -> int:
    """Most characters one speaker's line can have in a scene of scene_sec seconds: both lines plus
    the 0.6s pause must fit, with 10% headroom for TTS pace jitter so the voice isn't squeezed."""
    rate = chars_per_sec or _CHARS_PER_SEC
    line_seconds = max(1.5, (scene_sec - 0.6) / 2) * 0.9
    return max(6, round(line_seconds * rate))


def _gemma_json(prompt: str, num_predict: int = 500, temperature: float = 0.85) -> dict:
    last_error: Exception | None = None
    for _ in range(2):
        resp = _gemma_client_post(
            "/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "think": False,
                "keep_alive": _keep_alive(),
                "options": _gpu_options({"temperature": temperature, "num_predict": num_predict}),
            },
        )
        if resp.status_code != 200:
            raise OllamaError(f"Ollama /api/generate failed ({resp.status_code}): {resp.text}")
        try:
            return json.loads(resp.json().get("response", ""))
        except json.JSONDecodeError as exc:
            last_error = exc
    raise OllamaError(f"Failed to parse JSON from gemma response: {last_error}")


def generate_story_beats(
    keyword: str,
    name1: str,
    desc1: str,
    name2: str,
    desc2: str,
    n_scenes: int,
    scene_settings: list[str],
) -> list[str]:
    """One call: the plot outline. Returns a concrete one-line Korean 'situation' per scene."""
    settings_block = "\n".join(
        f"- 씬 {i + 1} 장소: {s}" for i, s in enumerate(scene_settings)
    ) or "- 장소 자유"
    prompt = f"""당신은 짧은 영상 드라마의 플롯을 짜는 작가입니다.

키워드: {keyword}
인물1: {name1} ({desc1})
인물2: {name2} ({desc2})
장소(씬 순서대로 고정):
{settings_block}

두 인물만 나오는 {n_scenes}개 씬짜리 이야기의 씬별 상황을 짭니다.
- 전체가 기-승-전-결로 이어지는 하나의 이야기. 키워드의 감정이 마지막 씬에서 절정에 이르거나 해소됩니다.
- 각 씬에는 구체적인 사건이나 행동이 하나씩 있습니다 (그냥 "대화한다"가 아니라 무슨 일이 벌어지는지).
- 각 씬 상황은 그 씬에 지정된 장소에서 자연스럽게 벌어질 수 있어야 합니다.
- 뒤 씬은 앞 씬의 결과를 이어받습니다 (시간·장소가 바뀌어도 인과가 연결).
- situation은 한국어 한 문장. 대사는 쓰지 마세요 (다음 단계에서 씁니다).
- JSON으로만: {{"scenes": [{{"situation": "..."}}]}} — 배열 길이 정확히 {n_scenes}."""
    data = _gemma_json(prompt, num_predict=900)
    raw = data["scenes"] if isinstance(data, dict) and "scenes" in data else data
    beats = [str(s.get("situation", "")).strip() for s in raw if str(s.get("situation", "")).strip()]
    if not beats:
        raise OllamaError("gemma returned no story beats")
    while len(beats) < n_scenes:
        beats.append(beats[-1])
    return beats[:n_scenes]


def _scene_dialogue_prompt(
    keyword: str, name1: str, desc1: str, name2: str, desc2: str,
    prior_text: str, situation: str, setting: str,
    lo1: int, hi1: int, lo2: int, hi2: int, scene_no: int, n_scenes: int,
) -> str:
    arc = "이야기의 시작" if scene_no == 1 else ("마지막 씬 (키워드 감정의 절정/마무리)" if scene_no == n_scenes else f"{scene_no}번째 씬")
    return f"""당신은 짧은 영상 드라마의 대사를 쓰는 작가입니다.

키워드: {keyword}
인물1: {name1} ({desc1})
인물2: {name2} ({desc2})

지금까지의 이야기:
{prior_text or "(없음 - 지금이 첫 씬)"}

이번 씬 ({arc}):
- 장소: {setting}
- 상황: {situation}

요구사항:
- 이번 씬에서 {name1}이(가) 먼저 한마디 하고, 그 말을 들은 {name2}이(가) 자연스럽게 답합니다.
- 위 "지금까지의 이야기"와 인과가 분명히 이어지게 씁니다 (앞 씬의 사건·감정을 이어받아서).
- 이번 씬 상황과 장소에 맞는 내용으로, 키워드의 감정이 드러나게.
- {name1}의 대사(line1)는 {lo1}~{hi1}자, {name2}의 대사(line2)는 {lo2}~{hi2}자. 자연스러운 구어체 한국어, 한두 문장.
- 글자 수를 반드시 지키세요. 길면 영상에서 말이 잘립니다.
- 지문·설명 없이 JSON으로만: {{"line1": "...", "line2": "..."}}"""


def generate_scene_dialogue(
    keyword: str, name1: str, desc1: str, name2: str, desc2: str,
    prior_text: str, situation: str, setting: str,
    per_line_chars1: tuple[int, int], scene_no: int, n_scenes: int,
    per_line_chars2: tuple[int, int] | None = None,
) -> tuple[str, str]:
    """One call: this scene's two lines, given all earlier scenes verbatim as context. Each speaker
    gets its own character budget, sized to that speaker's voice pace."""
    lo1, hi1 = per_line_chars1
    lo2, hi2 = per_line_chars2 or per_line_chars1
    prompt = _scene_dialogue_prompt(
        keyword, name1, desc1, name2, desc2, prior_text, situation, setting,
        lo1, hi1, lo2, hi2, scene_no, n_scenes,
    )
    best: tuple[str, str] | None = None
    best_over = 0
    for _ in range(3):
        data = _gemma_json(prompt)
        line1, line2 = str(data.get("line1", "")).strip(), str(data.get("line2", "")).strip()
        if not (line1 and line2):
            continue
        # the upper bound is a hard limit (it's what the scene's seconds can hold); keep whichever
        # attempt overshoots least, and stop as soon as one fits
        over = max(0, len(line1) - hi1) + max(0, len(line2) - hi2)
        if best is None or over < best_over:
            best, best_over = (line1, line2), over
        if over == 0:
            break
    if best is None:
        raise OllamaError("gemma returned empty scene dialogue")
    return best


def generate_story_outline(
    premise: str,
    name1: str,
    desc1: str,
    name2: str,
    desc2: str,
    n_scenes: int,
    total_sec: float,
    scene_settings: list[str] | None = None,
    chars_per_sec1: float | None = None,
    chars_per_sec2: float | None = None,
) -> list[dict]:
    """Orchestrator: plot beats (1 call) then per-scene dialogue (N calls, each with the real
    text of every earlier scene). Returns [{situation, line1, line2}] x n_scenes.

    chars_per_sec1/2 are the two cast voices' measured reading paces, so each line is written to
    the length that voice will actually take to say it."""
    settings = scene_settings or ["장소 자유"] * n_scenes
    beats = generate_story_beats(premise, name1, desc1, name2, desc2, n_scenes, settings)
    budget1 = per_scene_line_chars(total_sec, n_scenes, chars_per_sec1)
    budget2 = per_scene_line_chars(total_sec, n_scenes, chars_per_sec2)

    out: list[dict] = []
    for i, situation in enumerate(beats):
        prior = "\n".join(
            f"[씬 {j + 1}] {out[j]['situation']}\n  {out[j]['line1']}\n  {out[j]['line2']}"
            for j in range(i)
        )
        setting = settings[i] if i < len(settings) else "장소 자유"
        try:
            line1, line2 = generate_scene_dialogue(
                premise, name1, desc1, name2, desc2, prior, situation, setting,
                budget1, i + 1, n_scenes, budget2,
            )
        except OllamaError:
            line1, line2 = "", ""
        out.append({"situation": situation, "line1": line1, "line2": line2})
    return out


def regenerate_scene_lines(
    premise: str,
    name1: str,
    desc1: str,
    name2: str,
    desc2: str,
    prev_scenes_summary: str,
    situation: str,
    total_sec: float,
    n_scenes: int,
    scene_no: int = 1,
    chars_per_sec1: float | None = None,
    chars_per_sec2: float | None = None,
) -> tuple[str, str]:
    """Rewrite one scene's two lines given the scenes before it (their real text)."""
    return generate_scene_dialogue(
        premise, name1, desc1, name2, desc2,
        prev_scenes_summary, situation, "위 상황에 맞는 장소",
        per_scene_line_chars(total_sec, n_scenes, chars_per_sec1), scene_no, n_scenes,
        per_scene_line_chars(total_sec, n_scenes, chars_per_sec2),
    )


def condense_line(line: str, max_chars: int) -> str:
    """Shorten one spoken line to <= max_chars, keeping its meaning, tone and speech level. Used when
    the synthesized voice turned out too long for the scene's share of the total length."""
    if len(line) <= max_chars:
        return line
    prompt = f"""다음 한국어 대사를 영상 속 한 사람이 말합니다. 뜻, 감정, 말투(존댓말/반말)는 그대로 두고
{max_chars}자 이내로 줄여 주세요. 설명 없이 JSON으로만 답하세요.

원래 대사: "{line}"

{{"line": "줄인 대사"}}"""
    best = line
    for _ in range(2):
        try:
            short = _call_gemma_json(prompt)
        except OllamaError:
            break
        if len(short) < len(best):
            best = short
        if len(best) <= max_chars:
            break
    return best


def _build_video_prompt_request(
    situation: str,
    char1_name: str,
    char1_desc: str,
    char2_name: str,
    char2_desc: str,
    background_desc: str,
    line1: str,
    line2: str,
    t1: int,
    t2: int,
    ref1: str,
    ref2: str,
) -> str:
    duration_sec = t1 + t2
    return f"""You are a prompt writer for the LTX-2 image-to-video generation model. Write ONE structured video \
generation prompt in English that will be used as-is, following EXACTLY this structure and nothing else \
(no markdown, no extra commentary before or after):

<a descriptive paragraph, 3-5 sentences, describing the two-shot scene, who speaks first and who replies, \
and that only one person's mouth moves at a time while the other listens with their mouth closed>

scene: <scene description>
character: <first character's reference> — <description>, and <second character's reference> — <description>
action: <describe, in two segments ("First ~{t1} seconds: ..." and "Remaining ~{t2} seconds: ..."), each speaker's \
actual physical MOTION during their own segment — not just a static pose. Pick exactly ONE simple, slow, continuous \
movement for the speaker (e.g. only a slight head tilt, OR only one hand rising partway, OR only a slow weight \
shift — not several stacked together) so the motion stays smooth rather than busy. The listening speaker should \
also have at most one small continuous motion (a slow nod, a slight sway, blinking) rather than staying frozen. \
End each segment's motion with the exact spoken line in quotes>
audio: <sequencing description>
gaze: <state that each person looks at the other person, never at the camera>
camera: <camera framing description>

STRICT RULES:
- Refer to the two speakers EXACTLY as "{ref1}" and "{ref2}" throughout (every section) — these phrases are \
visually checkable against the image, unlike bare names, and prevent the two speakers' identities from being \
confused with each other.
- "{ref1}"'s line and "{ref2}"'s line must appear EXACTLY as given below, character-for-character, inside double \
quotes. Do not translate, paraphrase, shorten, or alter them in any way.
- Total duration is {duration_sec} seconds: {ref1} speaks for about {t1} seconds, then {ref2} speaks for \
about {t2} seconds.
- Every segment (both the speaking one and the listening one) must include exactly one specific, continuous, SLOW \
physical motion described in concrete visual terms (what moves, which direction, how fast) — avoid vague words \
like "naturally" or "subtly" without describing the actual motion itself, and avoid combining multiple motions \
in the same segment.
- The motion must read as smooth, slow, and continuous — explicitly avoid sudden, jerky, fast, or exaggerated \
movement, and avoid any motion that would move a hand or arm in front of the face.
- The two people talk TO EACH OTHER, like actors in a filmed sketch: in the paragraph and in every action segment, state that both keep their eyes on the other person and never look into the camera lens.
- Predict and describe actions, gestures, facial expressions, AND motion that fit the situation and the emotional \
tone of each line.
- Output ONLY the prompt text described above.

Inputs:
situation: {situation}
scene description: {background_desc}
{ref1}: {char1_desc}
{ref2}: {char2_desc}
{ref1}'s line (first ~{t1} seconds): "{line1}"
{ref2}'s line (remaining ~{t2} seconds): "{line2}\""""


def generate_video_prompt(
    situation: str,
    char1_name: str,
    char1_desc: str,
    char2_name: str,
    char2_desc: str,
    background_desc: str,
    line1: str,
    line2: str,
    t1: int,
    t2: int,
    char1_gender: str | None = None,
    char2_gender: str | None = None,
) -> str:
    """Ask gemma to write the full LTX prompt (scene/action/audio/camera) around the given dialogue,
    predicting fitting actions/expressions. Caller should validate line1/line2 appear verbatim and
    fall back to the deterministic template if not. t1/t2 are each speaker's actual measured TTS
    duration in seconds."""
    ref1, ref2 = paired_refs(char1_name, char1_gender, char2_name, char2_gender)
    prompt = _build_video_prompt_request(
        situation, char1_name, char1_desc, char2_name, char2_desc, background_desc, line1, line2, t1, t2, ref1, ref2
    )
    resp = _gemma_client_post(
        "/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": _keep_alive(),
            "options": _gpu_options({"temperature": 0.7, "num_predict": 800}),
        },
    )
    if resp.status_code != 200:
        raise OllamaError(f"Ollama /api/generate failed ({resp.status_code}): {resp.text}")
    text = resp.json().get("response", "").strip()
    if not text:
        raise OllamaError("gemma returned an empty video prompt")
    return text


def translate(text: str, target: str) -> str:
    """Translate for the 따라하기 page: Korean dialogue -> English ("en"), English prompt text -> Korean ("ko").
    Keeps meaning and tone; returns plain text."""
    if target == "en":
        instr = ("Translate this casual Korean spoken line into natural casual English, keeping the tone "
                 "(slang, whining, surprise). Only the translation.")
    else:
        instr = ("다음 영어 영상 프롬프트를 자연스러운 한국어로 번역하세요. 뜻을 빠짐없이 옮기고, "
                 "설명이나 덧붙임 없이 번역문만 쓰세요.")
    prompt = (instr + "\n\nText: " + json.dumps(text, ensure_ascii=False)
              + '\n\nAnswer as JSON: {"translation": "..."}')
    data = _gemma_json(prompt, num_predict=900, temperature=0.2)
    out = str(data.get("translation", "")).strip()
    if not out:
        raise OllamaError("empty translation")
    return out



def _comfy_busy() -> bool:
    try:
        from app import comfy_client
        q = httpx.get(f"{comfy_client.base_url()}/queue", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return True
    return bool(q.get("queue_running") or q.get("queue_pending"))


def translate_prompt_text(text: str, target: str) -> str:
    """번역기: Korean <-> English for video prompts (gemma on the GPU between ComfyUI jobs, see gemma_turn)."""
    if target == "en":
        instr = ("Translate the following Korean text into natural English for an AI video-generation prompt. "
                 "Keep every detail (who, clothes, place, gaze, motion, camera). Keep anything already in "
                 "English as it is. Output only the translation.")
    else:
        instr = ("다음 영어 텍스트를 자연스러운 한국어로 번역하세요. AI 영상 프롬프트이므로 인물·옷·장소·시선·움직임·카메라 "
                 "내용을 하나도 빼지 말고 옮기세요. 번역문만 쓰세요.")
    options = {"temperature": 0.2, "num_predict": 1200}
    resp = _gemma_client_post("/api/generate", json={
        "model": OLLAMA_MODEL,
        "prompt": f"{instr}\n\n---\n{text}\n---",
        "stream": False,
        "think": False,
        "keep_alive": _keep_alive(),
        "options": options,
    }, timeout=300)
    if resp.status_code != 200:
        raise OllamaError(f"Ollama /api/generate failed ({resp.status_code}): {resp.text}")
    out = resp.json().get("response", "").strip().strip("-").strip()
    if not out:
        raise OllamaError("empty translation")
    return out



def restyle_line(line: str, tone_ko: str) -> str:
    """Rewrite how a line is WRITTEN so the TTS delivers it in the wanted tone (drawn-out vowels,
    punctuation, interjections). The words themselves stay the same -- Qwen3-TTS has no mood input."""
    # 2026-10-01: with "..." after every word ("아... 진짜... 딱... 한 판만...") a 13-letter line took 4-6 s
    # and the cut grew; one or two marks carry the tone without stretching the line
    prompt = f"""다음 한국어 대사를 {tone_ko} 말하는 것처럼 표기만 바꿔 주세요.
단어와 뜻, 글자는 그대로 두고, 늘어뜨리기(~, ...), 쉼표, 느낌표, 물음표 같은 표기만 조절하세요.
'...'와 '~'는 합쳐서 최대 두 군데만, 말 길이가 거의 그대로 유지되게 하세요.
새로운 단어를 넣지 말고, 설명 없이 JSON으로만 답하세요.

원래 대사: "{line}"

{{"line": "바꾼 대사"}}"""
    try:
        out = _call_gemma_json(prompt)
    except OllamaError:
        return line
    # keep it only if it is the same line with different spelling, not a rewrite: the Hangul must be the
    # same apart from a stretched vowel or two (gemma wrote "하쟈아", "한 판만ㄴ", "어?" for "아")
    import re
    core = lambda t: re.sub(r"[^가-힣]", "", t)  # noqa: E731
    if not out or not (0.6 * len(line) <= len(out) <= 1.6 * len(line) + 4):
        return line
    a, b = core(line), core(out)
    if out.count("...") + out.count("…") + out.count("~") > 2 or len(b) - len(a) > 2 or not _same_words(a, b):
        return line
    return out


def _same_words(a: str, b: str) -> bool:
    """b is a with at most two extra syllables inserted (a drawn-out "아아", "하자아") -- nothing changed."""
    i = 0
    for ch in b:
        if i < len(a) and ch == a[i]:
            i += 1
    return i == len(a)


# ---------------------------------------------------------------- ✨ AI로 정리하기 (2026-10-02)
# The user: "if I write a prompt roughly, gemma should tidy it up". Three kinds of text are cleaned up -- a place
# (background picture), a cut's action line, a whole LTX prompt -- each following the rules the tests found
# (empty places, gaze said concretely, no mouth shapes, the person stays put, English for the video model).

class RefineError(OllamaError):
    pass


def _refine_call(prompt: str, num_predict: int) -> tuple[dict, float, bool]:
    """gemma JSON for a clean-up the user is waiting on: (answer, seconds, ran_on_gpu). Always on the card: if a
    ComfyUI job is running, this waits for it to end (the render queue then waits a few seconds for this) --
    gemma on the CPU next to LTX slowed both several times over (2026-10-05)."""
    import time

    from app import comfy_client
    t = time.monotonic()
    with comfy_client.gpu_turn():
        if not _gemma_on_gpu():
            comfy_client.free_vram(force=True)
        last: Exception | None = None
        for _ in range(2):
            resp = _client.post("/api/generate", json={
                "model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False, "think": False,
                "keep_alive": "2m", "options": {"temperature": 0.3, "num_predict": num_predict}}, timeout=300)
            if resp.status_code != 200:
                raise RefineError(f"Ollama /api/generate failed ({resp.status_code}): {resp.text[:200]}")
            try:
                return json.loads(resp.json().get("response", "")), round(time.monotonic() - t, 1), True
            except json.JSONDecodeError as exc:
                last = exc
    raise RefineError(f"gemma did not answer in JSON: {last}")


_PEOPLE_WORDS = re.compile(r"\b(people|person|man|woman|men|women|customer|customers|crowd|staff|"
                           r"student|students|actor|actors|someone|couple|friends?)\b", re.I)


_NO_PEOPLE = re.compile(r"(,\s*)?\b(with\s+)?(no|without)\s+(people|person|one|humans?|crowds?|customers?|staff|"
                        r"text|signs?|lettering|logos?)(\s+(around|present|in sight))?\b", re.I)


def _gemma_on_gpu() -> bool:
    try:
        models = _client.get("/api/ps", timeout=5).json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return False
    return any(m.get("name", "").startswith(OLLAMA_MODEL.split(":")[0]) and (m.get("size_vram") or 0) > 0
               for m in models)


def refine_background(text: str) -> dict:
    """A rough place ("비오는 편의점 밤 좀 어둡게") -> {"name", "ko", "en"} for an EMPTY background picture."""
    prompt = f"""너는 AI 이미지 생성용 '배경(장소) 프롬프트'를 다듬는 도우미야.
사용자가 대충 쓴 장소 설명을, 사람이 한 명도 없는 빈 장소 사진을 그리기 좋은 프롬프트로 정리해.

규칙:
1. 사용자가 말한 장소·분위기·시간대·날씨는 반드시 지켜. 다른 장소로 바꾸지 마.
2. 순서: 어떤 장소인지 → 눈에 보이는 것 3~5개(가구·소품·벽·바닥 색) → 빛과 시간대.
3. 사람, 손님, 직원, 동물은 절대 쓰지 마. 읽을 수 있는 글자·간판·로고도 쓰지 마.
4. 사용자가 말하지 않은 큰 요소(바다, 산 같은 풍경)는 지어내지 말고, 그 장소에 흔한 소품만 보태.
5. ko: 한국어 한두 문장(60~110자). en: 같은 내용의 자연스러운 영어 이미지 프롬프트(쉼표로 이어도 됨).
6. name: 장소 이름만 짧게 (10자 이내, 예: "비 오는 편의점").

사용자 입력: {json.dumps(text.strip(), ensure_ascii=False)}

JSON으로만 답해: {{"name": "...", "ko": "...", "en": "..."}}"""
    data, sec, gpu = _refine_call(prompt, 400)
    name, ko, en = (str(data.get(k, "")).strip() for k in ("name", "ko", "en"))
    if not ko or not en:
        raise RefineError("empty clean-up")
    # people in the place come out as strangers next to the cast (store run, 2026-10-02): drop those words -- a
    # "no people" phrase as a whole (the picture prompt already says the place is empty), then any other mention
    en = re.sub(r"\b(empty|devoid|free|void) of (people|persons?|humans?|customers?|anyone)\b", "empty", en, flags=re.I)
    en = _NO_PEOPLE.sub("", en)
    en = _PEOPLE_WORDS.sub("", en)
    en = re.sub(r"\b(empty|devoid|free) of\s*([.,])", r"empty\2", en, flags=re.I)
    en = re.sub(r"\s+([,.])", r"\1", re.sub(r"(,\s*)+,", ",", en)).strip(" ,")
    return {"name": name[:16] or ko[:12], "ko": ko, "en": " ".join(en.split()), "sec": sec, "gpu": gpu}


def refine_action(text: str, who: str, pron: str, line: str, place: str, side: str) -> dict:
    """A rough note on what the actor does ("놀라서 친구 쳐다봄 손 흔들고") -> {"en", "ko"}: one clean English action
    line in the shape the cut prompts use."""
    poss = {"she": "her", "he": "his"}.get(pron, "their")
    prompt = f"""You turn a rough note about what an actor does in a 1-2 second shot into ONE clean English action
line for an AI video model.

Context: the speaker is {who}, in {place}. The friend they talk to stands just off-screen to the {side}.
Their spoken line (Korean, it comes from the audio track): {json.dumps(line or "(no line)", ensure_ascii=False)}

Rules:
- One short sentence, present tense, starting with a verb, e.g. "raises {poss} eyebrows and glances at the
  shelves, then back at the friend". At most two small movements.
- Keep exactly what the note asks for (feeling, gesture, where the eyes go). Add no new props, people or places.
- When the note says where they look, say it concretely. Never make them look into the camera unless the note
  asks for it.
- Do not describe the mouth (open mouth, parted lips, agape, smiling mouth is fine as "smiles") -- the voice moves
  the mouth. Hands stay below the chin.
- They stay on the same spot: no walking, turning around or leaving. The friend is not in the picture: no touching
  the friend (nudging, tapping, hugging) -- turn it into a gesture toward them. If the note asks to walk away or leave,
  write a small "about to leave" movement instead (e.g. "glances toward the door and shifts her weight").
- Use {pron}/{poss} for the person.
- "ko": what the English line means, in natural Korean.

Note (Korean or English, may be rough): {json.dumps(text.strip(), ensure_ascii=False)}

Answer JSON only: {{"en": "...", "ko": "..."}}"""
    data, sec, gpu = _refine_call(prompt, 300)
    en, ko = str(data.get("en", "")).strip().rstrip("."), str(data.get("ko", "")).strip()
    en = re.sub(r"^(she|he|they|the (man|woman|person))\s+", "", en, flags=re.I)  # the prompt puts it after a comma
    if not en or re.search(r"[가-힣]", en):
        raise RefineError("no English action")
    warn = None
    if _WALK.search(en):
        warn = "걸어 나가거나 뒤돌면 얼굴이 사라지고 다른 사람이 들어오는 장면이 되기 쉬워요 (1~2초 컷은 제자리 동작이 잘 나와요)"
    elif re.search(r"\b(camera|lens|viewer)\b", en, re.I):
        warn = "카메라를 보게 했어요 — 원하신 게 아니면 '친구를 본다'로 바꿔 주세요"
    return {"en": en, "ko": ko, "warn": warn, "sec": sec, "gpu": gpu}


_WALK = re.compile(r"\b(walks?|walking|leaves?|leaving|turns? around|turns? (her|his|their) back|exits?)\b", re.I)


def refine_ltx_prompt(text: str, current: str) -> dict:
    """A rough wish for the whole shot ("카메라 천천히 다가가고 더 신나게") applied to the cut's current prompt ->
    {"prompt", "ko"} in LTX-2.3's shape (paragraph + scene/character/action/audio/camera lines)."""
    prompt = f"""You rewrite one 1-2 second shot's prompt for the LTX-2.3 video model according to a user's rough note.
Start from the CURRENT prompt and change only what the note asks for; keep everything else -- the person, outfit,
place and the "audio:" line -- exactly as it is.

Format (keep it exactly): one descriptive paragraph, an empty line, then the lines "scene: ...", "character: ...",
"action: ...", "audio: ...", "camera: ..." (one line each).

Rules:
- English only. Never quote the dialogue, never write Korean letters or the word "Korean", no subtitles or text.
- Describe what happens, not what must not happen.
- The person looks at the friend beside the camera unless the note says to look elsewhere; never into the lens
  unless the note asks for it.
- No mouth-shape descriptions (open mouth, parted lips): the audio moves the mouth.
- The person stays on the same spot; no one else walks in.
- "ko": one Korean sentence saying what you changed.
- "looks_into_camera": true if your new prompt makes the person look into the lens / at the viewer.
- "adds_person": true if your new prompt puts another person next to them or near the camera.

CURRENT prompt:
---
{current}
---
User note (Korean or English, may be rough): {json.dumps(text.strip(), ensure_ascii=False)}

Answer JSON only: {{"prompt": "...", "ko": "...", "looks_into_camera": false, "adds_person": false}}"""
    data, sec, gpu = _refine_call(prompt, 900)
    out = str(data.get("prompt", "")).strip()
    out = re.sub(r"\bKorean\s+", "", out)
    missing = [k for k in ("scene:", "character:", "action:", "audio:", "camera:") if k not in out]
    if re.search(r"[가-힣]", out):
        raise RefineError("영상 프롬프트에는 한글(자막·글자)을 넣을 수 없어요 — 글자는 영상에 깨져 나와요. 글자 없이 바라는 점만 적어 주세요")
    if not out or missing:
        raise RefineError("AI가 프롬프트 모양(scene·character·action·audio·camera 줄)을 지키지 못했어요. 한 번 더 눌러 주세요")
    # the audio line is what keeps the lips on the voice: always the current one, word for word
    cur_audio = next((l for l in current.splitlines() if l.strip().startswith("audio:")), None)
    if cur_audio:
        out = "\n".join(cur_audio if l.strip().startswith("audio:") else l for l in out.splitlines())
    # gemma says itself what it changed (wording checks missed "a friend appears next to her", 2026-10-03)
    warn = None
    single = not re.search(r"\btwo people\b", current)
    if single and data.get("adds_person") is True:
        warn = ("두 사람이 같이 나오게 하려면 컷 설정의 '화면 속 인원: 2명'을 고르세요 — 프롬프트만 바꾸면 첫 장면에 없던 "
                "친구가 걸어 들어오는 영상이 되기 쉬워요")
    elif data.get("looks_into_camera") is True:
        warn = "카메라를 보고 말하게 바꿨어요 — 원하신 게 아니면 '친구를 보며'라고 다시 적어 주세요"
    return {"prompt": out, "ko": str(data.get("ko", "")).strip(), "warn": warn, "sec": sec, "gpu": gpu}


def suggest_action(line: str, tone: str, who: str, pron: str, place: str, side: str, prev_line: str = "",
                   next_line: str = "") -> dict:
    """✨ AI가 행동 정하기 (2026-10-05, user: "let it decide the action too, and put it in the prompt"): from the
    line, its 말투 and the lines around it, one visible acting choice for a 1-2 s cut -- with a body movement, since
    face-only actions gave clips that hardly moved."""
    poss = {"she": "her", "he": "his"}.get(pron, "their")
    prompt = f"""You are directing a short Korean comedy sketch. Choose what the actor DOES in one 1-2 second shot.

The actor is {who}, in {place}. The friend they talk to stands just off-screen to the {side}.
Previous line (someone else): {json.dumps(prev_line or "-", ensure_ascii=False)}
THIS line (spoken by the actor, from the audio): {json.dumps(line or "(says nothing, only reacts)", ensure_ascii=False)}
Tone of voice: {tone or "natural"}
Next line (someone else): {json.dumps(next_line or "-", ensure_ascii=False)}

Write ONE English action line for an AI video model:
- present tense, starts with a verb, e.g. "throws {poss} hands up in disbelief and glances at the friend".
- exactly one clear, visible movement of the HANDS, ARMS or SHOULDERS that fits the line's meaning and tone
  (throws {poss} hands up, points, claps, waves a hand dismissively, shrugs with arms spread, presses {poss} palms
  together, grabs the friend's attention with a hand) -- a nod, a sigh or rolling the eyes alone is too small to
  see; plus the facial expression and where the eyes go (usually the friend on the {side}).
- small enough for 1-2 seconds; the actor stays on the same spot (no walking, no turning away).
- the friend is NOT in the picture: never touch the friend (no nudging, tapping, hugging) -- gesture toward them.
- no props that are not obviously there; no mouth shapes (the voice moves the mouth); hands below the chin;
  never look into the camera.
- use {pron}/{poss}.
"ko": the same in natural Korean.

Answer JSON only: {{"en": "...", "ko": "..."}}"""
    data, sec, gpu = _refine_call(prompt, 200)
    en, ko = str(data.get("en", "")).strip().rstrip("."), str(data.get("ko", "")).strip()
    en = re.sub(r"^(she|he|they|the (man|woman|person))\s+", "", en, flags=re.I)
    if not en or re.search(r"[가-힣]", en):
        raise RefineError("no English action")
    return {"en": en, "ko": ko, "sec": sec, "gpu": gpu}


def refine_shot(text: str, current_action: str, current_camera: str, place: str, pron: str) -> dict:
    """✍ 자세히 쓰기 / ✨ 프롬프트 정리, the fast way (2026-10-05): gemma writes only the parts that change -- the
    action, the camera move, extra atmosphere -- and the prompt is rebuilt from the cut's own parts. Rewriting the
    whole 1,500-character prompt took 2+ minutes on the CPU while a render was running."""
    poss = {"she": "her", "he": "his"}.get(pron, "their")
    prompt = f"""A user describes how one 1-2 second video shot should change. Turn the note into short English parts.

Current action: {json.dumps(current_action or "-", ensure_ascii=False)}
Current camera: {json.dumps(current_camera or "locked off", ensure_ascii=False)}
Place: {place}
User note (Korean or English, may be rough, may list 표정/손·팔 몸짓/시선/몸 움직임/카메라/분위기·빛/기타):
{json.dumps(text.strip(), ensure_ascii=False)}

Answer JSON only:
{{"action": "everything the person does in this shot -- expression, hand/arm gesture, body movement and where the
eyes go -- as one English clause starting with a verb, using {pron}/{poss}; keep the current action's parts the
note does not change; no mouth shapes; empty string if the note does not touch the acting",
"camera": "the camera move in a few English words (e.g. 'slowly pushes in toward the face'), empty if unchanged",
"scene_extra": "extra light / weather / atmosphere / background details in English, empty if none",
"ko": "한국어 한 문장: 무엇을 바꿨는지",
"looks_into_camera": true or false (the new action makes the person look into the lens),
"adds_person": true or false (the note puts another person next to them)}}"""
    data, sec, gpu = _refine_call(prompt, 260)
    out = {k: str(data.get(k, "") or "").strip().rstrip(".") for k in ("action", "camera", "scene_extra", "ko")}
    for k in ("action", "camera", "scene_extra"):
        if re.search(r"[가-힣]", out[k]):
            raise RefineError("영상 프롬프트에는 한글(자막·글자)을 넣을 수 없어요 — 글자는 영상에 깨져 나와요. 글자 없이 바라는 점만 적어 주세요")
    out["action"] = re.sub(r"^(she|he|they|the (man|woman|person))\s+", "", out["action"], flags=re.I)
    # gemma also said "looks into the camera" for a camera move ("카메라 천천히 다가감", 10 s run 195): only an action
    # that itself turns the eyes to the lens counts
    into_lens = re.search(r"\b(into|at|toward|towards|to)\s+(the\s+)?(camera|lens|viewer)", out["action"], re.I)
    out.update(looks_into_camera=data.get("looks_into_camera") is True and bool(into_lens),
               adds_person=data.get("adds_person") is True, sec=sec, gpu=gpu)
    return out


def pick_motions(cuts: list[dict], library: list[dict]) -> dict:
    """🎬 AI가 행동 정하기 (2026-10-07): gemma PICKS one tested motion per cut instead of writing a new line -- its own
    short lines hardly moved anyone; the library lines are written out step by step and were checked in test renders
    (db.motion_library). cuts: [{"n", "who", "line", "tone", "closeup", "prev", "next"}];
    library: [{"key", "label", "action_en", "closeup_ok"}] -> {"picks": {n: key}, "sec", "gpu"}"""
    menu = "\n".join(f'- {m["key"]}: {m["label"]} -- {" ".join(m["action_en"].split()[:16])}...'
                      + ("" if m["closeup_ok"] else " [not for close-ups]") for m in library)
    lines = "\n".join(
        f'{c["n"]}. {c["who"]}{" (FACE CLOSE-UP)" if c.get("closeup") else ""}, tone {c["tone"] or "natural"}, '
        f'says: {json.dumps(c["line"] or "(nothing, only reacts)", ensure_ascii=False)}' for c in cuts)
    prompt = f"""You are directing a short Korean comedy sketch. For EACH numbered 1-2 second shot, pick the ONE movement
from the menu that best fits what the actor says and how (tone). The shots follow each other as one conversation.
Do not give the same person the same movement twice in a row. For a FACE CLOSE-UP pick only a movement without
"[not for close-ups]".

Menu:
{menu}

Shots:
{lines}

Answer JSON only: {{"picks": [{{"n": 1, "key": "..."}}, ...]}}"""
    data, sec, gpu = _refine_call(prompt, 30 * len(cuts) + 60)
    keys = {m["key"]: m for m in library}
    out = {}
    for a in data.get("picks") or []:
        try:
            n = int(a.get("n"))
        except (TypeError, ValueError):
            continue
        k = str(a.get("key", "")).strip()
        if k in keys:
            out[n] = k
    if not out:
        raise RefineError("no picks")
    return {"picks": out, "sec": sec, "gpu": gpu}


def suggest_actions_all(cuts: list[dict]) -> dict:
    """🎬 모든 컷 행동 정하기 in ONE gemma call (2026-10-06): eight separate calls each waited for the GPU between the
    people pictures being drawn and swapped the 20 GB picture model out and back -- 5 min instead of 25 s.
    cuts: [{"n", "who", "pron", "line", "tone", "side"}] -> {"actions": {n: {"en", "ko"}}, "sec", "gpu"}"""
    lines = "\n".join(
        f'{c["n"]}. {c["who"]} ({c["pron"]}), friend off-screen to the {c["side"]}, tone {c["tone"] or "natural"}, '
        f'says: {json.dumps(c["line"] or "(nothing, only reacts)", ensure_ascii=False)}' for c in cuts)
    prompt = f"""You are directing a short Korean comedy sketch. For EACH numbered 1-2 second shot below, choose what the
actor DOES. The shots follow each other, so the actions should feel like one conversation.

{lines}

For each shot write ONE English action line for an AI video model:
- present tense, starts with a verb; exactly one clear, visible movement of the HANDS, ARMS or SHOULDERS that fits the
  line and tone (throws hands up, points, claps, waves a hand dismissively, shrugs with arms spread, presses palms
  together, pumps a fist) plus the facial expression. A nod, a sigh or rolling the eyes alone is too small.
- small enough for 1-2 seconds; the actor stays on the same spot (no walking, no turning away).
- the friend is NOT in the picture: never touch the friend; no mouth shapes; hands below the chin; never look into
  the camera. Use the given pronoun.
- "ko": the same in natural Korean.

Answer JSON only: {{"actions": [{{"n": 1, "en": "...", "ko": "..."}}, ...]}}"""
    data, sec, gpu = _refine_call(prompt, 90 * len(cuts) + 100)
    out = {}
    for a in data.get("actions") or []:
        try:
            n = int(a.get("n"))
        except (TypeError, ValueError):
            continue
        en = re.sub(r"^(she|he|they|the (man|woman|person))\s+", "", str(a.get("en", "")).strip().rstrip("."), flags=re.I)
        if en and not re.search(r"[가-힣]", en):
            out[n] = {"en": en, "ko": str(a.get("ko", "")).strip()}
    if not out:
        raise RefineError("no actions")
    return {"actions": out, "sec": sec, "gpu": gpu}
