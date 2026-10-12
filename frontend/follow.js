// =====================================================================
// 유튜브 따라하기 — 원본을 1~2초 컷으로 나누고 컷마다 AI 영상 + 프롬프트 해설
// (app.js 의 $, getJSON, postJSON, patchJSON, del, toast, state 를 같이 씀)
// =====================================================================
const F_MODE_KEY = "aivp.mode";
const F_PROJECT_KEY = "aivp.followId";
let fp = null;          // 현재 프로젝트 detail
let fCutIdx = 0;
let fPoll = null;
let fAutoAssemble = null; // project id: assemble as soon as its last cut is done
let fAutoAssembleStart = null, fAutoTick = null;

const fEsc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
// stored times are UTC ("2026-09-22T00:35:43+00:00"); show them in the viewer's own time
function fLocalTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(5, 16).replace("T", " ");
  const two = (n) => String(n).padStart(2, "0");
  return `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
}
const fStore = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, String(v)); } catch {} };
const fLoad = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const fActive = (c) => c.status === "queued" || c.status === "rendering";
const fSec = (s) => `${Number(s).toFixed(1)}초`;
let fAdmin = false;     // 관리자 화면: 가져오기 · 프롬프트 다듬기 · 공개

function setMode(mode) {
  if (mode !== "admin" || $("mode-admin").hidden) mode = "follow";  // 스토리 빌더는 2026-10-07에 뺐음
  fStore(F_MODE_KEY, mode);
  const follow = mode === "follow" || mode === "admin";
  const wasAdmin = fAdmin;
  fAdmin = mode === "admin";
  // 관리자 프리셋과 사용자 실행본은 서로의 화면에서 열지 않음
  if (fp && wasAdmin !== fAdmin) { fp = null; fStore(F_PROJECT_KEY, null); }
  $("follow").hidden = !follow;
  $("wiz").hidden = true;
  $("stepper").hidden = true;
  document.querySelectorAll("#mode-switch button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  document.querySelector(".brand-name").textContent = fAdmin ? "따라하기 관리자" : "유튜브 따라하기";
  if (follow) fRenderAll();
}

// ---------------------------------------------------------------- 시작 화면
async function fLoadList() {
  $("f-admin-start").hidden = !fAdmin;
  $("f-viewer-start").hidden = fAdmin;
  if (fAdmin) {
    const list = await getJSON("/api/follow?kind=template");
    $("f-list").innerHTML = list.length
      ? list.map((p) => `<button class="import-item" data-id="${p.id}">${p.published ? "🟢" : "⚪"} ${fEsc(p.title)} <span class="mini-note">${fSec(p.length_sec)}</span></button>`).join("")
      : `<span class="mini-note">아직 없습니다</span>`;
    $("f-list").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => fOpen(+b.dataset.id)));
    return;
  }
  const [cat, runs] = await Promise.all([getJSON("/api/follow/catalog"), getJSON("/api/follow?kind=run")]);
  $("f-catalog").innerHTML = cat.length ? cat.map((t) => `
    <div class="f-cat-card" data-id="${t.id}">
      <div class="f-cat-thumb">${t.thumb_url ? `<img src="${t.thumb_url}" />` : ""}<span>최대 ${fSec(t.length_sec)}</span></div>
      <div class="f-cat-body"><b>${fEsc(t.title)}</b>
        <div class="mini-note">${fEsc(t.description || "")}</div>
        <div class="mini-note">인물 ${t.cast_count}명 · 길이를 골라 시작</div>
        <div class="chip-row" style="margin-top:6px">${t.length_options.map((n) => `<button class="chip f-cat-len" data-len="${n}">▶ ${n}초 만들기</button>`).join("")}</div>
      </div>
    </div>`).join("") : `<div class="summary muted">아직 선택할 수 있는 영상이 없습니다. 관리자가 영상을 준비하면 여기에 나타납니다.</div>`;
  $("f-catalog").querySelectorAll(".f-cat-len").forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest(".f-cat-card");
    card.querySelectorAll(".f-cat-len").forEach((x) => { x.disabled = true; });
    try {
      fp = await postJSON(`/api/follow/catalog/${card.dataset.id}/start`, { length_sec: +b.dataset.len });
      fStore(F_PROJECT_KEY, fp.id); fCutIdx = 0; fRenderAll();
    } catch (e) { toast(fEsc(e.message), "bad"); card.querySelectorAll(".f-cat-len").forEach((x) => { x.disabled = false; }); }
  }));
  $("f-runs-sum").textContent = `내가 만든 따라하기 보기 (${runs.length}개)`;
  $("f-runs").innerHTML = runs.length
    ? runs.map((p) => `<button class="import-item" data-id="${p.id}">🎞 ${fEsc(p.title)} · ${Math.round(p.length_sec)}초 <span class="mini-note">${fLocalTime(p.created_at)}</span></button>`).join("")
    : `<span class="mini-note">아직 없습니다</span>`;
  $("f-runs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => fOpen(+b.dataset.id)));
}

async function fCreateFromUrl(ev) {
  ev.preventDefault();
  $("f-new-progress").hidden = false;
  try {
    fp = await postJSON("/api/follow/from-url", {
      url: $("f-url-input").value.trim(), title: $("f-url-title").value.trim(),
      start_sec: parseFloat($("f-url-start").value || "0"), length_sec: parseFloat($("f-url-length").value || "10"),
      cast_count: parseInt($("f-url-cast").value || "3", 10),
    });
    fCutIdx = 0; fStore(F_PROJECT_KEY, fp.id);
    toast(`컷 ${fp.cuts.length}개로 나눴습니다`);
    fRenderAll();
  } catch (e) {
    let msg = e.message; try { msg = JSON.parse(msg).detail || msg; } catch {}
    toast("가져오기 실패: " + fEsc(msg), "bad", 12000);
  } finally { $("f-new-progress").hidden = true; }
}

async function fCreate(ev) {
  ev.preventDefault();
  const file = $("f-video").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("video", file);
  fd.append("title", $("f-title").value.trim());
  fd.append("source_url", $("f-url").value.trim());
  fd.append("start_sec", $("f-start-sec").value || "0");
  fd.append("length_sec", $("f-length").value || "10");
  fd.append("cast_count", $("f-cast-count").value || "3");
  $("f-new-progress").hidden = false;
  try {
    const r = await fetch("/api/follow", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    fp = await r.json();
    fCutIdx = 0;
    fStore(F_PROJECT_KEY, fp.id);
    toast(`컷 ${fp.cuts.length}개로 나눴습니다`);
    fRenderAll();
  } catch (e) {
    toast("영상 처리 실패: " + fEsc(e.message), "bad");
  } finally {
    $("f-new-progress").hidden = true;
  }
}

function fRestart(mode) {
  if (fPoll) { clearInterval(fPoll); fPoll = null; }
  fp = null;
  fCutIdx = 0;
  fStore(F_PROJECT_KEY, null);
  $("tr-panel").hidden = true;
  setMode(mode);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function fOpen(id) {
  try {
    // the voice list may have changed since the page was opened (new voices, old ones taken out)
    try { state.voices = await getJSON("/api/voices"); } catch {}
    fp = await getJSON(`/api/follow/${id}`);
    fStore(F_PROJECT_KEY, id);
    fCutIdx = 0;
    if (fp.locked) postJSON(`/api/follow/${id}/prefetch`, {}).then((r) => { if (fp && fp.id === id) { fp.prefetch = r.prefetch; fRenderPrefetch(); fEnsurePrefetchPoll(); } }).catch(() => {});
  } catch { fp = null; fStore(F_PROJECT_KEY, null); }
  fRenderAll();
  // 컷은 다 만들었는데 합친 영상이 없으면 (창을 닫았다 다시 연 경우 등) 바로 합친다
  if (fp && fp.locked && !fp.final_video_url && fp.cuts.length && fp.cuts.every((c) => c.clip_url && !fActive(c))) fAssemble();
}

// ---------------------------------------------------------------- 작업 화면
function fRenderAll() {
  $("f-start").hidden = !!fp;
  $("f-work").hidden = !fp;
  if (!fp) { fLoadList(); return; }
  const ref = $("f-ref-video");
  if (ref.dataset.src !== fp.ref_video_url) { ref.src = fp.ref_video_url; ref.dataset.src = fp.ref_video_url; }
  $("f-ref-meta").textContent = `원본 ${fSec(fp.length_sec)} · 시작 ${fp.source_start_sec}초 · 컷 ${fp.cuts.length}개` + (fp.source_url ? ` · ${fp.source_url}` : "");
  if (fp.match_reference) $("f-ref-meta").textContent += " · 원본 프레임의 구도 유지 · 프롬프트/시드 저장됨";
  if (document.activeElement !== $("f-setting")) $("f-setting").value = fp.setting;
  $("f-bg").innerHTML = `<option value="">(사진 없이 글로만)</option>` +
    state.backgrounds.map((b) => `<option value="${b.id}" ${b.id === fp.background_id ? "selected" : ""}>${fEsc(b.name)}</option>`).join("");
  $("f-fast").checked = !!fp.fast_preview;
  const lock = !!fp.locked;
  ["f-setting", "f-bg"].forEach((id) => { $(id).disabled = lock; });
  // viewers change the place with the 배경 panel; the raw text box / photo list stay for the admin preset
  document.querySelectorAll(".f-admin-only").forEach((el) => { el.hidden = lock; });
  fLoadBgInfo(false);
  $("f-fast").disabled = false;  // 화질은 만드는 사람이 고름 (빠르게 vs 고화질)
  $("f-usestill").checked = !fp.use_still;
  $("f-admin-publish").hidden = lock;
  $("f-published").checked = !!fp.published;
  if (document.activeElement !== $("f-description")) $("f-description").value = fp.description || "";
  $("f-delete").textContent = lock ? "이 기록 삭제" : "프리셋 삭제";
  $("f-cast-hint").textContent = lock
    ? "역할마다 원하는 인물과 목소리를 고를 수 있어요. 내 사진을 올려서 넣을 수도 있어요 (본인 또는 동의받은 사람만). 헤어·의상·배경은 관리자가 맞춘 대로 유지됩니다."
    : "의상은 원본 영상과 똑같이 영어로 적으면 가장 잘 맞습니다. \"스틸 만들기\"로 얼굴·의상을 먼저 확인하세요.";
  fRenderCast();
  fRenderStrip();
  fRenderEditor();
  fRenderFinal();
  fRenderProgress();
  fRenderPrefetch();
  fEnsurePoll();
  fEnsurePrefetchPoll();
}

// saves used to fail silently (the click just did nothing); say what went wrong and redraw the saved state
function fSaveError(e) {
  const msg = /Failed to fetch|NetworkError/.test(e.message) ? "서버에 연결되지 않아 저장하지 못했어요 — 잠시 후 다시 눌러 주세요" : e.message;
  toast("저장 실패: " + fEsc(msg), "bad", 8000);
}

async function fPatchProject(body) {
  try { fp = await patchJSON(`/api/follow/${fp.id}`, body); } catch (e) { fSaveError(e); }
  fRenderAll();
}

function fRenderCast() {
  const charOpts = (sel, rec) => `<option value="">인물 고르기</option>` +
    state.characters.map((c) => `<option value="${c.id}" ${c.id === sel ? "selected" : ""}>${fEsc(c.name)}${c.id === rec ? " ⭐" : ""}</option>`).join("");
  // voices made from a recording (no VoiceDesign description) cannot change 말투 much -- measured 2026-10-01
  const voiceLabel = (v) => `${v.name} · ${v.gender === "male" ? "남성" : v.gender === "female" ? "여성" : "미지정"}${v.design_instruct ? " · 말투 조절 ✔" : " · 녹음 목소리(말투 약함)"}`;
  // a voice taken out of the list (hidden) still shows, marked, so nobody keeps using it by accident
  const voiceOpts = (sel, rec) => `<option value="">목소리 고르기</option>` +
    (sel && !state.voices.some((v) => v.id === sel) ? `<option value="${sel}" selected disabled>⚠ 목록에서 뺀 목소리 — 새 목소리로 바꿔 주세요</option>` : "") +
    state.voices.map((v) => `<option value="${v.id}" ${v.id === sel ? "selected" : ""}>${fEsc(voiceLabel(v))}${v.id === rec ? " ⭐" : ""}</option>`).join("");
  const voicePlayer = (m) => {
    const v = voiceById(m.voice_id);
    return v && v.reference_audio_path
      ? `<audio class="f-voice-audio" controls preload="none" src="${mediaUrl(v.reference_audio_path)}"></audio>`
      : `<span class="mini-note">목소리를 고르면 들어볼 수 있어요</span>`;
  };
  if (fp.locked) {
    $("f-cast").innerHTML = fp.cast.map((m) => `
      <div class="f-cast-card" data-id="${m.id}">
        <div class="f-cast-head"><span class="f-cast-label">${fEsc(m.label)}</span><b>${fEsc(m.character_name || "")}</b></div>
        <div class="f-cast-imgs">
          ${m.character_image_url ? `<img src="${m.character_image_url}" title="얼굴" />` : `<div class="f-noimg">얼굴</div>`}
          ${m.still_url ? `<img src="${m.still_url}" title="헤어·의상 적용" />` : `<div class="f-noimg">${fp.prefetch && fp.prefetch.state === "stills" ? "인물 사진<br />그리는 중…" : "만들기 전에<br />자동으로 그려요"}</div>`}
        </div>
        <div class="mini-note">헤어·의상 (고정): ${fEsc(m.costume || "-")}</div>
        <label class="f-label">이 역할을 할 사람<select class="f-run-char">${charOpts(m.character_id, m.recommended && m.recommended.character_id)}</select></label>
        <label class="f-label">목소리<select class="f-run-voice">${voiceOpts(m.voice_id, m.recommended && m.recommended.voice_id)}</select></label>
        ${voicePlayer(m)}
        <details class="f-upload">
          <summary>+ 내 사진으로 이 역할 하기</summary>
          <input type="file" class="f-up-img" accept="image/*" />
          <input type="text" class="f-up-name" placeholder="이름" />
          <select class="f-up-gender"><option value="female">여성</option><option value="male">남성</option><option value="unspecified">미지정</option></select>
          <input type="text" class="f-up-desc" placeholder="얼굴·머리 특징 (선택, 영어 권장)" />
          <label class="f-check"><input type="checkbox" class="f-up-consent" /> 본인이거나 영상 사용에 동의한 사람의 사진입니다</label>
          <button class="btn btn-sm btn-primary f-up-go">올리고 이 역할에 넣기</button>
        </details>
      </div>`).join("");
    $("f-cast").querySelectorAll(".f-cast-card").forEach((card) => {
      const id = +card.dataset.id;
      const recast = async (body, msg) => {
        try {
          fp = await patchJSON(`/api/follow/${fp.id}/cast/${id}`, body);
          toast(msg || "바꿨습니다 — 이 역할이 나오는 컷을 다시 만들어 주세요", "info");
          fRenderAll();
        } catch (e) { toast(fEsc(e.message), "bad"); }
      };
      card.querySelector(".f-run-char").addEventListener("change", (e) => {
        const cid = +e.target.value; if (!cid) return;
        const ch = state.characters.find((c) => c.id === cid);
        recast(ch && ch.voice_id ? { character_id: cid, voice_id: ch.voice_id } : { character_id: cid });
      });
      card.querySelector(".f-run-voice").addEventListener("change", (e) => { if (+e.target.value) recast({ voice_id: +e.target.value }); });
      card.querySelector(".f-up-go").addEventListener("click", async (e) => {
        const file = card.querySelector(".f-up-img").files[0];
        const name = card.querySelector(".f-up-name").value.trim();
        if (!file || !name) { toast("사진과 이름을 넣어 주세요", "bad"); return; }
        if (!card.querySelector(".f-up-consent").checked) { toast("본인 또는 동의받은 사람의 사진만 올릴 수 있어요", "bad"); return; }
        const gender = card.querySelector(".f-up-gender").value;
        const fd = new FormData();
        fd.append("image", file);
        fd.append("name", name);
        fd.append("gender", gender);
        fd.append("description", card.querySelector(".f-up-desc").value.trim()
          || (gender === "male" ? "a young man" : gender === "female" ? "a young woman" : "a young person"));
        e.target.disabled = true;
        try {
          const r = await fetch("/api/characters", { method: "POST", body: fd });
          if (!r.ok) throw new Error(await r.text());
          const ch = await r.json();
          state.characters = await getJSON("/api/characters");
          await recast({ character_id: ch.id }, `${fEsc(name)} 님으로 바꿨습니다 — 이 역할이 나오는 컷을 다시 만들어 주세요`);
        } catch (err) { toast("올리기 실패: " + fEsc(err.message), "bad"); e.target.disabled = false; }
      });
    });
    return;
  }
  $("f-cast").innerHTML = fp.cast.map((m) => `
    <div class="f-cast-card" data-id="${m.id}">
      <div class="f-cast-head"><span class="f-cast-label">${fEsc(m.label)}</span>
        <button class="btn btn-sm btn-danger f-cast-del" title="삭제">✕</button></div>
      <div class="f-cast-imgs">
        ${m.character_image_url ? `<img src="${m.character_image_url}" title="얼굴 사진" />` : `<div class="f-noimg">얼굴</div>`}
        ${m.still_url ? `<img src="${m.still_url}?t=${Date.now() % 100000}" title="스틸 (배경·의상 적용)" />` : `<div class="f-noimg">스틸</div>`}
      </div>
      <select class="f-cast-char">${charOpts(m.character_id)}</select>
      <select class="f-cast-voice">${voiceOpts(m.voice_id)}</select>
      ${voicePlayer(m)}
      <textarea class="f-cast-costume" rows="3" placeholder="의상 (영어) 예: a sheer grey track jacket with white side stripes over a white-collared shirt, thin gold necklace">${fEsc(m.costume)}</textarea>
      <button class="btn btn-sm f-cast-still" ${m.character_id ? "" : "disabled"}>${m.still_url ? "스틸 다시 만들기" : "스틸 만들기"}</button>
    </div>`).join("") + `<button id="f-cast-add" class="f-cast-add">+ 인물 추가</button>`;

  $("f-cast").querySelectorAll(".f-cast-card").forEach((card) => {
    const id = +card.dataset.id;
    const patch = async (body) => {
      try { fp = await patchJSON(`/api/follow/${fp.id}/cast/${id}`, body); } catch (e) { fSaveError(e); }
      fRenderAll();
    };
    card.querySelector(".f-cast-char").addEventListener("change", (e) => {
      const cid = +e.target.value || null;
      const ch = state.characters.find((c) => c.id === cid);
      const body = { character_id: cid };
      const m = fp.cast.find((x) => x.id === id);
      if (ch && ch.voice_id && !m.voice_id) body.voice_id = ch.voice_id;
      if (cid) patch(body);
    });
    card.querySelector(".f-cast-voice").addEventListener("change", (e) => { if (e.target.value) patch({ voice_id: +e.target.value }); });
    card.querySelector(".f-cast-costume").addEventListener("change", (e) => patch({ costume: e.target.value.trim() }));
    card.querySelector(".f-cast-del").addEventListener("click", async () => {
      if (!confirm("이 인물을 뺄까요? 이 인물이 정해진 컷은 인물이 비워집니다.")) return;
      fp = await del(`/api/follow/${fp.id}/cast/${id}`); fRenderAll();
    });
    card.querySelector(".f-cast-still").addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "스틸 만드는 중… (~1분)";
      try {
        const { job_id } = await postJSON(`/api/follow/${fp.id}/cast/${id}/still`, {});
        for (;;) {
          await new Promise((r) => setTimeout(r, 2500));
          const j = await getJSON(`/api/follow/jobs/${job_id}`);
          if (j.status === "done") break;
          if (j.status === "error") throw new Error(j.error);
        }
        fp = await getJSON(`/api/follow/${fp.id}`);
        toast("스틸 완성");
      } catch (err) { toast("스틸 실패: " + fEsc(err.message), "bad"); }
      fRenderAll();
    });
  });
  $("f-cast-add").addEventListener("click", async () => { fp = await postJSON(`/api/follow/${fp.id}/cast`, {}); fRenderAll(); });
}

function fStatusBadge(c) {
  // 마지막 확인: 다 만든 컷을 살펴보거나 한 번 더 다듬는 중 (영상은 이미 있음)
  if (c.clip_url && fActive(c)) return `<span class="badge warn">완성 · 마무리 확인 중</span>`;
  if (c.status === "rendering") return `<span class="badge warn">만드는 중</span>`;
  if (c.status === "queued") return `<span class="badge queued">${c.queue_pos ? `대기 ${c.queue_pos}` : "대기 중"}</span>`;
  if (c.status === "error") return `<span class="badge bad">실패</span>`;
  if (c.clip_url) return `<span class="badge ok">완성</span>`;
  if (fReady(c)) return `<span class="badge ready">⚡ 미리 만들어 둠</span>`;
  return `<span class="badge idle">아직</span>`;
}

// ---------------------------------------------------------------- ⚡ 미리 만들기 (2026-10-02)
// 사람·배경·연기를 고르는 동안 서버가 인물 사진 → 목소리 → 영상을 미리 만들어 둔다. 설정이 같은 컷은 만들기를
// 누르면 바로 끝난다. 지금 기다리는 작업(컷 만들기, 배경 그리기, AI 정리)이 있으면 그게 먼저.
function fReady(c) { return !!(fp && fp.prefetch && !c.clip_url && (fp.prefetch.ready_cuts || []).includes(c.id)); }

// ⏱ 예상 시간: 고를 때마다 바로 바뀐다 — 새 배경·새 인물이면 늘고, 미리 만들기가 진행되면 줄어든다
let fEstLast = null, fEstDelta = null;
function fRenderEstimate() {
  const box = $("f-estimate");
  if (!box) return;
  const e = fp && fp.estimate;
  if (!e || !e.todo || fp.cuts.some(fActive)) { box.hidden = true; return; }
  const key = `${fp.id}`;
  if (fEstLast && fEstLast.key === key && Math.abs(e.sec - fEstLast.sec) >= 20) {
    const why = e.stills > fEstLast.stills ? ` — 인물 사진 ${e.stills - fEstLast.stills}장을 새로 그려야 해요`
      : e.clips > fEstLast.clips ? ` — 다시 만들 영상이 ${e.clips - fEstLast.clips}컷 늘었어요`
      : e.ready > fEstLast.ready ? ` — 미리 만들어 둔 컷 ${e.ready - fEstLast.ready}개 덕분이에요`
      : e.stills < fEstLast.stills ? ` — 인물 사진이 준비됐어요` : "";
    fEstDelta = { up: e.sec > fEstLast.sec, sec: Math.abs(e.sec - fEstLast.sec), why, at: Date.now() };
  }
  fEstLast = { key, ...e };
  const d = fEstDelta && Date.now() - fEstDelta.at < 15000 ? fEstDelta : null;
  const parts = [];
  if (e.stills) parts.push(`인물 사진 ${e.stills}장 새로 그림 (장당 약 1분 30초)`);
  if (e.clips) parts.push(`영상 ${e.clips}컷 (${e.fast ? "빠른 렌더 컷당 약 35초" : "고화질 컷당 약 50초"})`);
  if (e.check) parts.push(`마지막 확인·다듬기 약 ${fTime(e.check)}`);
  if (e.ready) parts.push(`⚡ ${e.ready}컷은 미리 만들어 둠`);
  box.className = `f-estimate ${d ? (d.up ? "up" : "down") : ""}`;
  box.innerHTML = `<b>⏱ 지금 "전부 만들기"를 누르면 약 ${fTime(e.sec)}</b>
    ${d ? `<span class="f-est-delta">${d.up ? "▲" : "▼"} ${fTime(d.sec)} ${d.up ? "늘었어요" : "줄었어요"}${fEsc(d.why)}</span>` : ""}
    <span class="f-est-parts">${parts.join(" · ")}</span>
    <span class="mini-note">배경·인물을 바꾸면 늘고, 고르는 동안 미리 만들기가 진행되면 줄어요</span>
    <button type="button" class="btn btn-primary btn-sm" id="f-est-go">▶ 지금 남은 컷 전부 만들기</button>
    ${(fp.speed_warn || []).map((w) => `<div class="warn-note" style="width:100%">🐢 ${fEsc(w)}</div>`).join("")}`;
  box.hidden = false;
  // 버튼이 긴 편집 화면 맨 아래에만 있어서 찾기 어려웠다 (2026-10-07): 예상 시간 바로 옆에서 시작
  $("f-est-go").addEventListener("click", () => {
    const b = $("f-render-all");
    if (b && !b.disabled) { b.click(); setTimeout(() => { if ($("f-progress")) $("f-progress").scrollIntoView({ block: "center" }); }, 800); }
  });
}

let fPfPoll = null;
function fRenderPrefetch() {
  fRenderEstimate();
  const box = $("f-prefetch");
  if (!box) return;
  const pf = fp && fp.prefetch;
  const btn = $("f-render-all");
  const ready = fp ? fp.cuts.filter(fReady).length : 0;
  if (btn && !btn.disabled) btn.textContent = ready ? `남은 컷 전부 만들기 (⚡ ${ready}컷은 바로)` : "남은 컷 전부 만들기";
  const left = fp ? fp.cuts.filter((c) => !c.clip_url).length : 0;
  if (!pf || fp.cuts.some(fActive) || (!left && pf.enabled)) { box.hidden = true; return; }  // 다 만든 영상엔 안 보여 줌
  const step = (name, v, cur) => {
    if (!v) return "";
    const [i, n] = v; const done = i >= n;
    return `<span class="f-pf-step ${done ? "done" : ""}">${done ? "✔" : "…"} ${name} ${Math.min(i, n)}/${n}${cur && !done ? ` (${cur}번 컷)` : ""}</span>`;
  };
  let head;
  if (!pf.enabled) head = "⚡ 미리 만들기 꺼짐";
  else if (pf.state === "waiting") head = "⚡ 곧 미리 만들기 시작 (고르는 중이면 잠깐 기다려요)";
  else if (pf.state === "paused") head = "⏸ 지금 하시는 작업이 먼저예요 — 끝나면 이어서 미리 만들어요";
  else if (pf.state === "done") head = left ? `⚡ 미리 준비 끝 — ${ready}/${left}컷은 만들기를 누르면 바로 나와요` : "⚡ 미리 준비 끝";
  else if (pf.state === "error") head = "미리 만들기를 하다 멈췄어요 (만들기를 누르면 그때 만들어요)";
  else head = `<span class="spinner"></span> ⚡ 고르는 동안 미리 만드는 중`;
  box.className = `f-prefetch ${pf.state === "paused" ? "paused" : ""}`;
  box.innerHTML = `<b>${head}</b>
    <span class="f-pf-steps">${step("인물 사진", pf.stills)}${step("목소리", pf.voices)}${step("영상", pf.clips, pf.current)}</span>
    <span class="f-pf-note">바꾸면 그 부분만 다시 만들어요 · 컴퓨터가 쉬는 동안만 써요</span>
    <button type="button" class="btn btn-sm" id="f-pf-toggle">${pf.enabled ? "미리 만들기 끄기" : "미리 만들기 켜기"}</button>`;
  box.hidden = false;
  $("f-pf-toggle").addEventListener("click", async () => {
    try { const r = await postJSON(`/api/follow/${fp.id}/prefetch`, { on: !pf.enabled }); fp.prefetch = r.prefetch; } catch (e) { toast(fEsc(e.message), "bad"); }
    fRenderPrefetch(); fEnsurePrefetchPoll();
  });
}

// ✨로 정리하는 칸에 쓰기 시작하면 잠깐 미리 만들기가 새 영상을 시작하지 않게 한다: 그래픽카드가 비어 있어야
// AI 정리가 몇 초에 끝난다 (영상 만드는 중이면 CPU로 50초쯤). 쓰는 동안 20초마다 다시 알림.
let fHoldSent = 0;
function fHoldPrefetch() {
  if (!fp || !fp.prefetch || !fp.prefetch.enabled || Date.now() - fHoldSent < 20000) return;
  fHoldSent = Date.now();
  postJSON(`/api/follow/${fp.id}/prefetch/hold`, {}).catch(() => {});
}
document.addEventListener("focusin", (e) => { if (["f-bg-text", "f-action", "f-prompt-ko-in", "f-prompt"].includes(e.target.id)) fHoldPrefetch(); });
document.addEventListener("input", (e) => { if (["f-bg-text", "f-action", "f-prompt-ko-in", "f-prompt"].includes(e.target.id)) fHoldPrefetch(); });

// 미리 만드는 동안 5초마다 상태만 새로 받아 온다 (입력 중인 칸은 건드리지 않음)
function fEnsurePrefetchPoll() {
  const going = fp && fp.prefetch && fp.prefetch.enabled && !["done", "error", "off", undefined].includes(fp.prefetch.state);
  if (going && !fPfPoll) {
    fPfPoll = setInterval(async () => {
      if (!fp || $("follow").hidden || fPoll) return;
      let nf; try { nf = await getJSON(`/api/follow/${fp.id}`); } catch { return; }
      if (!fp || nf.id !== fp.id) return;
      // 다른 곳에서(다른 탭, 서버 재시작 후 이어 만들기) 만들기가 시작됐으면 이 화면도 진행 막대로 따라간다
      if (nf.cuts.some(fActive) && !fp.cuts.some(fActive)) { fp = nf; fRenderAll(); fEnsurePoll(); return; }
      const was = (fp.prefetch && fp.prefetch.ready_cuts || []).length;
      const stillsKey = (x) => x.cast.map((m) => m.still_url || "").join("|") + (x.prefetch && x.prefetch.state === "stills");
      const castChanged = stillsKey(nf) !== stillsKey(fp);
      fp.prefetch = nf.prefetch; fp.estimate = nf.estimate; fp.speed_warn = nf.speed_warn;
      // 인물 사진이 다 그려지면 등장인물 카드에 바로 보여 준다 (고르는 중인 칸이 있으면 건드리지 않음)
      if (castChanged && !$("f-cast").contains(document.activeElement)) { fp.cast = nf.cast; fRenderCast(); }
      fp.cuts.forEach((c) => { const n = nf.cuts.find((x) => x.id === c.id); if (n) { c.spoken_line = n.spoken_line; } });
      fRenderStrip(); fRenderPrefetch();
      if ($("f-cut-badge") && fp.cuts[fCutIdx]) $("f-cut-badge").innerHTML = fStatusBadge(fp.cuts[fCutIdx]);
      if ((nf.prefetch && nf.prefetch.ready_cuts || []).length > was && nf.prefetch.state === "done") toast("⚡ 미리 준비 끝 — 만들기를 누르면 바로 나와요", "info", 6000);
      if (!nf.prefetch || ["done", "error", "off"].includes(nf.prefetch.state) || !nf.prefetch.enabled) { clearInterval(fPfPoll); fPfPoll = null; }
    }, 5000);
  }
}

function fRenderStrip() {
  const castLabel = (id) => (fp.cast.find((m) => m.id === id) || {}).label || "?";
  $("f-strip").innerHTML = fp.cuts.map((c, i) => `
    <button class="f-thumb ${i === fCutIdx ? "active" : ""} s-${c.clip_url ? "done" : c.status}" data-i="${i}">
      <img src="${c.ref_thumb_url}" />
      <span class="f-thumb-num">${i + 1}</span>
      <span class="f-thumb-cast">${c.cast_id ? castLabel(c.cast_id) : "·"}</span>
      <span class="f-thumb-time">${c.start_sec.toFixed(1)}–${c.end_sec.toFixed(1)}s</span>
      <span class="f-thumb-dot"></span>
      ${fReady(c) ? `<span class="f-thumb-ready" title="미리 만들어 둠 — 누르면 바로 나와요">⚡</span>` : ""}
    </button>`).join("");
  $("f-strip").querySelectorAll(".f-thumb").forEach((b) => b.addEventListener("click", () => { fCutIdx = +b.dataset.i; fRenderStrip(); fRenderEditor(); }));
}

function fRenderEditor() {
  const c = fp.cuts[fCutIdx];
  if (!c) { $("f-editor").innerHTML = ""; return; }
  const member = fp.cast.find((m) => m.id === c.cast_id);
  // ⭐ marks the setting this video came tuned with (the admin's choice for this cut)
  const rec = (kind, key) => (c.recommended && (c.recommended[kind] || "") === key ? " ⭐" : "");
  const lineLen = (c.line || "").length;
  const origLine = c.recommended && "line" in c.recommended ? (c.recommended.line || "") : null;
  const partner = fPartner(fCutIdx);
  $("f-editor").innerHTML = `
    <div class="f-ed-head">
      <b>컷 ${fCutIdx + 1} / ${fp.cuts.length}</b>
      <span class="mini-note">원본 ${c.start_sec.toFixed(2)}–${c.end_sec.toFixed(2)}초 (${fSec(c.duration)})</span>
      <span id="f-cut-badge">${fStatusBadge(c)}</span>
      <span class="f-ed-nav">
        <button class="btn btn-sm" id="f-prev" ${fCutIdx === 0 ? "disabled" : ""}>◀ 이전 컷</button>
        <button class="btn btn-sm" id="f-next" ${fCutIdx >= fp.cuts.length - 1 ? "disabled" : ""}>다음 컷 ▶</button>
      </span>
    </div>
    <div class="f-compare">
      <figure><figcaption>원본 컷</figcaption>
        <video src="${c.ref_clip_url}" controls loop playsinline></video></figure>
      <figure><figcaption>AI 컷 ${c.render_sec ? `<span class="mini-note">(${fTime(c.render_sec)} 걸림${c.timing ? ` · 인물사진 ${c.timing.still}s + 목소리 ${c.timing.voice}s + 영상 ${c.timing.video}s + 그 외 ${c.timing.other}s` : ""})</span>` : ""}</figcaption>
        <div id="f-ai-slot">${fAiSlot(c)}</div></figure>
    </div>

    ${fTalkHtml()}
    <div class="f-lineblock">
      <div class="f-field"><span class="f-label">대사 <span class="mini-note ${lineLen > c.line_limit ? "over" : ""}">${lineLen}/${c.line_limit}자 · 원본 컷 길이에 맞는 글자 수</span></span>
        <div class="f-line-note">✎ 대사는 자르지 않고 적은 그대로 전부 말해요. 원본보다 길게 바꾸면 이 컷이 길어져서 <b>전체 영상이 고른 길이보다 길어질 수 있고</b>, <b>만드는 시간도 더 걸릴 수 있어요.</b></div>
        ${origLine != null ? `<div class="f-orig-line">⭐ 추천 대사: <q>${fEsc(origLine) || "(말 없음)"}</q>
          ${origLine !== (c.line || "") ? `<button class="btn btn-sm" id="f-line-orig">추천 대사로 되돌리기</button>` : ""}</div>` : ""}
        <input type="text" id="f-line" value="${fEsc(c.line)}" placeholder="바꾸고 싶은 대사를 적으세요 (비우면 말 없는 리액션 컷)" />
        ${c.spoken_line && c.spoken_line !== c.line ? `<div class="f-spoken">실제로 말할 때: <q>${fEsc(c.spoken_line)}</q> (말투에 맞춰 바뀜)</div>` : ""}
        ${c.line_en ? `<span class="f-trans">EN · ${fEsc(c.line_en)}</span>` : ""}
      </div>
      <div class="f-field"><span class="f-label">누구를 보고 말하나요? (대화 상대가 있는 쪽)</span>
        <div class="chip-row">
          <button class="chip ${c.look === "left" ? "active" : ""}" data-look="left">← 화면 왼쪽을 봄</button>
          <button class="chip ${c.look === "right" ? "active" : ""}" data-look="right">화면 오른쪽을 봄 →</button>
          ${partner ? `<span class="mini-note" style="align-self:center">대화 상대: ${fEsc(partner)}</span>` : ""}
          ${c.recommended && c.recommended.look ? `<span class="mini-note" style="align-self:center">⭐ 추천: ${c.recommended.look === "left" ? "왼쪽" : "오른쪽"}</span>` : ""}
        </div>
      </div>
    </div>

    <div class="f-fields">
      <div class="f-field"><span class="f-label">이 컷에 나오는 사람</span>
        ${fp.locked
          ? `<div>${member ? fEsc(member.label + (member.character_name ? " · " + member.character_name : "")) : "-"}</div>`
          : `<div class="chip-row">${fp.cast.map((m) => `<button class="chip ${m.id === c.cast_id ? "active" : ""}" data-cast="${m.id}">${fEsc(m.label)}${m.character_name ? " · " + fEsc(m.character_name) : ""}</button>`).join("")}</div>`}
      </div>
      <div class="f-field"><span class="f-label">행동 · 표정 · 시선 <span class="f-ai-hint">✨ 조금만 써도 돼요 — AI(gemma)가 영상용 문장으로 정리해요</span></span>
        <div class="f-action-row"><input type="text" id="f-action" value="${fEsc(c.action)}" placeholder="예: 놀라서 친구 쳐다봄 (한국어로 짧게 써도 OK)" />
          <button type="button" class="btn btn-sm btn-primary" id="f-action-refine">✨ AI로 정리</button>
          <button type="button" class="btn btn-sm" id="f-action-suggest" title="대사·말투·앞뒤 대사를 보고 AI가 이 컷의 몸짓·표정·시선을 정해서 넣어요 (프롬프트에 자동으로 들어가요)">🎬 AI가 행동 정하기</button></div>
        ${fMotionChips(c)}
        <div class="f-ex-row"><span class="mini-note">✨ 예시 (AI가 다듬어요):</span>${["놀라서 친구 쳐다봄", "팔짱 끼고 한숨", "손으로 가리키며 웃음", "고개 갸웃하며 의심", "박수 치며 신남", "어깨 으쓱", "위를 올려다보며 감탄"].map((x) => `<button type="button" class="chip f-ex" data-act-ex="${x}">${x}</button>`).join("")}</div>
        <div id="f-action-note" class="mini-note"></div></div>
      <div class="f-field"><span class="f-label">말투 (목소리 + 표정) <span class="mini-note">⭐ = 추천 · 누르면 그 말투 프롬프트가 Qwen3로 들어가요</span></span>
        <div class="chip-row">${(fp.tone_options || []).map((t) => `<button class="chip ${(c.tone || "") === t.key ? "active" : ""}" data-tone="${t.key}" title="${fEsc(t.instruct || "")}">${fEsc(t.label)}${rec("tone", t.key)}</button>`).join("")}</div>
        ${(() => { const t = (fp.tone_options || []).find((x) => x.key === (c.tone || "")); return t && t.instruct ? `<div class="mini-note">Qwen3에 보내는 말투 프롬프트: ${fEsc(t.instruct)}</div>` : ""; })()}
        ${(() => { const v = member && voiceById(member.voice_id); return v && !v.design_instruct && c.tone ? `<div class="warn-note">이 역할의 목소리(${fEsc(v.name)})는 녹음을 복제한 목소리라 말투 변화가 작아요 (문장부호로만 조금 바뀜). 말투를 확실히 바꾸려면 위 등장인물 카드에서 "말투 조절 ✔" 목소리를 고르세요.</div>` : ""; })()}
      </div>
      <div class="f-field"><span class="f-label">화면 속 인원 <span class="mini-note">기본은 말하는 사람 1명 (얼굴이 커서 립싱크가 잘 맞아요)</span></span>
        <div class="chip-row">
          ${[1, 2].map((n) => `<button class="chip ${(c.people || 1) === n ? "active" : ""}" data-people="${n}">${n}명${c.recommended && c.recommended.people === n ? " ⭐" : ""}</button>`).join("")}
        </div>
        ${(c.people || 1) >= 2 ? `<div class="chip-row" style="margin-top:6px">
          <span class="mini-note" style="align-self:center">옆에 같이 있을 사람:</span>
          ${fp.cast.filter((m) => m.id !== c.cast_id).map((m) => `<button class="chip ${m.id === c.cast_id2 ? "active" : ""}" data-cast2="${m.id}">${fEsc(m.label)}${m.character_name ? " · " + fEsc(m.character_name) : ""}</button>`).join("")}
        </div>` : ""}
      </div>
      <div class="f-field"><span class="f-label">샷 크기 <span class="mini-note">원본 구도에 맞추기</span></span>
        <div class="chip-row">${(fp.framing_options || []).map((t) => `<button class="chip ${(c.framing || "medium") === t.key ? "active" : ""}" data-framing="${t.key}">${fEsc(t.label)}${rec("framing", t.key)}</button>`).join("")}</div>
      </div>
      <div class="f-field"><span class="f-label">카메라 움직임 <span class="mini-note">원본에서 자동 감지됨</span></span>
        <div class="chip-row">${(fp.camera_options || []).map((t) => `<button class="chip ${(c.camera || "locked") === t.key ? "active" : ""}" data-camera="${t.key}">${fEsc(t.label)}${rec("camera", t.key)}</button>`).join("")}</div>
      </div>
      <div class="f-field"><span class="f-label">인물 움직임</span>
        <div class="chip-row">${(fp.motion_options || []).map((t) => `<button class="chip ${(c.motion || "") === t.key ? "active" : ""}" data-motion="${t.key}">${fEsc(t.label)}${rec("motion", t.key)}</button>`).join("")}</div>
      </div>
    </div>

    <div class="f-lesson">
      <div class="f-lesson-head">프롬프트 <button class="btn btn-sm" id="f-lesson-here">📖 해설 보기</button></div>
      ${c.prompt_ko ? `<div class="f-trans">한국어 뜻 · ${fEsc(c.prompt_ko)}</div>` : ""}
      <details class="prompt-box" open>
        <summary>✍ 프롬프트 자세히 쓰기 · 실제로 보내는 프롬프트 ${c.prompt_edited ? '<span class="badge warn">직접 수정함</span>' : '<span class="badge idle">자동</span>'}</summary>
        <div class="f-detail">
          <div class="f-detail-tip">💡 <b>자세히 쓸수록 영상이 좋아져요.</b> AI는 적힌 것만 따라 해요 — 표정·손·시선·카메라를 칸마다 하나씩 적으면 그대로 움직이고, 비워 두면 가만히 서서 말만 해요. 1~2초 컷이라 동작은 1~2개가 가장 잘 나와요.</div>
          <div class="f-detail-grid">
            ${[["face", "표정", "예: 눈이 커지며 놀랐다가 입꼬리가 올라가며 웃음"],
               ["hands", "손·팔 몸짓", "예: 두 손을 번쩍 들었다 내림 / 손가락으로 선반을 가리킴"],
               ["gaze", "시선", "예: 선반을 올려다봤다가 오른쪽 친구를 봄"],
               ["body", "몸 움직임", "예: 몸을 앞으로 살짝 기울임 / 어깨를 들썩임"],
               ["camera", "카메라", "예: 천천히 얼굴 쪽으로 다가감 / 고정"],
               ["mood", "분위기·빛", "예: 노을빛, 바람에 머리카락이 날림"],
               ["etc", "기타", "예: 뒤쪽에 사람들이 작게 지나감"]].map(([k, l, ph]) =>
              `<label class="f-detail-f"><span>${l}</span><input type="text" data-detail="${k}" placeholder="${ph}" /></label>`).join("")}
          </div>
          <button type="button" class="btn btn-primary" id="f-detail-make">✨ 이 내용으로 프롬프트 자세히 만들기</button>
        </div>
        <label class="f-label" style="margin:0 12px">또는 바라는 점만 조금 적으세요 <span class="f-ai-hint">✨ AI(gemma)가 아래 프롬프트를 규칙에 맞게 고쳐 줘요</span>
          <textarea id="f-prompt-ko-in" rows="2" placeholder="예: 더 신나게, 손짓 크게 / 카메라 천천히 다가가기 (짧게 써도 OK)"></textarea></label>
        <div class="f-ex-row" style="margin:0 12px"><span class="mini-note">눌러 보기:</span>${["더 신나게, 손짓 크게", "카메라 천천히 다가가기", "슬프게 고개 숙였다가 친구 봄", "웃으면서 어깨 들썩", "놀라서 뒤로 살짝 물러남", "차분하게, 움직임 적게"].map((x) => `<button type="button" class="chip f-ex" data-pr-ex="${x}">${x}</button>`).join("")}</div>
        <div class="scene-foot" style="padding-top:0">
          <button class="btn btn-primary" id="f-prompt-refine" title="위 칸에 바라는 점을 대충 쓰면, 지금 프롬프트를 규칙에 맞게 고쳐서 아래에 넣어요">✨ AI로 프롬프트 정리하기</button>
          <button class="btn btn-sm" id="f-prompt-ko2en">↓ 번역만 해서 아래에 넣기</button>
        </div>
        <div id="f-prompt-refined" class="f-refined" hidden></div>
        <textarea id="f-prompt">${fEsc(c.prompt)}</textarea>
        <div class="scene-foot">
          <button class="btn btn-sm" id="f-prompt-en">한국어로 쓴 부분 → 영어로 바꾸기</button>
          <button class="btn btn-sm" id="f-prompt-ko">이 프롬프트 한국어로 보기</button>
          <button class="btn btn-sm" id="f-prompt-save">수정한 프롬프트 저장</button>
          ${c.prompt_edited ? `<button class="btn btn-sm" id="f-prompt-reset">자동 프롬프트로 되돌리기</button>` : ""}
        </div>
        <details class="f-hist" id="f-hist"><summary>📜 이 컷에 예전에 쓴 프롬프트 (만든 영상과 함께 저장돼요)</summary>
          <div id="f-hist-list" class="mini-note">불러오는 중…</div></details>
      </details>
    </div>

    ${c.error_message ? `<div class="warn-note">실패 이유: ${fEsc(c.error_message)}</div>` : ""}
    ${member ? "" : `<div class="warn-note">이 컷에 나오는 사람을 먼저 고르세요.</div>`}
    <div class="btn-row">
      <button class="btn btn-primary" id="f-render" ${member && !fActive(c) ? "" : "disabled"}>${c.clip_url ? "이 컷 다시 만들기" : "이 컷 만들기"} (1~2초 영상)</button>
      <button class="btn" id="f-render-next" ${member && !fActive(c) ? "" : "disabled"}>${fCutIdx < fp.cuts.length - 1 ? "만들고 다음 컷 →" : "만들고 컷 합치기 + 원본 비교 영상 →"}</button>
    </div>`;

  const cutUrl = `/api/follow/${fp.id}/cuts/${c.id}`;
  const patch = async (body) => {
    try { fp = await patchJSON(cutUrl, body); } catch (e) { fSaveError(e); }
    fRenderStrip(); fRenderEditor(); fRenderPrefetch(); fEnsurePrefetchPoll();
  };
  $("f-prev").addEventListener("click", () => { fCutIdx--; fRenderStrip(); fRenderEditor(); });
  $("f-next").addEventListener("click", () => { fCutIdx++; fRenderStrip(); fRenderEditor(); });
  // 인물 배정만 관리자용, 나머지 연출 선택은 누구나
  $("f-editor").querySelectorAll("[data-cast]").forEach((b) => b.addEventListener("click", () => {
    const castId = +b.dataset.cast;
    const idx = fp.cast.findIndex((m) => m.id === castId);
    // 기본 시선: 첫 번째 인물은 오른쪽, 나머지는 왼쪽 상대를 봄 (컷을 이으면 서로 마주 보는 느낌)
    patch(c.cast_id ? { cast_id: castId } : { cast_id: castId, look: idx === 0 ? "right" : "left" });
  }));
  $("f-editor").querySelectorAll("[data-look]").forEach((b) => b.addEventListener("click", () => patch({ look: b.dataset.look })));
  $("f-line").addEventListener("change", (e) => fTrack(patch({ line: e.target.value.trim() })));
  if ($("f-line-orig")) $("f-line-orig").addEventListener("click", () => patch({ line: origLine }));
  $("f-editor").querySelectorAll("[data-talk]").forEach((b) => b.addEventListener("click", () => {
    fCutIdx = +b.dataset.talk; fRenderStrip(); fRenderEditor();
  }));
  $("f-editor").querySelectorAll("[data-tone]").forEach((b) => b.addEventListener("click", () => patch({ tone: b.dataset.tone })));
  $("f-editor").querySelectorAll("[data-motion]").forEach((b) => b.addEventListener("click", () => patch({ motion: b.dataset.motion })));
  $("f-editor").querySelectorAll("[data-people]").forEach((b) => b.addEventListener("click", () => patch({ people: +b.dataset.people })));
  $("f-editor").querySelectorAll("[data-cast2]").forEach((b) => b.addEventListener("click", () => patch({ cast_id2: +b.dataset.cast2 })));
  $("f-editor").querySelectorAll("[data-framing]").forEach((b) => b.addEventListener("click", () => patch({ framing: b.dataset.framing })));
  $("f-editor").querySelectorAll("[data-camera]").forEach((b) => b.addEventListener("click", () => patch({ camera: b.dataset.camera })));
  $("f-lesson-here").addEventListener("click", () => fShowLesson(c));
  // ✨ 행동: 대충 쓴 메모(한국어 그대로도 됨)를 gemma가 영상용 영어 한 줄로 정리해서 저장 (2026-10-02)
  const refineAction = async (text) => {
    const btn = $("f-action-refine"); const inp = $("f-action");
    btn.disabled = true; inp.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { $("f-action-note").innerHTML = `<span class="spinner"></span> AI가 정리하는 중… ${fTime((Date.now() - t0) / 1000)}`; }, 500);
    try {
      const r = await postJSON(`/api/follow/${fp.id}/refine`, { kind: "action", text, cut_id: c.id });
      clearInterval(tick);
      toast(`✨ 정리했어요 (${fTime((Date.now() - t0) / 1000)}): ${fEsc(r.en)}<br><span class="mini-note">뜻: ${fEsc(r.ko)}</span>` +
        (r.warn ? `<br>⚠ ${fEsc(r.warn)}` : ""), r.warn ? "bad" : "info", r.warn ? 12000 : 8000);
      await patch({ action: r.en });
      if ($("f-action-note")) $("f-action-note").innerHTML = `뜻: ${fEsc(r.ko)}` + (r.warn ? ` <span class="warn-note">⚠ ${fEsc(r.warn)}</span>` : "");
    } catch (e) {
      clearInterval(tick);
      if (/[가-힣]/.test(text)) { toast(`${fEsc(e.message)} — 이번엔 번역만 해서 넣을게요`, "bad", 6000);
        try { await patch({ action: (await postJSON("/api/translate", { text, target: "en" })).text.trim() }); } catch (e2) { fSaveError(e2); } }
      else toast(fEsc(e.message), "bad");
      btn.disabled = false; inp.disabled = false; $("f-action-note").textContent = "";
    }
  };
  // 한국어로 쓴 행동은 "뜻 그대로 번역"만 한다 (2026-10-07 사용자: 쓴 내용이 AI 정리로 딴 문장이 됐음).
  // 다시 쓰기(✨ AI로 정리)는 그 버튼을 눌렀을 때만.
  const translateAction = async (text) => {
    const inp = $("f-action");
    if (inp) inp.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { if ($("f-action-note")) $("f-action-note").innerHTML = `<span class="spinner"></span> 쓴 그대로 영어로 옮기는 중… ${fTime((Date.now() - t0) / 1000)}`; }, 500);
    try {
      const r = await postJSON("/api/translate", { text, target: "en" });
      clearInterval(tick);
      await patch({ action: r.text.trim() });
      if ($("f-action-note")) $("f-action-note").innerHTML = `쓴 내용: ${fEsc(text)} → <b>${fEsc(r.text.trim())}</b> <span class="mini-note">(뜻 그대로 번역, ${fTime((Date.now() - t0) / 1000)})</span>`;
    } catch (e) { clearInterval(tick); fSaveError(e); if (inp) inp.disabled = false; }
  };
  $("f-action").addEventListener("change", (e) => {
    const text = e.target.value.trim();
    fTrack(/[가-힣]/.test(text) ? translateAction(text) : patch({ action: text }));
  });
  $("f-editor").querySelectorAll("[data-mlib]").forEach((b) => b.addEventListener("click", async () => {
    const m = fMotions.find((x) => x.key === b.dataset.mlib);
    if (!m) return;
    $("f-action").value = m.action_en;
    await patch({ action: m.action_en });
    if ($("f-action-note")) $("f-action-note").innerHTML = `${m.verified ? "✅" : "🧪"} '${fEsc(m.label)}' 넣었어요 — 매번 같은 문장이라 결과가 일정해요${m.verified ? " (테스트에서 움직임이 잘 나온 문장)" : ""}`;
  }));
  $("f-hist").addEventListener("toggle", () => { if ($("f-hist").open) fLoadPromptHistory(c); });
  $("f-editor").querySelectorAll("[data-act-ex]").forEach((b) => b.addEventListener("click", () => {
    $("f-action").value = b.dataset.actEx; refineAction(b.dataset.actEx);
  }));
  $("f-detail-make").addEventListener("click", () => {
    const parts = [...$("f-editor").querySelectorAll("[data-detail]")]
      .map((i) => [i.closest("label").querySelector("span").textContent, i.value.trim()]).filter(([, v]) => v);
    if (!parts.length) { toast("칸 하나라도 적어 주세요 — 예시처럼 짧게 써도 돼요", "bad", 4000); return; }
    $("f-prompt-ko-in").value = parts.map(([l, v]) => `${l}: ${v}`).join(" / ");
    $("f-prompt-refine").click();
  });
  $("f-editor").querySelectorAll("[data-pr-ex]").forEach((b) => b.addEventListener("click", () => {
    $("f-prompt-ko-in").value = b.dataset.prEx; $("f-prompt-refine").click();
  }));
  $("f-action-suggest").addEventListener("click", async (e) => {
    const btn = e.target; btn.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { $("f-action-note").innerHTML = `<span class="spinner"></span> AI가 대사·말투를 보고 행동을 정하는 중… ${fTime((Date.now() - t0) / 1000)}`; }, 500);
    try {
      const r = await postJSON(`/api/follow/${fp.id}/refine`, { kind: "suggest", text: "", cut_id: c.id });
      clearInterval(tick);
      await patch({ action: r.en });
      if ($("f-action-note")) $("f-action-note").innerHTML = `🎬 AI가 정했어요: ${fEsc(r.ko)} <span class="mini-note">(프롬프트 action 줄에 들어갔어요)</span>`;
    } catch (err) { clearInterval(tick); toast(fEsc(err.message), "bad"); btn.disabled = false; $("f-action-note").textContent = ""; }
  });
  $("f-action-refine").addEventListener("click", () => {
    const text = $("f-action").value.trim();
    if (!text) { toast("어떤 행동인지 먼저 대충 적어 주세요 (예: 놀라서 친구 쳐다봄)", "bad", 4000); return; }
    refineAction(text);
  });
  $("f-prompt-save").addEventListener("click", () => patch({ ltx_prompt: $("f-prompt").value }));
  $("f-prompt-en").addEventListener("click", async (e) => {
    const box = $("f-prompt");
    e.target.disabled = true; e.target.textContent = "영어로 바꾸는 중…";
    try {
      const r = await postJSON("/api/translate", { text: box.value, target: "en" });
      box.value = r.text;
      toast("영어로 바꿨습니다 — 확인 후 '수정한 프롬프트 저장'을 누르세요", "info");
    } catch (err) { toast(fEsc(err.message), "bad"); }
    e.target.disabled = false; e.target.textContent = "한국어로 쓴 부분 → 영어로 바꾸기";
  });
  $("f-prompt-ko").addEventListener("click", () => trOpen($("f-prompt").value, "ko", true));
  $("f-prompt-refine").addEventListener("click", async (e) => {
    const note = $("f-prompt-ko-in").value.trim();
    if (!note) { toast("바라는 점을 위 칸에 대충 적어 주세요 (예: 카메라 천천히 다가가고 더 신나게)", "bad", 5000); return; }
    const btn = e.target; btn.disabled = true;
    const t0 = Date.now();
    const tick = setInterval(() => { btn.textContent = `AI가 정리하는 중… ${fTime((Date.now() - t0) / 1000)}`; }, 500);
    try {
      const r = await postJSON(`/api/follow/${fp.id}/refine`, { kind: "prompt", text: note, cut_id: c.id });
      $("f-prompt").value = r.prompt;
      const box = $("f-prompt-refined");
      box.innerHTML = `<b>✨ 정리했어요 (${fTime((Date.now() - t0) / 1000)})</b> — ${fEsc(r.ko || "")}` +
        (r.warn ? `<div class="warn-note">⚠ ${fEsc(r.warn)}</div>` : "") +
        `<br><span class="mini-note">아래 프롬프트를 확인하고 "수정한 프롬프트 저장"을 누르면 이 컷에 들어가요</span>`;
      box.hidden = false;
    } catch (err) { toast(fEsc(err.message), "bad", 8000); }
    clearInterval(tick); btn.disabled = false; btn.textContent = "✨ AI로 프롬프트 정리하기";
  });
  $("f-prompt-ko2en").addEventListener("click", async (e) => {
    const ko = $("f-prompt-ko-in").value.trim();
    if (!ko) { toast("한국어로 먼저 적어 주세요", "bad", 3000); return; }
    e.target.disabled = true; e.target.textContent = "바꾸는 중…";
    try {
      const r = await postJSON("/api/translate", { text: ko, target: "en" });
      $("f-prompt").value = r.text;
      toast("영어로 바꿨어요 — '수정한 프롬프트 저장'을 누르면 적용됩니다", "info", 7000);
    } catch (err) { toast(fEsc(err.message), "bad"); }
    e.target.disabled = false; e.target.textContent = "↓ 영어로 바꿔서 아래에 넣기";
  });
  if ($("f-prompt-reset")) $("f-prompt-reset").addEventListener("click", () => patch({ ltx_prompt: "" }));
  if (c.status === "rendering") fStartTick();
  const render = async (advance) => {
    // lock both buttons at once: a second click used to re-send this cut and skip the next one
    $("f-render").disabled = true; $("f-render-next").disabled = true;
    const idx = fp.cuts.findIndex((x) => x.id === c.id);
    try {
      // 2026-10-07 (#209 cut 1, 9 min): the render went out with the OLD action while the typed one was still being
      // converted; when it landed, the half-made clip was thrown away and made again. Finish every save first, and
      // a prompt typed into the box but not saved is saved now (it used to be ignored).
      const typed = $("f-prompt") ? $("f-prompt").value.trim() : "";
      if (typed && typed !== (c.prompt || "").trim()) fTrack(patch({ ltx_prompt: typed }));
      if (fPending.length) {
        toast("쓰신 내용을 먼저 저장하고 바로 만들어요", "info", 3000);
        await Promise.allSettled([...fPending]);
      }
      const health = await getJSON("/api/follow/progress").catch(() => ({ comfyui: true }));
      if (!health.comfyui) throw new Error("영상 프로그램(ComfyUI)이 꺼져 있습니다. ComfyUI를 켜고 다시 눌러 주세요.");
      await postJSON(`${cutUrl}/render`, {});
      toast(`컷 ${idx + 1} 만들기 시작`, "info", 3000);
      fp = await getJSON(`/api/follow/${fp.id}`);
      const last = advance && idx === fp.cuts.length - 1;
      if (advance && !last) fCutIdx = idx + 1;
      fRenderStrip(); fRenderEditor(); fEnsurePoll();
      if (last) {
        // the last cut: no "next" -- when it is done, join the cuts and show the 원본 | AI video
        fAutoAssemble = fp.id;
        fRenderFinal();
        $("f-assemble").scrollIntoView({ behavior: "smooth", block: "center" });
        toast("이 컷이 끝나면 자동으로 컷을 합쳐서 원본 비교 영상을 만들어요", "info", 8000);
      }
    } catch (e) { toast(fEsc(e.message), "bad"); fRenderEditor(); }
  };
  $("f-render").addEventListener("click", () => render(false));
  $("f-render-next").addEventListener("click", () => render(true));
}

function fWho(castId) {
  const m = fp.cast.find((x) => x.id === castId);
  return m ? m.label + (m.character_name ? " · " + m.character_name : "") : "?";
}

// the person this cut's speaker is talking to: the one beside them, else the nearest cut spoken by someone else
function fPartner(i) {
  const c = fp.cuts[i];
  if (!c || !c.cast_id) return "";
  if ((c.people || 1) >= 2 && c.cast_id2) return fWho(c.cast_id2);
  for (let d = 1; d < fp.cuts.length; d++) {
    for (const j of [i - d, i + d]) {
      const o = fp.cuts[j];
      if (o && o.cast_id && o.cast_id !== c.cast_id) return fWho(o.cast_id) + " (앞뒤 컷 기준)";
    }
  }
  return "";
}

// 지금까지 나눈 대화: 앞 컷들의 누가 · 어디를 보며 · 무슨 말 (다음 컷 하나는 흐리게 미리보기)
function fTalkHtml() {
  if (!fp || !fp.cuts.length) return "";
  const toneLabel = (k) => ((fp.tone_options || []).find((t) => t.key === (k || "")) || {}).label || "";
  const upto = Math.min(fCutIdx + 1, fp.cuts.length - 1);
  const rows = fp.cuts.slice(0, upto + 1).map((c, i) => {
    const orig = c.recommended && "line" in c.recommended ? (c.recommended.line || "") : null;
    const said = c.line ? `"${fEsc(c.line)}"` : `<span class="mini-note">(말 없이 반응)</span>`;
    const changed = orig != null && orig !== (c.line || "") ? `<span class="f-talk-orig">추천: "${fEsc(orig)}"</span>` : "";
    const look = c.look === "left" ? "← 왼쪽을 보며" : "오른쪽을 보며 →";
    const tone = toneLabel(c.tone);
    return `<button class="f-talk-row ${i === fCutIdx ? "now" : i > fCutIdx ? "later" : ""}" data-talk="${i}">
      <span class="f-talk-num">${i + 1}번${i === fCutIdx ? " ◀" : ""}</span>
      <span class="f-talk-who">${fEsc(c.cast_id ? fWho(c.cast_id) : "-")}</span>
      <span class="f-talk-look">${look}</span>
      <span class="f-talk-line">${said}${tone && tone !== "평범하게" ? ` <span class="mini-note">(${fEsc(tone)})</span>` : ""}${changed}</span>
    </button>`;
  }).join("");
  return `<div class="f-talk"><div class="f-talk-title">지금까지 나눈 대화 (누르면 그 컷으로 이동)</div>${rows}</div>`;
}

// 만드는 중 막대: 몇 개 완성됐는지, 지금 무엇을 만드는지, 얼마나 걸렸고 얼마나 남았는지
let fProgTick = null;
let fProgLocalStart = null;
// 서버가 응답을 안 하기 시작한 시각. 2026-09-23 서버가 20분 넘게 꺼져 있는 동안에도 경과 시간만 늘어서
// 느린 줄 알았다 -- 끊긴 건 끊겼다고 보여 준다.
let fOffline = null;
let fEtaSeen = null;  // 서버가 준 남은 시간과 받은 시각: 다음 응답까지 1초씩 줄인다
function fRenderProgress() {
  const box = $("f-progress");
  if (!box) return;
  const active = fp && fp.cuts.some(fActive);
  if (!active) {
    box.hidden = true; clearInterval(fProgTick); fProgTick = null; fProgLocalStart = null; return;
  }
  const total = fp.cuts.length;
  // 마지막 확인에서 다시 다듬는 컷도 이미 영상이 있으니 완성으로 센다 (2026-10-07: 23/23 → 21/23으로 줄어들어 보였음)
  const done = fp.cuts.filter((c) => c.clip_url).length;
  const waiting = fp.cuts.filter(fActive);
  const rendering = fp.cuts.map((c, i) => (c.status === "rendering" ? i + 1 : null)).filter(Boolean);
  const eta = fp.eta;
  let stage = (fp.render_stage && fp.render_stage.text) ||
    (rendering.length ? `${rendering.join(", ")}번 컷 만드는 중` : "차례를 기다리는 중");
  if (eta && eta.remaking && eta.remaking.length) stage = `마무리 — 뒤돌아선 ${eta.remaking.join(", ")}번 컷을 한 번 더 다듬는 중 · ${stage}`;
  if (!fProgLocalStart) fProgLocalStart = Date.now();
  const started = fp.render_stage && fp.render_stage.started ? fp.render_stage.started * 1000 : fProgLocalStart;
  // 남은 시간: 서버가 인물 사진·목소리·영상·마지막 확인까지 더해서 계산한 값을 받은 뒤 1초씩 줄여 보여 준다.
  // 예전엔 "대기 컷 수 × 90초"만 세서 사진 그리는 동안엔 멈춰 있다가 마지막 확인 때 갑자기 늘었다.
  // 컷을 더 눌러서 할 일이 늘면 늘어난 시간을 그대로 보여 준다 (그건 갑자기 늘어난 게 아니라 일이 늘어난 것)
  const newWork = fp.cuts.filter((c) => fActive(c) && !c.clip_url).length;  // 마지막 확인의 다시 만들기는 빼고 센다
  if (eta && (!fEtaSeen || fEtaSeen.pid !== fp.id || newWork > fEtaSeen.n)) fEtaSeen = { sec: eta.sec, raw: eta.sec, at: Date.now(), pid: fp.id, n: newWork };
  else if (eta && fEtaSeen.raw !== eta.sec) {
    fEtaSeen.n = newWork;
    const now = Date.now();
    const shown = fEtaSeen.sec - Math.max(0, now - fEtaSeen.at) / 1000;
    // 줄어든 값은 바로 반영. 조금 늘어난 값(2분 이내)은 숫자를 올리지 않고 그만큼 잠깐 멈춰 있다가 다시 줄인다 —
    // 남은 시간이 갑자기 늘어나 보이지 않게 (2026-10-07). 크게 늘면(다시 만들기 등) 사실대로 보여 준다.
    if (eta.sec <= shown || eta.sec - shown > 120) fEtaSeen = { sec: eta.sec, raw: eta.sec, at: now, pid: fp.id, n: newWork };
    else fEtaSeen = { sec: shown, raw: eta.sec, at: now + (eta.sec - shown) * 1000, pid: fp.id, n: newWork };
  }
  const perCut = fp.fast_preview ? 35 : 48;
  const draw = () => {
    const el = (Date.now() - started) / 1000;
    const left = eta ? fEtaSeen.sec - Math.max(0, Date.now() - fEtaSeen.at) / 1000 : waiting.length * perCut;
    const almost = left < 15;
    box.innerHTML = `
      <div class="f-progress-top"><span class="spinner"></span>
        <b>만드는 중… ${done}/${total}컷 완성</b>
        <span class="f-progress-stage">지금: ${fEsc(stage)}</span>
        <span class="mini-note">경과 ${fTime(el)} · ${almost ? "거의 다 됐어요" : `남은 시간 약 ${left >= 120 ? `${Math.round(left / 60)}분` : fTime(Math.ceil(left / 10) * 10)}`}</span></div>
      ${fOffline ? `<div class="f-progress-off">⚠ 서버 연결이 끊겼어요 (${fTime((Date.now() - fOffline) / 1000)}째). 지금은 만들어지지 않고 있어요 — 서버가 다시 켜지면 남은 컷부터 이어서 만듭니다.</div>` : ""}
      <div class="f-progress-bar"><span style="width:${Math.max(3, Math.round((done / total) * 100))}%"></span></div>`;
  };
  box.hidden = false;
  draw();
  clearInterval(fProgTick);
  fProgTick = setInterval(() => { if (!$("follow").hidden) draw(); }, 1000);
}

function fAiSlot(c) {
  if (c.clip_url) return `<video src="${c.clip_url}" controls loop playsinline></video>`;
  if (c.status === "rendering") return `<div class="f-placeholder f-working"><span class="spinner"></span>
    <span>만드는 중… <b id="f-elapsed">0초</b></span>
    <span class="mini-note" id="f-stage">${fEsc((fp.render_stage && fp.render_stage.text) || "인물 사진 → 목소리 → 영상 순서로 만듭니다")}</span></div>`;
  if (c.status === "queued") return `<div class="f-placeholder"><span class="spinner"></span> ${c.queue_pos ? `대기 ${c.queue_pos}번째` : "대기 중"} — 목소리를 먼저 만들고 차례대로 영상을 만들어요</div>`;
  if (c.status === "error") return `<div class="f-placeholder f-bad">만들지 못했어요<br /><span class="mini-note">${fEsc(c.error_message || "")}</span></div>`;
  if (fReady(c)) return `<div class="f-placeholder">⚡ 미리 만들어 뒀어요<br /><span class="mini-note">"이 컷 만들기"를 누르면 바로 나와요</span></div>`;
  return `<div class="f-placeholder">아직 안 만들었어요</div>`;
}

// ---------------------------------------------------------------- ✅ 검증된 동작 · 📜 프롬프트 기록 (2026-10-07)
// 같은 칩은 매번 같은 영어 문장이 들어간다 (gemma가 다시 쓰지 않음). 테스트 렌더에서 움직임이 잘 보이고 입이 가려지지 않은
// 문장에 ✅. 얼굴 클로즈업에서는 손이 화면 밖이라 얼굴·고개 동작만 고를 수 있다.
let fMotions = [];
// saves / translations still on their way: a render waits for them (see render() in fRenderEditor)
let fPending = [];
function fTrack(p) {
  const q = Promise.resolve(p);
  fPending.push(q);
  q.finally(() => { fPending = fPending.filter((x) => x !== q); });
  return q;
}
async function fLoadMotions() {
  try { fMotions = await getJSON("/api/follow/motions"); } catch { fMotions = []; }
  if (fp && fp.cuts && fp.cuts.length && $("f-editor") && document.activeElement?.id !== "f-action") fRenderEditor();
}
function fMotionChips(c) {
  if (!fMotions.length) return "";
  const close = (c.framing || "medium") === "closeup";
  const list = [...fMotions].sort((a, b) => (b.verified - a.verified) || (a.sort - b.sort)).filter((m) => m.result !== "bad");
  return `<div class="f-ex-row"><span class="mini-note">${list.some((m) => m.verified) ? "✅ 검증된 동작" : "🎬 동작 고르기"} (누르면 바로 들어가요):</span>${list.map((m) => {
    const off = close && !m.closeup_ok;
    return `<button type="button" class="chip f-ex ${(c.action || "") === m.action_en ? "active" : ""}" data-mlib="${m.key}" ${off ? "disabled" : ""}
      title="${off ? "얼굴 클로즈업에서는 손이 화면 밖이라 안 보여요" : fEsc(m.action_en)}">${m.verified ? "✅ " : ""}${fEsc(m.label)}</button>`;
  }).join("")}</div>`;
}
async function fLoadPromptHistory(c) {
  const box = $("f-hist-list");
  if (!box) return;
  let rows;
  try { rows = await getJSON(`/api/follow/${fp.id}/cuts/${c.id}/prompts`); } catch (e) { box.textContent = "불러오지 못했어요"; return; }
  if (!rows.length) { box.textContent = "아직 없어요 — 이 컷을 만들면 그때 쓴 프롬프트가 여기 쌓여요"; return; }
  box.innerHTML = rows.map((r) => `
    <div class="f-hist-row">
      <div class="f-hist-meta">${r.starred ? "⭐ " : ""}${fLocalTime(r.created_at)} · ${r.project_id === fp.id ? "이 영상" : `#${r.project_id}`}${r.from_cache ? " · 같은 설정 재사용" : ""}${r.current ? ' · <b>지금 쓰는 중</b>' : ""}</div>
      ${r.action ? `<div class="f-hist-act">행동: ${fEsc(r.action)}</div>` : ""}
      ${r.clip_url ? `<video src="${r.clip_url}" controls preload="none" playsinline></video>` : ""}
      <details><summary>프롬프트 전체 보기</summary><pre>${fEsc(r.prompt)}</pre></details>
      <div class="btn-row">
        <button type="button" class="btn btn-sm" data-hstar="${r.id}" data-on="${r.starred ? 0 : 1}">${r.starred ? "⭐ 빼기" : "⭐ 좋았어요"}</button>
        <button type="button" class="btn btn-sm btn-primary" data-huse="${r.id}">이 프롬프트로 다시 쓰기</button>
      </div>
    </div>`).join("");
  box.querySelectorAll("[data-hstar]").forEach((b) => b.addEventListener("click", async () => {
    try { await postJSON(`/api/follow/prompts/${b.dataset.hstar}/star`, { starred: b.dataset.on === "1" }); fLoadPromptHistory(c); } catch (e) { toast(fEsc(e.message), "bad"); }
  }));
  box.querySelectorAll("[data-huse]").forEach((b) => b.addEventListener("click", async () => {
    try {
      fp = await postJSON(`/api/follow/${fp.id}/cuts/${c.id}/use-prompt`, { history_id: +b.dataset.huse });
      toast("그 프롬프트와 시드로 바꿨어요 — '이 컷 만들기'를 누르면 같은 느낌으로 나와요", "info", 6000);
      fRenderAll();
    } catch (e) { toast(fEsc(e.message), "bad"); }
  }));
}

// 렌더가 도는 동안 경과 시간을 세어 준다 (사용자가 멈춘 줄 알까 봐)
let fTick = null;
function fStartTick() {
  clearInterval(fTick);
  const started = Date.now();
  fTick = setInterval(() => {
    const el = $("f-elapsed");
    if (!el) { clearInterval(fTick); fTick = null; return; }
    el.textContent = fTime((Date.now() - started) / 1000);
  }, 1000);
}

function fTime(sec) {
  const s = Math.round(sec || 0);
  return s >= 60 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${s}초`;
}

function fRenderFinal() {
  const done = fp.cuts.filter((c) => c.clip_url && !fActive(c)).length;
  if (fAssembleTick) return;  // 합치는 중: 1초씩 세는 표시를 덮어쓰지 않는다
  $("f-assemble").disabled = done < fp.cuts.length;
  const missing = fp.cuts.map((c, i) => (!c.clip_url || fActive(c)) ? i + 1 : null).filter(Boolean);
  const waiting = fp.cuts.some(fActive);
  $("f-assemble-status").innerHTML = missing.length
    ? `${done}/${fp.cuts.length} 컷 완성 · <b>${waiting ? "만드는 중/대기" : "아직 안 만든 컷"}: ${missing.join(", ")}번</b>` +
      (waiting ? "" : ` — "남은 컷 전부 만들기"를 누르면 합치기가 열립니다`)
    : `${done}/${fp.cuts.length} 컷 완성 — 합칠 수 있어요`;
  if (fAutoAssemble === fp.id && waiting) {
    // 마지막 컷을 "만들고 합치기"로 눌렀을 때: 기다리는 동안에도 초가 올라간다
    if (!fAutoAssembleStart) fAutoAssembleStart = Date.now();
    const draw = () => { if (!fAssembleTick && $("f-assemble-auto")) $("f-assemble-auto").textContent = fTime((Date.now() - fAutoAssembleStart) / 1000); };
    $("f-assemble-status").innerHTML += ` · <span class="spinner"></span> 마지막 컷이 끝나면 자동으로 합쳐요 — 경과 <b id="f-assemble-auto"></b>`;
    draw();
    if (!fAutoTick) fAutoTick = setInterval(() => { if (fAutoAssemble !== fp.id) { clearInterval(fAutoTick); fAutoTick = null; fAutoAssembleStart = null; return; } draw(); }, 1000);
  }
  const made = fp.cuts.filter((c) => c.render_sec);
  const totalSec = made.reduce((a, c) => a + c.render_sec, 0);
  const sum = (k) => made.reduce((a, c) => a + ((c.timing && c.timing[k]) || 0), 0);
  $("f-time").innerHTML = made.length ? `
    ${fp.last_render_sec ? `<b>"전부 만들기" 실제 걸린 시간 ${fTime(fp.last_render_sec)}</b><br />` : ""}
    <b>컷별 시간 합계 ${fTime(totalSec)}</b> <span class="mini-note">(만든 컷 ${made.length}개 · 컷당 평균 ${fTime(totalSec / made.length)} · 목소리를 한꺼번에 먼저 만드는 시간은 컷별 합계에 안 들어가요)</span>
    <div class="f-time-bar">
      ${[["인물 사진", sum("still"), "#a5b4fc"], ["목소리", sum("voice"), "#fbbf24"], ["영상 생성", sum("video"), "#34d399"], ["그 외", sum("other"), "#cbd5e1"]]
        .filter(([, v]) => v > 0)
        .map(([label, v, color]) => `<span style="flex:${v}; background:${color}" title="${label} ${fTime(v)}"></span>`).join("")}
    </div>
    <div class="mini-note">인물 사진 ${fTime(sum("still"))} · 목소리 ${fTime(sum("voice"))} · 영상 생성 ${fTime(sum("video"))} · 그 외 ${fTime(sum("other"))}
      ${fp.fast_preview ? "" : " — ⚡ 빠른 렌더를 켜면 영상 생성 시간이 절반 이하로 줄어요"}</div>` : "";
  $("f-final").innerHTML = fp.final_video_url ? `
    <figure><figcaption>AI 따라하기 결과</figcaption><video src="${fp.final_video_url}" controls playsinline></video></figure>
    <figure><figcaption>원본 | AI 나란히 비교</figcaption><video src="${fp.compare_video_url}" controls playsinline></video></figure>` : "";
}

// ---------------------------------------------------------------- 진행 상황 폴링
function fEnsurePoll() {
  const busy = fp && fp.cuts.some(fActive);
  if (busy && !fPoll) {
    fPoll = setInterval(async () => {
      if (!fp || $("follow").hidden) return;
      const before = new Map(fp.cuts.map((c) => [c.id, c.status]));
      const hadClip = new Map(fp.cuts.map((c) => [c.id, c.clip_url]));
      try { fp = await getJSON(`/api/follow/${fp.id}`); fOffline = null; } catch { if (!fOffline) { fOffline = Date.now(); fRenderProgress(); } return; }
      fp.cuts.forEach((c, i) => {
        const was = before.get(c.id);
        if (was === "rendering" && c.status === "done" && hadClip.get(c.id) !== c.clip_url) toast(hadClip.get(c.id) ? `✨ 컷 ${i + 1} 다시 다듬기 완료` : `✅ 컷 ${i + 1} 완성`);
        if (was === "rendering" && c.status === "error") toast(`❌ 컷 ${i + 1} 실패: ${fEsc(c.error_message)}`, "bad", 10000);
      });
      fRenderStrip();
      const c = fp.cuts[fCutIdx];
      const slot = $("f-ai-slot");
      // 입력 중인 칸은 건드리지 않고 상태/영상만 갱신
      if (c && slot && before.get(c.id) !== c.status) fRenderEditor();
      else if (c && $("f-cut-badge")) $("f-cut-badge").innerHTML = fStatusBadge(c);
      fRenderFinal();
      fRenderProgress();
      if ($("f-stage") && fp.render_stage && fp.render_stage.text) $("f-stage").textContent = fp.render_stage.text;
      if (!fp.cuts.some(fActive)) {
        clearInterval(fPoll); fPoll = null;
        const missing = fp.cuts.map((x, i) => (x.clip_url ? null : i + 1)).filter(Boolean);
        if (fAutoAssemble === fp.id) {
          fAutoAssemble = null;
          if (missing.length) toast(`${missing.join(", ")}번 컷이 아직 없어서 합치지 못했어요 — 그 컷을 만든 뒤 합치기를 눌러 주세요`, "bad", 10000);
          else fAssemble();
        } else if (fp.locked && !missing.length) fAssemble();  // 모든 컷이 생겼으면 누르지 않아도 합친다
      }
    }, 3000);
  }
}

// ---------------------------------------------------------------- 연결
function fSetup() {
  fSetupBgPanel();
  // 스토리 빌더 로고처럼: 따라하기/관리자 버튼을 누르면 항상 처음 화면(영상 선택하기)부터
  document.querySelectorAll("#mode-switch button").forEach((b) => b.addEventListener("click", () => {
    fRestart(b.dataset.mode);
  }));
  // 따라하기 화면에서 로고를 누르면 스토리 빌더가 아니라 따라하기 처음 화면으로
  $("brand-home").addEventListener("click", (e) => {
    if ($("follow").hidden) return;
    e.stopImmediatePropagation();
    fRestart(fAdmin ? "admin" : "follow");
  }, true);
  $("f-new").addEventListener("submit", fCreate);
  $("f-url-form").addEventListener("submit", fCreateFromUrl);
  $("f-published").addEventListener("change", (e) => fPatchProject({ published: e.target.checked }));
  $("f-description").addEventListener("change", (e) => fPatchProject({ description: e.target.value.trim() }));
  $("f-extend").addEventListener("click", async () => {
    const n = +$("f-extend-len").value;
    if (!confirm(`원본을 ${n}초까지 늘릴까요? 지금 컷은 그대로 두고 뒷부분 컷만 새로 나눕니다.`)) return;
    $("f-extend").disabled = true; $("f-extend").textContent = "늘리는 중…";
    try {
      const { job_id } = await postJSON(`/api/follow/${fp.id}/extend`, { length_sec: n });
      for (;;) {
        await new Promise((r) => setTimeout(r, 2500));
        const j = await getJSON(`/api/follow/jobs/${job_id}`);
        if (j.status === "done") { toast(`컷 ${j.result.added}개 추가 · 이제 ${Math.round(j.result.length_sec)}초`); break; }
        if (j.status === "error") throw new Error(j.error);
      }
      fp = await getJSON(`/api/follow/${fp.id}`); fRenderAll();
    } catch (e) { toast("늘리기 실패: " + fEsc(e.message), "bad"); }
    $("f-extend").disabled = false; $("f-extend").textContent = "원본 길이 늘리기";
  });
  const adminJob = (btnId, path, label, done) => $(btnId).addEventListener("click", async () => {
    const btn = $(btnId); const was = btn.textContent;
    btn.disabled = true; btn.textContent = label;
    try {
      const { job_id } = await postJSON(`/api/follow/${fp.id}/${path}`, {});
      for (;;) {
        await new Promise((r) => setTimeout(r, 3000));
        const jb = await getJSON(`/api/follow/jobs/${job_id}`);
        if (jb.status === "done") { toast(done(jb.result)); break; }
        if (jb.status === "error") throw new Error(jb.error);
      }
      fp = await getJSON(`/api/follow/${fp.id}`); fRenderAll();
    } catch (e) { toast(fEsc(e.message), "bad"); }
    btn.disabled = false; btn.textContent = was;
  });
  adminJob("f-rescan", "rescan", "분석 중… (컷당 몇 초)", (r) => `${r.updated}개 컷의 인원·구도·카메라를 원본에 맞췄어요`);
  adminJob("f-mimic", "mimic", "원본 연기 읽는 중…", (r) => `${r.updated}개 컷의 표정·행동·시선을 원본에서 가져왔어요`);
  $("f-translate").addEventListener("click", async () => {
    const btn = $("f-translate");
    btn.disabled = true; btn.textContent = "번역 중… (gemma)";
    try {
      const { job_id } = await postJSON(`/api/follow/${fp.id}/translate`, {});
      for (;;) {
        await new Promise((r) => setTimeout(r, 2500));
        const j = await getJSON(`/api/follow/jobs/${job_id}`);
        if (j.status === "done") { toast(`번역 ${j.result.translated}개 채움`); break; }
        if (j.status === "error") throw new Error(j.error);
      }
      fp = await getJSON(`/api/follow/${fp.id}`);
      fRenderAll();
    } catch (e) { toast("번역 실패: " + fEsc(e.message), "bad"); }
    btn.disabled = false; btn.textContent = "대사 영어 · 프롬프트 한국어 번역 채우기";
  });
  $("f-close").addEventListener("click", () => { fp = null; fStore(F_PROJECT_KEY, null); fRenderAll(); });
  $("f-delete").addEventListener("click", async () => {
    if (!confirm(fp.locked ? "내가 만든 이 따라하기 기록을 지울까요?" : "이 프리셋과 만든 컷을 모두 지울까요? (공개된 경우 영상 선택하기에서도 사라집니다)")) return;
    await del(`/api/follow/${fp.id}`);
    fp = null; fStore(F_PROJECT_KEY, null); fRenderAll();
  });
  $("f-setting").addEventListener("change", (e) => fPatchProject({ setting: e.target.value.trim() }));
  $("f-bg").addEventListener("change", (e) => fPatchProject(e.target.value ? { background_id: +e.target.value } : { clear_background: true }));
  $("f-fast").addEventListener("change", (e) => fPatchProject({ fast_preview: e.target.checked }));
  $("f-usestill").addEventListener("change", (e) => fPatchProject({ use_still: !e.target.checked }));
  $("f-render-all").addEventListener("click", async () => {
    const btn = $("f-render-all");
    btn.disabled = true; btn.textContent = "목소리 먼저 만드는 중…";
    try {
      const r = await postJSON(`/api/follow/${fp.id}/render-all`, {});
      // 다 만들어지면 따로 누르지 않아도 합치고 원본 비교 영상으로 내려간다 (2026-10-07 사용자 편의)
      if (fp.locked) fAutoAssemble = fp.id;
      toast(`${r.queued.length}개 컷 시작 — 다 만들어지면 자동으로 합쳐서 원본 비교 영상을 보여 드려요`, "info", 8000);
      fp = await getJSON(`/api/follow/${fp.id}`); fRenderAll(); fEnsurePoll();
    } catch (e) { toast(fEsc(e.message), "bad"); }
    btn.disabled = false; btn.textContent = "남은 컷 전부 만들기";
  });
  $("f-assemble").addEventListener("click", fAssemble);
  // 🎬 모든 컷 행동 AI로 정하기: 컷마다 대사·말투·앞뒤 대사를 보고 몸짓·표정·시선을 정해서 행동 칸(=프롬프트 action 줄)에 넣는다
  $("f-suggest-all").addEventListener("click", async (e) => {
    if (!fp) return;
    const todo = fp.cuts.filter((c) => c.cast_id && !fActive(c) && !c.prompt_edited);
    if (!todo.length) { toast("정할 컷이 없어요 (만드는 중이거나 프롬프트를 직접 고친 컷은 건너뛰어요)", "bad"); return; }
    if (!confirm(`${todo.length}개 컷의 행동을 AI가 새로 정해서 바꿀까요? (이미 만든 컷은 다시 만들어야 해요)`)) return;
    const btn = e.target; btn.disabled = true;
    const t0 = Date.now();
    let done = 0;
    const tick = setInterval(() => { $("f-suggest-status").innerHTML = `<span class="spinner"></span> AI가 ${todo.length}개 컷 행동을 한 번에 정하는 중… ${fTime((Date.now() - t0) / 1000)}`; }, 500);
    try {
      const r = await postJSON(`/api/follow/${fp.id}/suggest-all`, {});
      fp = r.project; done = r.updated;
    } catch (err) { toast(fEsc(err.message), "bad", 8000); }
    clearInterval(tick);
    $("f-suggest-status").textContent = `✔ ${done}개 컷 행동을 정했어요 (${fTime((Date.now() - t0) / 1000)}) — 컷을 눌러 확인하고, 마음에 안 들면 고쳐 쓰세요`;
    btn.disabled = false;
    fRenderAll();
  });
}

// 합치기: 1초, 2초… 세면서 기다리고, 끝나면 원본 비교 영상으로 내려간다
let fAssembleTick = null;
async function fAssemble() {
  $("f-assemble").disabled = true;
  const started = Date.now();
  const sec = () => fTime((Date.now() - started) / 1000);
  const show = () => { $("f-assemble-status").innerHTML = `<span class="spinner"></span> 컷 합치는 중… <b>${sec()}</b>`; };
  show();
  clearInterval(fAssembleTick);
  fAssembleTick = setInterval(show, 1000);
  let took = null;
  try {
    const { job_id } = await postJSON(`/api/follow/${fp.id}/assemble`, {});
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000));
      const j = await getJSON(`/api/follow/jobs/${job_id}`);
      if (j.status === "done") break;
      if (j.status === "error") throw new Error(j.error);
    }
    took = sec();
    fp = await getJSON(`/api/follow/${fp.id}`);
    toast(`완성 영상과 비교 영상을 만들었습니다 (합치기 ${took})`);
  } catch (e) { toast("합치기 실패: " + fEsc(e.message), "bad"); }
  clearInterval(fAssembleTick); fAssembleTick = null;
  fRenderAll();
  if (took) $("f-assemble-status").textContent += ` · 합치기 ${took} 걸림`;
  if (fp.compare_video_url && $("f-final")) $("f-final").scrollIntoView({ block: "start" });  // not smooth: a background tab never runs the animation
}

async function fInit() {
  fSetup();
  fLoadMotions();
  // app.js init 이 인물/배경/목소리를 불러올 때까지 잠깐 기다림
  for (let i = 0; i < 50 && !(state.characters.length && state.voices.length); i++) await new Promise((r) => setTimeout(r, 100));
  if (new URLSearchParams(location.search).has("admin")) fStore("aivp.admin", 1);
  $("mode-admin").hidden = fLoad("aivp.admin") !== "1";
  $("mode-switch").hidden = $("mode-admin").hidden;  // 따라하기 하나만 남아서 관리자일 때만 전환 버튼을 보여 줌
  const saved = parseInt(fLoad(F_PROJECT_KEY) || "", 10);
  if (saved) { try { fp = await getJSON(`/api/follow/${saved}`); } catch { fStore(F_PROJECT_KEY, null); } }
  let mode = fLoad(F_MODE_KEY) || "follow";
  if (mode !== "admin" || $("mode-admin").hidden) mode = "follow";
  if (fp && !!fp.locked === (mode === "admin")) { fp = null; fStore(F_PROJECT_KEY, null); }
  fAdmin = mode === "admin";
  setMode(mode);
}
document.addEventListener("DOMContentLoaded", fInit);


// ---------------------------------------------------------------- 번역기
let trTarget = "en";
function trSetDir(target) {
  trTarget = target;
  document.querySelectorAll("[data-tr]").forEach((b) => b.classList.toggle("active", b.dataset.tr === target));
}
function trOpen(text, target, run) {
  $("tr-panel").hidden = false;
  if (text != null) $("tr-in").value = text;
  if (target) trSetDir(target);
  if (run) trGo();
}
async function trGo() {
  const text = $("tr-in").value.trim();
  if (!text) return;
  $("tr-go").disabled = true;
  $("tr-status").textContent = "번역 중… (영상 만드는 중이면 조금 더 걸려요)";
  try {
    const r = await postJSON("/api/translate", { text, target: trTarget });
    $("tr-out").value = r.text;
    $("tr-status").textContent = r.cached ? "완료 (저장된 번역)" : "완료";
  } catch (e) {
    let msg = e.message; try { msg = JSON.parse(msg).detail || msg; } catch {}
    $("tr-status").textContent = "실패: " + msg;
  }
  $("tr-go").disabled = false;
}
document.addEventListener("DOMContentLoaded", () => {
  $("f-save-preset").addEventListener("click", async (e) => {
    if (!fp) return;
    e.currentTarget.disabled = true;
    try {
      const saved = await postJSON(`/api/follow/${fp.id}/save-preset`, {});
      toast(`「${fEsc(saved.title)}」 저장 완료. 영상 선택 목록에서 같은 기준으로 다시 만들 수 있어요.`, "info", 6000);
    } catch (err) { toast(fEsc(err.message), "bad"); }
    finally { $("f-save-preset").disabled = false; }
  });
  $("tr-open").addEventListener("click", () => { $("tr-panel").hidden = !$("tr-panel").hidden; });
  $("tr-close").addEventListener("click", () => { $("tr-panel").hidden = true; });
  document.querySelectorAll("[data-tr]").forEach((b) => b.addEventListener("click", () => trSetDir(b.dataset.tr)));
  $("tr-go").addEventListener("click", trGo);
  $("tr-in").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) trGo(); });
  $("tr-swap").addEventListener("click", () => {
    if (!$("tr-out").value) return;
    $("tr-in").value = $("tr-out").value; $("tr-out").value = "";
    trSetDir(trTarget === "en" ? "ko" : "en");
  });
  $("tr-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("tr-out").value); $("tr-status").textContent = "복사했습니다"; }
    catch { $("tr-out").select(); document.execCommand("copy"); }
  });
});


// ---------------------------------------------------------------- 오른쪽 해설 패널 + 프롬프트 만들기
let fLessonTimer = null;
function fShowLesson(cut) {
  const c = cut || fp.cuts[fCutIdx];
  if (!c) return;
  $("f-lesson-body").innerHTML = c.prompt_parts.map((p) => `
    <div class="f-part f-part-${p.key}">
      <div class="f-part-label">${fEsc(p.label)}</div>
      <div class="f-part-body"><div class="f-part-note">${fEsc(p.note)}</div><code>${fEsc(p.text)}</code>
        ${p.text_ko ? `<div class="f-trans">한국어 · ${fEsc(p.text_ko)}</div>` : ""}</div>
    </div>`).join("");
  $("f-lesson-panel").hidden = false;
  clearTimeout(fLessonTimer);
  fLessonTimer = setTimeout(() => { $("f-lesson-panel").hidden = true; }, 20000);
}

function fBuilderInit() {
  const opt = (arr) => arr.map((t) => `<option value="${t.key}">${fEsc(t.label)}</option>`).join("");
  $("fb-tone").innerHTML = opt((fp && fp.tone_options) || [{ key: "", label: "평범하게" }]);
  $("fb-motion").innerHTML = opt((fp && fp.motion_options) || [{ key: "", label: "고정" }]);
}

async function fBuilderMake() {
  const who = $("fb-who").value.trim() || "a young person";
  const place = $("fb-place").value.trim() || "a board-game cafe";
  const look = $("fb-look").value;
  const action = $("fb-action").value.trim();
  const toneLabel = $("fb-tone").selectedOptions[0] ? $("fb-tone").selectedOptions[0].textContent : "";
  const motionLabel = $("fb-motion").selectedOptions[0] ? $("fb-motion").selectedOptions[0].textContent : "";
  $("fb-make").disabled = true;
  try {
    // 한국어로 적어도 되게: 사람/장소/행동만 영어로 옮겨서 문장을 조립
    const [whoEn, placeEn, actionEn] = await Promise.all([
      postJSON("/api/translate", { text: who, target: "en" }).then((r) => r.text),
      postJSON("/api/translate", { text: place, target: "en" }).then((r) => r.text),
      action ? postJSON("/api/translate", { text: action, target: "en" }).then((r) => r.text) : Promise.resolve(""),
    ]);
    const gaze = look === "camera"
      ? "The person looks straight into the camera."
      : `The person is talking with a friend just off-screen to the ${look}; they keep looking off-camera to the ${look} and never look into the camera lens.`;
    $("fb-out").value = [
      `A 2-second medium close-up from a candid drama scene filmed on a cinema camera, one person in frame.`,
      `The person is ${whoEn}.`, `Location: ${placeEn}, softly out of focus.`, gaze,
      toneLabel && toneLabel !== "평범하게" ? `The line is delivered in a "${toneLabel}" mood and the face matches it.` : "",
      motionLabel && motionLabel !== "고정" ? `Movement: ${motionLabel}.` : "",
      actionEn ? `Action: ${actionEn}` : "",
      `Camera: handheld medium close-up with a very slight natural sway, shallow depth of field, no zoom, no pan, no cut.`,
    ].filter(Boolean).join(" ");
  } catch (e) { toast("만들기 실패: " + fEsc(e.message), "bad"); }
  $("fb-make").disabled = false;
}

document.addEventListener("DOMContentLoaded", () => {
  $("f-lesson-open").addEventListener("click", () => fShowLesson());
  $("f-lesson-close").addEventListener("click", () => { $("f-lesson-panel").hidden = true; });
  $("f-builder-open").addEventListener("click", () => {
    const b = $("f-builder");
    b.hidden = !b.hidden;
    if (!b.hidden) fBuilderInit();
  });
  $("fb-make").addEventListener("click", fBuilderMake);
  $("fb-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("fb-out").value); toast("복사했습니다", "info", 2500); } catch {}
  });
  $("fb-ko").addEventListener("click", () => trOpen($("fb-out").value, "ko", true));
});

// ---------------------------------------------------------------- 배경: 예시 · 직접 쓰기 · ChatGPT로 만들기
let fBgInfo = null;       // {examples, tips, writing_examples, openai}
let fBgBusy = false;
let fBgShowAll = false;
const fErrText = (e) => { try { return JSON.parse(e.message).detail || e.message; } catch { return e.message; } };

async function fLoadBgInfo(force) {
  if (!fBgInfo || force) {
    try { fBgInfo = await getJSON("/api/follow/backgrounds/examples"); } catch { fBgInfo = null; }
  }
  fRenderBgPanel();
}

function fRenderBgPanel() {
  if (!fp || !$("f-bgpanel")) return;
  const b = fp.background || {};
  $("f-bg-current").innerHTML = `
    ${b.image_url ? `<img src="${b.image_url}" alt="" />` : `<div class="f-noimg">사진 없음<br />(글로만)</div>`}
    <div>
      <b>지금 배경${b.name ? ` · ${fEsc(b.name)}` : ""}</b>
      ${b.prompt_ko ? `<div class="f-bg-ko">🇰🇷 ${fEsc(b.prompt_ko)}</div>` : ""}
      <div class="f-bg-en">🇺🇸 ${fEsc(b.prompt_en || "")}</div>
    </div>`;
  if (!fBgInfo) { $("f-bg-examples").innerHTML = `<span class="mini-note">불러오는 중…</span>`; return; }
  // 저장된 배경이 30개가 넘어 페이지가 너무 길었다 (2026-10-07): ⭐ 추천 + 지금 배경만 보이고 나머지는 "더 보기"
  const shownBg = fBgShowAll ? fBgInfo.examples
    : fBgInfo.examples.filter((x, i) => x.recommended || fp.background_id === x.id || i < 8);
  const moreBg = fBgInfo.examples.length - shownBg.length;
  $("f-bg-examples").innerHTML = shownBg.map((x) => `
    <div class="f-bg-card ${fp.background_id === x.id ? "active" : ""}" data-id="${x.id}">
      <img src="${x.image_url}" alt="" />
      <b>${x.recommended ? "⭐ " : ""}${fEsc(x.name)}${x.recommended ? ' <span class="mini-note">추천</span>' : x.source === "local" ? ' <span class="mini-note">직접 만든 배경</span>' : x.source === "gpt" ? ' <span class="mini-note">ChatGPT</span>' : ""}</b>
      <div class="f-bg-ko">${fEsc(x.prompt_ko)}</div>
      <div class="f-bg-en">${fEsc(x.prompt_en)}</div>
      <div class="btn-row">
        <button type="button" class="btn btn-sm btn-primary f-bg-use">이 배경 쓰기</button>
        <button type="button" class="btn btn-sm f-bg-copy">문장 가져오기</button>
      </div>
    </div>`).join("") +
    (moreBg > 0 ? `<button type="button" class="btn btn-sm" id="f-bg-more">직접 만든 배경 ${moreBg}개 더 보기</button>`
      : fBgShowAll ? `<button type="button" class="btn btn-sm" id="f-bg-more">접기</button>` : "");
  if ($("f-bg-more")) $("f-bg-more").addEventListener("click", () => { fBgShowAll = !fBgShowAll; fRenderBgPanel(); });
  $("f-bg-samples").innerHTML = fBgInfo.writing_examples.map((w, i) => `
    <button type="button" class="f-bg-sample" data-i="${i}">
      <span class="f-bg-sample-n">예시 ${i + 1}</span>
      <span class="f-bg-ko">${fEsc(w.ko)}</span><span class="f-bg-en">${fEsc(w.en)}</span></button>`).join("");
  $("f-bg-tips").innerHTML = `
    <ol class="f-bg-tiplist">${fBgInfo.tips.map((t) => `<li>${fEsc(t.ko)}<br /><span class="f-bg-en">${fEsc(t.en)}</span></li>`).join("")}</ol>`;
  $("f-bg-gen").title = "Z-Image-Turbo 모델로 이 PC에서 그려요 (비용 없음, 보통 20~40초)";
  $("f-bg-examples").querySelectorAll(".f-bg-card").forEach((card) => {
    const id = +card.dataset.id; const x = fBgInfo.examples.find((e) => e.id === id);
    card.querySelector(".f-bg-use").addEventListener("click", () => fBgAction("use", { background_id: id }, `'${x.name}' 배경으로 바꿨어요`));
    card.querySelector(".f-bg-copy").addEventListener("click", () => { $("f-bg-text").value = x.prompt_ko || x.prompt_en; $("f-bg-text").focus(); });
  });
  $("f-bg-samples").querySelectorAll(".f-bg-sample").forEach((btn) => btn.addEventListener("click", () => {
    $("f-bg-text").value = fBgInfo.writing_examples[+btn.dataset.i].ko; $("f-bg-text").focus();
  }));
  ["f-bg-gen", "f-bg-orig"].forEach((id) => { $(id).disabled = fBgBusy || fp.cuts.some(fActive); });
  fShowBgRefined();
}

// use / write / generate: the last two run as jobs; the seconds count up while waiting
async function fBgAction(kind, body, doneMsg) {
  if (fBgBusy) return;
  fBgBusy = true; fRenderBgPanel();
  const started = Date.now();
  const label = { use: "배경 바꾸는 중", write: "한국어·영어로 정리하는 중", generate: (body && body.en ? "" : "AI가 문장을 정리하고 ") + "배경 사진을 그리는 중 (이 PC에서 · 처음엔 모델을 불러오느라 1분쯤)" }[kind];
  const tick = setInterval(() => { $("f-bg-status").innerHTML = `<span class="spinner"></span> ${label}… <b>${fTime((Date.now() - started) / 1000)}</b>`; }, 1000);
  $("f-bg-status").innerHTML = `<span class="spinner"></span> ${label}…`;
  try {
    const r = await postJSON(`/api/follow/${fp.id}/background/${kind}`, body);
    if (r.job_id) {
      for (;;) {
        await new Promise((res) => setTimeout(res, 2000));
        const j = await getJSON(`/api/follow/jobs/${r.job_id}`);
        if (j.status === "done") break;
        if (j.status === "error") throw new Error(JSON.stringify({ detail: j.error }));
      }
    }
    fp = await getJSON(`/api/follow/${fp.id}`);
    toast(`${doneMsg} (${fTime((Date.now() - started) / 1000)}) — 새 배경의 인물 사진을 지금부터 미리 만들어 둘게요`, "info", 8000);
    $("f-bg-status").textContent = "";
    if (kind === "generate") await fLoadBgInfo(true);
  } catch (e) {
    $("f-bg-status").textContent = "";
    toast(fEsc(fErrText(e)), "bad", 10000);
  }
  clearInterval(tick);
  fBgBusy = false;
  fRenderAll();
}

// ✨ 배경: 대충 쓴 장소를 gemma가 "빈 장소 사진"용 한국어·영어 프롬프트로 정리 (2026-10-02). 정리한 문장을 그대로
// 두고 그리면 그 영어를 쓰고, 고치거나 정리 없이 그리면 그릴 때 서버가 한 번 더 정리한다.
let fBgRefined = null;
function fShowBgRefined() {
  const box = $("f-bg-refined");
  const r = fBgRefined;
  if (!r || $("f-bg-text").value.trim() !== r.ko) { box.hidden = true; return; }
  box.innerHTML = `<b>✨ AI가 정리했어요${r.sec ? ` (${fTime(r.sec)})` : ""}</b> · 이름: ${fEsc(r.name)}<br>
    <span class="f-bg-en">🇺🇸 ${fEsc(r.en)}</span><br><span class="mini-note">마음에 들면 바로 "배경 사진 만들기", 고쳐 써도 돼요</span>`;
  box.hidden = false;
}

async function fBgRefine() {
  const text = $("f-bg-text").value.trim();
  if (!text) { toast("배경을 대충이라도 먼저 적어 주세요 (예: 비오는 밤 편의점)", "bad", 4000); return; }
  const btn = $("f-bg-refine"); btn.disabled = true;
  const t0 = Date.now();
  const tick = setInterval(() => { $("f-bg-status").innerHTML = `<span class="spinner"></span> AI가 배경 문장을 정리하는 중… <b>${fTime((Date.now() - t0) / 1000)}</b>`; }, 500);
  try {
    fBgRefined = await postJSON(`/api/follow/${fp.id}/refine`, { kind: "background", text });
    $("f-bg-text").value = fBgRefined.ko;
    fShowBgRefined();
    $("f-bg-status").textContent = "";
  } catch (e) { $("f-bg-status").textContent = ""; toast(fEsc(e.message), "bad", 8000); }
  clearInterval(tick);
  btn.disabled = false;
}

function fSetupBgPanel() {
  $("f-bg-gen").addEventListener("click", () => {
    const text = $("f-bg-text").value.trim();
    if (!text) { toast("만들 배경을 먼저 적어 주세요 (예시를 눌러도 돼요)", "bad"); return; }
    const r = fBgRefined && text === fBgRefined.ko ? fBgRefined : null;
    fBgAction("generate", r ? { text, en: r.en, name: r.name } : { text }, r ? "정리한 배경으로 바꿨어요" : "AI가 정리해서 그린 배경으로 바꿨어요");
  });
  $("f-bg-refine").addEventListener("click", fBgRefine);
  $("f-bg-text").addEventListener("input", fShowBgRefined);
  $("f-bg-orig").addEventListener("click", () => fBgAction("use", { original: true }, "원본 배경으로 되돌렸어요"));
}
