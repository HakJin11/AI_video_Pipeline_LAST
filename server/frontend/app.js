// =====================================================================
// AI 영상 파이프라인 — 장르·템플릿 위저드
//   장르 → 템플릿 → 인물 → 구성 → 캐스팅 → 대본 → 완성
//   선택하면 다음 스텝으로 슬라이드 전환. 전 과정 /api/stories/* 파이프라인.
// =====================================================================

// 장르 -> 템플릿 -> 인물 -> 구성 -> 캐스팅 -> 대본 -> 완성
const WIZ_ORDER = ["genre", "template", "cast", "config", "casting", "script", "produce"];
const STEP_ORDER = ["genre", "cast", "config", "script", "produce"];   // 스텝퍼 5칸
const STEP_GROUP = { genre: "genre", template: "genre", cast: "cast", config: "config", casting: "config", script: "script", produce: "produce" };
const GROUP_ENTRY = { genre: "genre", cast: "cast", config: "config", script: "script", produce: "produce" };

const SCENE_COUNTS = [1, 2, 3, 4, 6, 8];
const TOTAL_SECS = [10, 20, 30, 45, 60, 90, 120];
const LS_KEY = "aivp.storyId";

const GENRES = [
  { key: "romance", name: "로맨스", emoji: "💗", tone: "설렘과 감정선 중심, 따뜻하게" },
  { key: "comedy", name: "코미디", emoji: "😆", tone: "과장된 상황과 티키타카, 가볍게" },
  { key: "thriller", name: "스릴러", emoji: "😱", tone: "긴장과 반전, 서늘하게" },
  { key: "drama", name: "감동 드라마", emoji: "🥲", tone: "관계와 성장, 진하게" },
  { key: "mystery", name: "미스터리", emoji: "🕵️", tone: "단서와 의심, 서서히 밝혀지게" },
  { key: "action", name: "액션", emoji: "🔥", tone: "속도와 대립, 강하게" },
];
const TEMPLATES = {
  romance: [
    "3년 만에 우연히 재회", "고백하려다 계속 실패", "소개팅 첫 만남", "썸인지 아닌지 확인하는 대화",
    "헤어지자는 말을 꺼내는 날", "짝사랑을 들켜버림", "결혼하자고 말하는 순간", "오래된 연인의 권태기",
  ],
  comedy: [
    "돈 빌려간 친구가 모른 척함", "내 야식을 몰래 먹은 룸메이트", "무리한 부탁을 계속하는 직장 동료",
    "허세 부리다 들통난 사람", "약속에 매번 늦는 친구", "다이어트 한다더니 몰래 먹다 걸림",
    "잘못 보낸 카톡을 수습하려다 더 꼬임", "서로 상대가 계산할 줄 알았던 밥값",
  ],
  thriller: [
    "동업자가 돈을 들고 사라짐", "협박 전화를 받음", "누가 나를 미행하고 있음", "비밀을 아는 사람이 찾아옴",
    "알리바이가 맞지 않음", "거래는 끝나지 않았다", "믿었던 사람의 정체", "돌아오지 말았어야 할 곳",
  ],
  drama: [
    "오랜 친구가 어려운 부탁을 함", "가족이 숨겨온 비밀", "떠나기 전 마지막 인사", "병실에서 나눈 대화",
    "꿈을 포기하겠다고 말함", "미안하다는 말을 못 했던 사이", "빚을 갚으러 온 사람", "다시 시작하자는 제안",
  ],
  mystery: [
    "사라진 친구의 마지막 문자", "그날 밤 무슨 일이 있었나", "낯선 목격자의 증언", "닫힌 방에서 벌어진 일",
    "거짓말을 하고 있는 사람", "오래된 사진 속 한 사람", "같은 꿈을 꾸는 두 사람", "잘못 배달된 상자",
  ],
  action: [
    "쫓기는 자와 쫓는 자", "마지막 임무 브리핑", "배신자를 색출함", "시간이 얼마 없다",
    "인질을 두고 벌이는 담판", "무기를 겨눈 채 나누는 대화", "탈출 직전 마지막 선택", "약속된 장소에서의 대치",
  ],
};

const state = { characters: [], backgrounds: [], voices: [] };
const pick = {
  char1: null, char2: null,
  genre: null, template: null,
  sceneCount: 4, totalSec: 80, bgIds: [],
  voice1: null, voice2: null, keyword: "",
  fastPreview: true,
};
let story = null;
let wizStep = "genre";

// ---------- utils ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/"/g, "&quot;");
function fmtSec(s) {
  if (s == null) return "-";
  if (s < 60) return `${s.toFixed(1)}초`;
  return `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`;
}
const mediaUrl = (p) => `/media/${p}`;
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function saveStoryId(id) { try { id ? localStorage.setItem(LS_KEY, String(id)) : localStorage.removeItem(LS_KEY); } catch {} }
function loadStoryId() { try { const v = localStorage.getItem(LS_KEY); return v ? parseInt(v, 10) : null; } catch { return null; } }
const genreByKey = (k) => GENRES.find((g) => g.key === k) || null;
const voiceById = (id) => state.voices.find((v) => v.id === id) || null;

// ---------- api ----------
async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(await r.text()); return r.json(); }
async function req(url, method, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
const postJSON = (u, b) => req(u, "POST", b);
const patchJSON = (u, b) => req(u, "PATCH", b);
async function del(url) { const r = await fetch(url, { method: "DELETE" }); if (!r.ok) throw new Error(await r.text()); return r.json(); }

// ---------- wizard nav ----------
function goStep(name) {
  // leaving the 대본 step: keep what was typed in the scene on screen
  if (wizStep === "script" && name !== "script" && currentScene) {
    currentScene.save().catch((e) => alert("저장 실패: " + e.message));
  }
  wizStep = name;
  document.querySelectorAll(".wiz-step").forEach((s) => s.classList.toggle("active", s.dataset.step === name));
  const group = STEP_GROUP[name];
  const gi = STEP_ORDER.indexOf(group);
  $("stepper").querySelectorAll(".step").forEach((btn, i) => {
    btn.classList.toggle("active", btn.dataset.step === group);
    btn.classList.toggle("done", i < gi);
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
  const r = STEP_RENDER[name];
  if (r) r();
}
function setupStepper() {
  $("brand-home").addEventListener("click", restartAll);
  $("stepper").querySelectorAll(".step").forEach((btn) => {
    btn.addEventListener("click", () => goStep(GROUP_ENTRY[btn.dataset.step]));
  });
  document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => goStep(b.dataset.go)));
}

// =====================================================================
// STEP: 인물
// =====================================================================
async function loadCharacters() { state.characters = await getJSON("/api/characters"); renderCast(); }
function renderCast() {
  renderCastStatus();
  const el = $("cast-grid");
  el.innerHTML = "";
  state.characters.forEach((c) => {
    const s1 = pick.char1?.id === c.id, s2 = pick.char2?.id === c.id;
    const card = document.createElement("div");
    card.className = "tile" + (s1 || s2 ? " sel" : "");
    card.innerHTML = `
      ${s1 || s2 ? `<div class="slot-badge">${s1 ? 1 : 2}</div>` : ""}
      <img src="${mediaUrl(c.image_path)}" alt="${c.name}" />
      <div class="tile-body">
        <div class="tile-name">${c.name} <span class="g">${c.gender}</span></div>
        <div class="tile-desc">${c.description}</div>
        <div class="pick-btns">
          <button class="btn${s1 ? " on" : ""}" data-slot="1" ${s2 ? "disabled" : ""}>인물1</button>
          <button class="btn${s2 ? " on" : ""}" data-slot="2" ${s1 ? "disabled" : ""}>인물2</button>
        </div>
        <button class="tile-del">삭제</button>
      </div>`;
    card.querySelector('[data-slot="1"]').addEventListener("click", () => pickSlot(1, c));
    card.querySelector('[data-slot="2"]').addEventListener("click", () => pickSlot(2, c));
    card.querySelector(".tile-del").addEventListener("click", async () => {
      if (!confirm(`${c.name} 삭제?`)) return;
      await del(`/api/characters/${c.id}`);
      if (pick.char1?.id === c.id) pick.char1 = null;
      if (pick.char2?.id === c.id) pick.char2 = null;
      await loadCharacters();
    });
    el.appendChild(card);
  });
}
function pickSlot(n, c) {
  const key = n === 1 ? "char1" : "char2", other = n === 1 ? "char2" : "char1";
  if (pick[key]?.id === c.id) pick[key] = null;
  else { pick[key] = c; if (pick[other]?.id === c.id) pick[other] = null; }
  renderCast();
}
function renderCastStatus() {
  const p = (c) => (c ? `<b>${c.name}</b>` : "<b>미선택</b>");
  $("cast-status").innerHTML = `인물1: ${p(pick.char1)} &nbsp;·&nbsp; 인물2: ${p(pick.char2)}`;
  $("cast-next").disabled = !(pick.char1 && pick.char2);
}
function setupCharUploadForm() {
  const form = $("char-upload-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("char-upload-image").files[0];
    const name = $("char-upload-name").value.trim();
    const description = $("char-upload-desc").value.trim();
    if (!file || !name || !description) return;
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "추가 중...";
    try {
      const fd = new FormData();
      fd.append("image", file); fd.append("name", name);
      fd.append("description", description); fd.append("gender", $("char-upload-gender").value);
      const r = await fetch("/api/characters", { method: "POST", body: fd });
      if (!r.ok) throw new Error(await r.text());
      form.reset();
      await loadCharacters();
    } catch (err) { alert("추가 실패: " + err.message); }
    finally { btn.disabled = false; btn.textContent = "추가"; }
  });
}

// =====================================================================
// STEP: 장르 / 템플릿
// =====================================================================
function renderGenre() {
  const el = $("genre-grid");
  el.innerHTML = "";
  GENRES.forEach((g) => {
    const card = document.createElement("button");
    card.className = "genre-card" + (pick.genre === g.key ? " on" : "");
    card.innerHTML = `<span class="emoji">${g.emoji}</span><span class="g-name">${g.name}</span><span class="g-tone">${g.tone}</span>`;
    card.addEventListener("click", () => {
      pick.genre = g.key;
      pick.template = null;
      setTimeout(() => goStep("template"), 120);
    });
    el.appendChild(card);
  });
}
function renderTemplate() {
  const g = genreByKey(pick.genre);
  $("template-title").textContent = g ? `${g.name} — 어떤 상황?` : "상황을 골라주세요";
  const el = $("template-grid");
  el.innerHTML = "";
  (TEMPLATES[pick.genre] || []).forEach((t) => {
    const card = document.createElement("button");
    card.className = "template-card" + (pick.template === t ? " on" : "");
    card.textContent = t;
    card.addEventListener("click", () => { pick.template = t; setTimeout(() => goStep("cast"), 120); });
    el.appendChild(card);
  });
  const custom = document.createElement("button");
  custom.className = "template-card custom";
  custom.textContent = "직접 설정 (템플릿 없이 키워드만으로)";
  custom.addEventListener("click", () => { pick.template = null; setTimeout(() => goStep("cast"), 120); });
  el.appendChild(custom);
}

// =====================================================================
// STEP: 구성
// =====================================================================
async function loadBackgrounds() { state.backgrounds = await getJSON("/api/backgrounds"); if (wizStep === "config") renderConfig(); }
function chipRow(elId, values, current, fmt, onPick) {
  const el = $(elId);
  el.innerHTML = "";
  values.forEach((v) => {
    const c = document.createElement("button");
    c.className = "chip" + (v === current ? " active" : "");
    c.textContent = fmt(v);
    c.addEventListener("click", () => onPick(v));
    el.appendChild(c);
  });
}
function renderConfig() {
  chipRow("scene-count-chips", SCENE_COUNTS, pick.sceneCount,
    (v) => (v === 1 ? "1개 (단일)" : `${v}개`), (v) => { pick.sceneCount = v; renderConfig(); });
  chipRow("total-sec-chips", TOTAL_SECS, pick.totalSec,
    (v) => `${v}초`, (v) => { pick.totalSec = v; renderConfig(); });
  chipRow("quality-chips", [true, false], pick.fastPreview,
    (v) => (v ? "⚡ 빠른 미리보기" : "🎬 고화질 최종본"),
    (v) => { pick.fastPreview = v; renderConfig(); });
  $("quality-hint").innerHTML = pick.fastPreview
    ? `<b>640×360</b>으로 뽑습니다 (업스케일 패스 생략). 씬당 <b>2분 안팎</b> — 이야기·연기를 빠르게 확인할 때.`
    : `<b>1280×720</b> 전체 품질. 씬당 <b>5분 안팎</b> — 마음에 드는 대본이 나온 뒤에.`;
  renderBgGrid();
  renderConfigStatus();
}
function perSceneSec() {
  return Math.round(pick.totalSec / Math.max(1, pick.sceneCount));
}
function renderConfigStatus() {
  const g = genreByKey(pick.genre);
  const bgs = pick.bgIds.map((id) => state.backgrounds.find((b) => b.id === id)).filter(Boolean)
    .map((b, i) => `${i + 1}.${b.name}`).join(" · ");
  const est = pick.sceneCount * (pick.fastPreview ? 2 : 6);
  const per = perSceneSec();
  $("config-status").innerHTML =
    `${g ? g.emoji + " " + g.name : ""}${pick.template ? ` · "${pick.template}"` : " · 직접 설정"}` +
    `<div class="mini-note">총 <b>${pick.totalSec}초</b> · 씬 ${pick.sceneCount}개 · 씬당 약 ${per}초 · 렌더 예상 ${est}분` +
    (bgs ? ` · 배경 ${bgs}` : " · 배경 미선택") + `</div>` +
    (per < 5 ? `<div class="warn-note">씬당 ${per}초는 두 사람이 한마디씩 하기엔 짧습니다. 총 길이를 늘리거나 씬을 줄이세요.</div>` : "");
  $("config-next").disabled = pick.bgIds.length === 0;
}
function toggleBg(id) {
  const i = pick.bgIds.indexOf(id);
  if (i >= 0) pick.bgIds.splice(i, 1); else pick.bgIds.push(id);
  renderConfig();
}
function renderBgGrid() {
  const el = $("bg-grid");
  el.innerHTML = "";
  state.backgrounds.forEach((b) => {
    const order = pick.bgIds.indexOf(b.id);
    const card = document.createElement("div");
    card.className = "tile" + (order >= 0 ? " sel" : "");
    card.innerHTML = `
      ${order >= 0 ? `<div class="bg-order">${order + 1}</div>` : ""}
      <img src="${mediaUrl(b.image_path)}" alt="${b.name}" />
      <div class="tile-body">
        <div class="tile-name">${b.name}</div>
        <div class="tile-desc">${b.description}</div>
        <div class="pick-btns"><button class="btn${order >= 0 ? " on" : ""}">${order >= 0 ? "선택됨" : "배경으로"}</button></div>
        <button class="tile-del">삭제</button>
      </div>`;
    card.querySelector(".btn").addEventListener("click", () => toggleBg(b.id));
    card.querySelector(".tile-del").addEventListener("click", async () => {
      await del(`/api/backgrounds/${b.id}`);
      const i = pick.bgIds.indexOf(b.id); if (i >= 0) pick.bgIds.splice(i, 1);
      await loadBackgrounds();
    });
    el.appendChild(card);
  });
}

// =====================================================================
// STEP: 캐스팅 (목소리)
// =====================================================================
async function loadVoices() {
  state.voices = await getJSON("/api/voices");
  if (wizStep === "casting") renderCasting();
}
function renderCasting() {
  // prefill from each character's saved voice_id
  if (!pick.voice1 && pick.char1?.voice_id) pick.voice1 = voiceById(pick.char1.voice_id);
  if (!pick.voice2 && pick.char2?.voice_id) pick.voice2 = voiceById(pick.char2.voice_id);
  $("voice1-label").textContent = `${pick.char1?.name || "인물1"} 목소리`;
  $("voice2-label").textContent = `${pick.char2?.name || "인물2"} 목소리`;
  renderVoiceGrid("pick-voice1", pick.voice1, (v) => { pick.voice1 = v; renderCasting(); });
  renderVoiceGrid("pick-voice2", pick.voice2, (v) => { pick.voice2 = v; renderCasting(); });
  renderCastingStatus();
}
function renderVoiceGrid(containerId, selected, onSelect) {
  const el = $(containerId);
  el.innerHTML = "";
  state.voices.forEach((item) => {
    const card = document.createElement("div");
    card.className = "pick-card" + (selected && selected.id === item.id ? " selected" : "");
    card.innerHTML = `<div class="thumb-audio"><button type="button" class="play-btn">▶</button></div><div class="pick-name">${item.name}</div>`;
    card.addEventListener("click", () => onSelect(selected && selected.id === item.id ? null : item));
    card.querySelector(".play-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      new Audio(mediaUrl(item.reference_audio_path)).play();
    });
    el.appendChild(card);
  });
}
function renderCastingStatus() {
  const v = (x) => (x ? `<b>${x.name}</b>` : "<b>미선택</b>");
  $("casting-status").innerHTML =
    `${pick.char1?.name || "인물1"}: ${v(pick.voice1)} &nbsp;·&nbsp; ${pick.char2?.name || "인물2"}: ${v(pick.voice2)}`;
  $("btn-create-story").disabled = !(pick.voice1 && pick.voice2 && pick.char1 && pick.char2 && pick.bgIds.length);
}

// voice bank record / upload
let mediaRecorder = null, recordedChunks = [], recordedBlob = null;
function setupVoiceRecorder() {
  const startBtn = $("btn-record-start"), stopBtn = $("btn-record-stop");
  const status = $("record-status"), preview = $("voice-record-preview");
  startBtn.addEventListener("click", async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recordedChunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
      mediaRecorder.onstop = () => {
        recordedBlob = new Blob(recordedChunks, { type: "audio/webm" });
        preview.src = URL.createObjectURL(recordedBlob);
        preview.style.display = "block";
        stream.getTracks().forEach((t) => t.stop());
        status.textContent = "녹음 완료 — 이름 입력 후 추가하세요.";
      };
      mediaRecorder.start();
      startBtn.disabled = true; stopBtn.disabled = false;
      status.textContent = "🔴 녹음 중...";
    } catch (err) { alert("마이크에 접근할 수 없습니다: " + err.message); }
  });
  stopBtn.addEventListener("click", () => {
    if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
    startBtn.disabled = false; stopBtn.disabled = true;
  });
}
function setupVoiceUploadForm() {
  const form = $("voice-upload-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("voice-upload-name").value.trim();
    const file = $("voice-upload-audio").files[0] || recordedBlob;
    if (!file || !name) { alert("녹음하거나 파일을 고르고 이름을 입력하세요."); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "추가 중...";
    try {
      const fd = new FormData();
      fd.append("audio", file, file.name || "recording.webm");
      fd.append("name", name); fd.append("gender", $("voice-upload-gender").value);
      const r = await fetch("/api/voices", { method: "POST", body: fd });
      if (!r.ok) throw new Error(await r.text());
      form.reset(); recordedBlob = null;
      $("voice-record-preview").style.display = "none";
      $("record-status").textContent = "";
      await loadVoices();
    } catch (err) { alert("추가 실패: " + err.message); }
    finally { btn.disabled = false; btn.textContent = "추가"; }
  });
}

// =====================================================================
// library import
// =====================================================================
async function loadImportLists() {
  const data = await getJSON("/api/library-import/scan");
  const fill = (elId, label, handler) => {
    const el = $(elId);
    el.innerHTML = "";
    data.images.forEach((fname) => {
      const item = document.createElement("div");
      item.className = "import-item";
      item.innerHTML = `<span>${fname}</span><button>${label}</button>`;
      item.querySelector("button").addEventListener("click", () => handler(fname));
      el.appendChild(item);
    });
  };
  fill("import-character-list", "인물로", async (fname) => {
    const name = prompt("인물 이름", fname.slice(0, 12)); if (!name) return;
    const description = prompt("설명 (영문 권장)", "");
    const gender = prompt("성별 (male/female/unspecified)", "unspecified") || "unspecified";
    await postJSON("/api/library-import/character", { filename: fname, name, description, gender });
    await loadCharacters();
  });
  fill("import-background-list", "배경으로", async (fname) => {
    const name = prompt("배경 이름", fname.slice(0, 12)); if (!name) return;
    const description = prompt("설명 (영문 권장)", "");
    await postJSON("/api/library-import/background", { filename: fname, name, description });
    await loadBackgrounds();
  });
  const vEl = $("import-voice-list");
  vEl.innerHTML = "";
  data.audio.forEach((fname) => {
    const item = document.createElement("div");
    item.className = "import-item";
    item.innerHTML = `<span>${fname}</span><button>목소리로</button>`;
    item.querySelector("button").addEventListener("click", async () => {
      const name = prompt("목소리 이름", fname.slice(0, 12)); if (!name) return;
      const gender = prompt("성별 (male/female/unspecified)", "unspecified") || "unspecified";
      await postJSON("/api/library-import/voice", { filename: fname, name, gender });
      await loadVoices();
    });
    vEl.appendChild(item);
  });
}

// =====================================================================
// STEP: 대본
// =====================================================================
function buildPremise() {
  const g = genreByKey(pick.genre);
  const kw = $("story-keyword").value.trim();
  pick.keyword = kw;
  let s = g ? `${g.name} 장르` : "";
  if (pick.template) s += `, "${pick.template}" 설정`;
  if (kw) s += `. ${kw}`;
  return s || kw || "이야기";
}
async function pollStoryJob(jobId, label, onDone, boxId) {
  const box = $(boxId);
  const text = box.querySelector("span:last-child");
  box.classList.add("on");
  const t0 = Date.now();
  try {
    while (true) {
      const job = await getJSON(`/api/stories/jobs/${jobId}`);
      text.textContent = `${label}... ${fmtSec((Date.now() - t0) / 1000)} 경과`;
      if (job.status === "done") { if (onDone) await onDone(); return; }
      if (job.status === "error") { alert(label + " 실패: " + job.error); if (onDone) await onDone(); return; }
      await new Promise((r) => setTimeout(r, 2000));
    }
  } finally { box.classList.remove("on"); }
}
async function createStory() {
  if (!(pick.char1 && pick.char2 && pick.voice1 && pick.voice2 && pick.bgIds.length)) return;
  const btn = $("btn-create-story");
  btn.disabled = true; btn.textContent = "생성 중...";
  try {
    const premise = buildPremise();
    const created = await postJSON("/api/stories", {
      premise,
      title: pick.template || pick.keyword || genreByKey(pick.genre)?.name || "",
      character1_id: pick.char1.id,
      character2_id: pick.char2.id,
      voice1_id: pick.voice1.id,
      voice2_id: pick.voice2.id,
      scene_count: pick.sceneCount,
      target_total_sec: pick.totalSec,
      background_ids: pick.bgIds,
      fast_preview: pick.fastPreview,
    });
    story = created;
    sceneIdx = 0;
    saveStoryId(created.id);
    goStep("script");
    const { job_id } = await postJSON(`/api/stories/${created.id}/outline`, {});
    await pollStoryJob(job_id, "gemma가 씬별 대본 작성 중 (씬당 약 20초)", async () => {
      story = await getJSON(`/api/stories/${created.id}`);
      renderScript();
    }, "script-progress");
  } catch (err) {
    alert("스토리 생성 실패: " + err.message);
  } finally {
    btn.disabled = false; btn.textContent = "스토리 대본 만들기";
  }
}
function newStory() {
  story = null; saveStoryId(null);
  pick.template = null; pick.keyword = "";
  goStep("genre");
}

// 좌측 상단 로고 = 전부 비우고 1단계 인물부터 다시. 만들던 스토리는 서버에 남아 있어서
// "완성" 단계의 "이전에 만든 스토리 불러오기"로 언제든 되찾을 수 있다.
function restartAll() {
  if (story && !confirm("처음부터 새로 시작할까요?\n(만든 스토리는 지워지지 않고, 완성 단계에서 다시 불러올 수 있습니다)")) return;
  stopScenePoll();
  story = null;
  sceneIdx = 0;
  currentScene = null;
  saveStoryId(null);
  Object.assign(pick, {
    char1: null, char2: null, genre: null, template: null,
    sceneCount: 4, totalSec: 80, bgIds: [],
    voice1: null, voice2: null, keyword: "", fastPreview: true,
  });
  const kw = $("story-keyword"); if (kw) kw.value = "";
  goStep("genre");
}

function totalRenderSec() {
  return (story?.scenes || []).reduce((a, s) => a + (s.render_sec || 0), 0);
}
function renderedCount() {
  return (story?.scenes || []).filter((s) => s.clip_path).length;
}
function renderScript() {
  const sb = $("script-summary");
  if (!story) { sb.innerHTML = '먼저 캐스팅 단계에서 "스토리 대본 만들기"를 누르세요.'; $("storyboard").innerHTML = ""; return; }
  const total = totalRenderSec();
  sb.innerHTML = `<b>${story.title || story.premise}</b>` +
    `<div class="mini-note">${story.premise} · ${story.fast_preview ? "⚡ 빠른 미리보기" : "🎬 고화질"}` +
    ` · 렌더 완료 ${renderedCount()}/${story.scenes.length}` +
    (total ? ` · 지금까지 ${fmtSec(total)}` : "") + `</div>`;
  renderStoryboard();
}

// ---- 씬별 렌더 상태 (텍스트 편집을 날리지 않게 상태 부분만 갱신) ----
function sceneStatusHtml(sc) {
  const t = sc.render_sec ? ` <span class="mini-note">${fmtSec(sc.render_sec)} 걸림</span>` : "";
  if (sc.status === "done") return `<span class="badge ok">완료</span>${t}`;
  if (sc.status === "rendering") return `<span class="badge warn">렌더 중…</span>`;
  if (sc.status === "queued") return `<span class="badge queued">대기${sc.queue_pos ? ` ${sc.queue_pos}번째` : ""}</span>` +
    (sc.clip_url ? ` <span class="mini-note">이전 영상은 새 영상이 나올 때까지 유지</span>` : "");
  if (sc.status === "error") return `<span class="badge bad">실패</span> <span class="mini-note">${sc.error_message || ""}</span>`;
  return `<span class="badge idle">렌더 안 됨</span>`;
}
function scenePreviewHtml(sc) {
  return sc.clip_url ? `<video src="${sc.clip_url}" controls preload="metadata"></video>` : "";
}
function updateSceneStatuses() {
  if (!story) return;
  story.scenes.forEach((sc) => {
    const card = document.querySelector(`.scene-panel[data-scene="${sc.id}"]`);
    if (!card) return;
    const st = card.querySelector('[data-role="status"]');
    if (st) st.innerHTML = sceneStatusHtml(sc);
    const pv = card.querySelector('[data-role="preview"]');
    if (pv && pv.dataset.clip !== (sc.clip_url || "")) {
      pv.dataset.clip = sc.clip_url || "";
      pv.innerHTML = scenePreviewHtml(sc);
    }
    const btn = card.querySelector('[data-act="render"]');
    if (btn) {
      const busy = sc.status === "rendering" || sc.status === "queued";
      btn.disabled = busy;
      btn.textContent = sc.status === "rendering" ? "렌더 중…" : sc.status === "queued" ? "대기 중…" : (sc.clip_path ? "다시 렌더" : "이 씬 렌더");
    }
  });
  renderSceneRail();   // 다른 씬이 뒤에서 끝나면 레일 점 색이 바뀌어야 한다
  const sb = $("script-summary");
  if (sb && wizStep === "script") {
    const total = totalRenderSec();
    const note = sb.querySelector(".mini-note");
    if (note) {
      note.innerHTML = `${story.premise} · ${story.fast_preview ? "⚡ 빠른 미리보기" : "🎬 고화질"}` +
        ` · 렌더 완료 ${renderedCount()}/${story.scenes.length}` + (total ? ` · 지금까지 ${fmtSec(total)}` : "");
    }
  }
}
// ---- 완료 알림 ----
function toast(msg, kind = "ok", ms = 6000) {
  let box = $("toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.appendChild(box); }
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = msg;
  t.addEventListener("click", () => t.remove());
  box.appendChild(t);
  setTimeout(() => t.remove(), ms);
}
const BASE_TITLE = document.title;
const isActive = (s) => s.status === "rendering" || s.status === "queued";

let scenePoll = null;
function stopScenePoll() { if (scenePoll) { clearInterval(scenePoll); scenePoll = null; } }
function ensureScenePoll() {
  if (scenePoll) return;
  scenePoll = setInterval(async () => {
    if (!story) return stopScenePoll();
    let fresh;
    try { fresh = await getJSON(`/api/stories/${story.id}`); } catch { return; }
    let finishedNow = false;
    let changed = false;
    // merge ONLY render-side fields, so whatever the user is typing right now survives
    fresh.scenes.forEach((fs, i) => {
      const cur = story.scenes.find((x) => x.id === fs.id);
      if (!cur) return;
      const was = cur.status;
      if (was !== fs.status || cur.clip_path !== fs.clip_path || cur.queue_pos !== fs.queue_pos) changed = true;
      Object.assign(cur, {
        status: fs.status, clip_path: fs.clip_path, clip_url: fs.clip_url, queue_pos: fs.queue_pos,
        render_sec: fs.render_sec, error_message: fs.error_message,
      });
      if ((was === "rendering" || was === "queued") && fs.status === "done") {
        toast(`✅ <b>씬 ${i + 1} 렌더 완료</b>${fs.render_sec ? ` · ${fmtSec(fs.render_sec)}` : ""}`);
        finishedNow = true;
      } else if ((was === "rendering" || was === "queued") && fs.status === "error") {
        toast(`⚠️ <b>씬 ${i + 1} 렌더 실패</b> · ${fs.error_message || ""}`, "bad", 12000);
      }
    });
    story.status = fresh.status;
    updateSceneStatuses();
    // rebuild the 완성 timeline only when something actually moved; rebuilding it every tick
    // re-created every <video> and restarted whatever clip the user was watching
    if (changed && wizStep === "produce") { renderTimeline(); refreshRenderAllBtn(); }
    const active = story.scenes.filter(isActive).length;
    document.title = active ? `⏳ 렌더 ${active}개 남음 — ${BASE_TITLE}` : BASE_TITLE;
    if (!active) {
      stopScenePoll();
      if (finishedNow && story.scenes.every((s) => s.clip_path)) {
        toast(`🎬 <b>모든 씬 렌더 완료</b> — "완성" 단계에서 최종 영상을 만드세요`, "ok", 15000);
        document.title = `✅ 렌더 완료 — ${BASE_TITLE}`;
      }
    }
  }, 3000);
}
function bgImg(id) { const b = state.backgrounds.find((x) => x.id === id); return b ? mediaUrl(b.image_path) : ""; }
function transitionOptions(sel) {
  return [["fade", "디졸브 (부드럽게)"], ["blackout", "암전 (시간·장소 점프)"], ["cut", "컷 (바로)"]]
    .map(([v, t]) => `<option value="${v}" ${sel === v ? "selected" : ""}>${t}</option>`).join("");
}
// 대본 단계는 씬을 한 번에 하나씩 보여준다 (sceneIdx). 렌더는 백그라운드로 돌리고 화면만 다음 씬으로.
let sceneIdx = 0;
let currentScene = null;   // { id, save } — 지금 화면에 떠 있는 씬

async function renderCurrentSceneClip() {
  if (!story || !currentScene) return;
  const sc = story.scenes.find((x) => x.id === currentScene.id);
  if (!sc) return;
  if (!(sc.line1 || "").trim() || !(sc.line2 || "").trim()) {
    alert("이 씬에 대사가 없습니다. 먼저 대사를 채우세요.");
    throw new Error("no lines");
  }
  await postJSON(`/api/stories/${story.id}/scenes/${sc.id}/render`, {});
  sc.status = "queued";
  ensureScenePoll();
}

async function advanceScene(withRender) {
  if (!story) return;
  try {
    if (currentScene) await currentScene.save();
    if (withRender) await renderCurrentSceneClip();
  } catch (err) {
    if (err && err.message !== "no lines") alert("실패: " + err.message);
    return;
  }
  if (sceneIdx < story.scenes.length - 1) gotoScene(sceneIdx + 1);
  else goStep("produce");
}

// save whatever is typed in the scene on screen before the screen changes
async function saveCurrentScene() {
  if (!currentScene) return true;
  try { await currentScene.save(); return true; }
  catch (e) { alert("저장 실패: " + e.message); return false; }
}

async function gotoScene(i) {
  if (!story) return;
  if (!(await saveCurrentScene())) return;
  sceneIdx = Math.max(0, Math.min(i, story.scenes.length - 1));
  renderStoryboard();
  const wrap = $("storyboard");
  wrap.classList.remove("slide-in");
  void wrap.offsetWidth; // reflow so the animation replays
  wrap.classList.add("slide-in");
  $("phase-script") || window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderSceneRail() {
  const rail = $("scene-rail");
  if (!rail || !story) return;
  rail.innerHTML = "";
  story.scenes.forEach((sc, i) => {
    const b = document.createElement("button");
    b.className = `rail-item s-${sc.status}` + (i === sceneIdx ? " on" : "");
    b.innerHTML = `<span class="rail-no">${i + 1}</span><span class="rail-dot"></span>`;
    b.title = `씬 ${i + 1} — ${{ done: "완료", rendering: "렌더 중", error: "실패" }[sc.status] || "렌더 안 됨"}`;
    b.addEventListener("click", () => gotoScene(i));
    rail.appendChild(b);
  });
  // 지금 보고 있는 씬의 순서를 여기서 바로 옮긴다
  const move = document.createElement("span");
  move.className = "rail-move";
  move.innerHTML = `<button data-m="-1" ${sceneIdx === 0 ? "disabled" : ""} title="앞으로">◀</button>` +
                   `<button data-m="1" ${sceneIdx === story.scenes.length - 1 ? "disabled" : ""} title="뒤로">▶</button>`;
  move.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => moveSceneAndFollow(parseInt(b.dataset.m, 10))));
  rail.appendChild(move);

  const done = story.scenes.filter((s) => s.clip_path).length;
  const tail = document.createElement("span");
  tail.className = "mini-note";
  tail.style.marginLeft = "8px";
  tail.textContent = `씬 ${sceneIdx + 1} / ${story.scenes.length} · 렌더 완료 ${done}개`;
  rail.appendChild(tail);
}

// 순서를 바꾸고 화면은 방금 옮긴 그 씬을 계속 보여준다
async function moveSceneAndFollow(dir) {
  const target = sceneIdx + dir;
  if (!story || target < 0 || target >= story.scenes.length) return;
  if (!(await saveCurrentScene())) return;
  try {
    await moveScene(sceneIdx, dir);
    gotoScene(target);
  } catch (e) { alert("순서 변경 실패: " + e.message); }
}

function updateSceneNav() {
  if (!story) return;
  const last = sceneIdx === story.scenes.length - 1;
  $("scene-next").textContent = last ? "렌더하고 완성으로 →" : "렌더하고 다음 씬 →";
  $("scene-skip").textContent = last ? "그냥 완성으로 →" : "그냥 다음 →";
  $("scene-prev").textContent = sceneIdx === 0 ? "← 캐스팅" : "← 이전 씬";
  // 구성 단계에서 정한 씬 개수를 넘겨서 추가하지 못하게
  const full = story.scenes.length >= story.scene_count;
  const add = $("btn-add-scene");
  add.disabled = full;
  add.title = full ? `구성 단계에서 ${story.scene_count}개로 정했습니다` : "";
  add.textContent = full ? `씬 ${story.scene_count}개 (설정된 최대)` : "+ 씬 추가";
}

function renderStoryboard() {
  const wrap = $("storyboard");
  wrap.innerHTML = "";
  if (!story) return;
  if (sceneIdx > story.scenes.length - 1) sceneIdx = story.scenes.length - 1;
  renderSceneRail();
  updateSceneNav();
  [story.scenes[sceneIdx]].forEach((sc) => {
    const idx = sceneIdx;
    const bgOpts = state.backgrounds
      .map((b) => `<option value="${b.id}" ${b.id === sc.background_id ? "selected" : ""}>${b.name}</option>`).join("");
    const card = document.createElement("div");
    card.className = "scene-panel";
    card.dataset.scene = sc.id;
    card.innerHTML = `
      <div class="scene-strip" style="background-image:url('${bgImg(sc.background_id)}')">
        <div class="scene-no">씬 ${idx + 1}<span class="scene-badge">${sc.background_name || ""}</span></div>
        <select data-f="background_id">${bgOpts}</select>
      </div>
      <div class="scene-body">
        <div class="scene-preview" data-role="preview" data-clip="${sc.clip_url || ""}">${scenePreviewHtml(sc)}</div>
        <label class="scene-field situation"><span>상황</span>
          <input data-f="situation" type="text" value="${esc(sc.situation)}" /></label>
        <label class="scene-field line-field l1"><span>${story.character1_name || "인물1"}</span>
          <textarea data-f="line1">${sc.line1 || ""}</textarea></label>
        <label class="scene-field line-field l2"><span>${story.character2_name || "인물2"}</span>
          <textarea data-f="line2">${sc.line2 || ""}</textarea></label>
        <details class="prompt-box"${sc.ltx_prompt ? "" : ""}>
          <summary>영상 프롬프트 ${sc.ltx_prompt ? "<span class='badge ok'>저장됨</span>" : "<span class='badge idle'>아직 없음</span>"} <span class="mini-note">LTX에 실제로 들어가는 지시문</span></summary>
          <textarea data-f="ltx_prompt" placeholder="비어 있으면 렌더할 때 gemma가 만들어서 여기에 저장합니다. 직접 고쳐도 됩니다.">${sc.ltx_prompt || ""}</textarea>
          <div class="scene-foot">
            <button data-act="mkprompt" class="btn btn-sm">${sc.ltx_prompt ? "프롬프트 다시 만들기" : "프롬프트 미리 만들기"}</button>
            <span class="mini-note">배경·인물 설명과 대사 타이밍이 여기 들어갑니다</span>
          </div>
        </details>
        <div class="scene-foot">
          <button data-act="regen" class="btn btn-sm">대사 다시 (gemma)</button>
          <button data-act="save" class="btn btn-sm">저장</button>
          <button data-act="del" class="btn btn-sm btn-danger" ${story.scenes.length <= 1 ? "disabled" : ""}>씬 삭제</button>
          <button data-act="render" class="btn btn-sm btn-primary">${sc.clip_path ? "다시 렌더" : "이 씬 렌더"}</button>
          <span data-role="status">${sceneStatusHtml(sc)}</span>
          <span class="mini-note grow" data-role="len"></span>
        </div>
      </div>`;
    const lenNote = card.querySelector('[data-role="len"]');
    // live length vs this scene's per-line limit; over the limit the line is still said in full,
    // but this scene then runs longer than its share of 총 길이
    const lim = (story.line_limits || [])[idx] || [];
    const fmtLim = (n, m) => !m ? `${n}자`
      : n > m ? `<span style="color:var(--bad);font-weight:700">${n}/${m}자</span>` : `${n}/${m}자`;
    const showLen = () => {
      const a = card.querySelector('[data-f="line1"]').value.length;
      const b = card.querySelector('[data-f="line2"]').value.length;
      const over = (lim[0] && a > lim[0]) || (lim[1] && b > lim[1]);
      lenNote.innerHTML = `${fmtLim(a, lim[0])} / ${fmtLim(b, lim[1])}` +
        (over ? ` <span style="color:var(--bad)">— 권장보다 깁니다: 목소리가 조금 빨라지고, 많이 길면 이 씬이 설정보다 길어집니다</span>` : "");
    };
    showLen();
    card.querySelectorAll("textarea").forEach((t) => t.addEventListener("input", showLen));
    const save = async () => {
      const body = {};
      const cur = story.scenes.find((x) => x.id === sc.id) || sc;
      card.querySelectorAll("[data-f]").forEach((inp) => {
        const k = inp.dataset.f;
        const v = k === "background_id" ? parseInt(inp.value, 10) : inp.value;
        // send only what the user actually changed -- an untouched field must never look like an
        // edit, or the server throws away the finished clip (and the prompt stops being refreshed)
        if (v === (k === "background_id" ? cur[k] : (cur[k] || ""))) return;
        body[k] = v;
      });
      if (!Object.keys(body).length) return;
      const updated = await patchJSON(`/api/stories/${story.id}/scenes/${sc.id}`, body);
      story.scenes[story.scenes.findIndex((x) => x.id === sc.id)] = updated;
    };
    // the wizard nav buttons below the card act on whatever scene is showing
    currentScene = { id: sc.id, save };
    // save as soon as a field is left (blur), so an edit can't be lost by clicking elsewhere
    card.querySelectorAll("[data-f]").forEach((el) =>
      el.addEventListener("change", () => save().catch((e) => alert("저장 실패: " + e.message))));
    card.querySelector('[data-act="save"]').addEventListener("click", () =>
      save().then(() => renderStoryboard()).catch((e) => alert(e.message)));
    card.querySelector('[data-act="del"]').addEventListener("click", async () => {
      if (!confirm(`씬 ${idx + 1} 삭제?`)) return;
      try { story = await del(`/api/stories/${story.id}/scenes/${sc.id}`); renderStoryboard(); }
      catch (e) { alert(e.message); }
    });
    card.querySelector('[data-act="regen"]').addEventListener("click", async (e) => {
      e.target.disabled = true; e.target.textContent = "생성 중...";
      try {
        await save();
        const updated = await postJSON(`/api/stories/${story.id}/scenes/${sc.id}/regenerate-lines`, {});
        story.scenes[story.scenes.findIndex((x) => x.id === sc.id)] = updated;
        renderStoryboard();
      } catch (err) {
        alert("대사 생성 실패: " + err.message);
        e.target.disabled = false; e.target.textContent = "대사 다시 (gemma)";
      }
    });
    card.querySelector('[data-act="mkprompt"]').addEventListener("click", async (e) => {
      e.preventDefault();
      e.target.disabled = true; e.target.textContent = "만드는 중...";
      try {
        await save();
        const updated = await postJSON(`/api/stories/${story.id}/scenes/${sc.id}/prompt`, {});
        story.scenes[story.scenes.findIndex((x) => x.id === sc.id)] = updated;
        renderStoryboard();
        const box = document.querySelector(`.scene-panel[data-scene="${sc.id}"] .prompt-box`);
        if (box) box.open = true;
      } catch (err) {
        alert("프롬프트 생성 실패: " + err.message);
        e.target.disabled = false; e.target.textContent = "프롬프트 만들기";
      }
    });
    // fire-and-forget: queue this scene's render and keep editing the next one while it runs
    card.querySelector('[data-act="render"]').addEventListener("click", async () => {
      try {
        await save();
        await postJSON(`/api/stories/${story.id}/scenes/${sc.id}/render`, {});
        sc.status = "queued";
        updateSceneStatuses();
        ensureScenePoll();
      } catch (err) { alert("렌더 요청 실패: " + err.message); }
    });
    wrap.appendChild(card);
  });
  if (story.scenes.some(isActive)) ensureScenePoll();
}
async function addScene() {
  if (!story) return;
  const bgId = pick.bgIds[0] || (state.backgrounds[0] && state.backgrounds[0].id);
  if (!bgId) return alert("먼저 배경을 등록하세요.");
  story = await postJSON(`/api/stories/${story.id}/scenes`, { background_id: bgId });
  gotoScene(story.scenes.length - 1);
}

// =====================================================================
// STEP: 완성
// =====================================================================
async function renderProduce() {
  await renderStoryList();
  const sum = $("produce-summary");
  if (!story) {
    $("produce-workspace").hidden = true;
    sum.innerHTML = '대본을 먼저 만들거나, 아래에서 이전 스토리를 불러오세요.';
    return;
  }
  story = await getJSON(`/api/stories/${story.id}`);
  $("produce-workspace").hidden = false;
  const total = totalRenderSec();
  sum.innerHTML = `<b>${story.title || story.premise}</b> — 씬 ${story.scenes.length}개 · ` +
    `${story.fast_preview ? "⚡ 빠른 미리보기" : "🎬 고화질"} · 상태 ${story.status}` +
    `<div class="mini-note">렌더 완료 ${renderedCount()}/${story.scenes.length}` +
    (total ? ` · <b>총 렌더 시간 ${fmtSec(total)}</b>` : "") + `</div>` +
    (story.error_message ? `<div class="warn-note">⚠️ ${story.error_message}</div>` : "");
  $("story-intro").value = story.intro_text || "";
  $("story-outro").value = story.outro_text || "";
  $("story-music-gain").value = story.music_gain ?? 0.25;
  $("music-gain-val").textContent = story.music_gain ?? 0.25;
  refreshRenderAllBtn();
  renderTimeline();
  renderFinal();
  if (story.scenes.some(isActive)) ensureScenePoll();
}

// 씬은 대본 단계에서 하나씩 렌더하므로, 여기 버튼은 "아직 안 만든 씬"을 몰아서 대기열에 넣는 용도다.
function refreshRenderAllBtn() {
  const btn = $("btn-render-all");
  if (!btn || !story) return;
  const left = story.scenes.filter((s) => !s.clip_path && !isActive(s)).length;
  btn.hidden = left === 0;
  btn.textContent = `아직 안 만든 씬 ${left}개 렌더`;

  // 최종 영상은 모든 씬이 다 만들어진 뒤에만
  const ab = $("btn-assemble");
  if (ab) {
    const notReady = story.scenes.filter((s) => !s.clip_path || isActive(s)).length;
    ab.disabled = notReady > 0;
    ab.textContent = notReady
      ? `최종 영상 만들기 — 씬 ${notReady}개가 아직 안 만들어졌습니다`
      : "최종 영상 만들기 (편집 반영, GPU 없이 ~20초)";
  }
}
async function renderStoryList() {
  const el = $("story-list");
  let list = [];
  try { list = await getJSON("/api/stories"); } catch { el.innerHTML = "목록을 불러올 수 없습니다."; return; }
  if (!list.length) { el.innerHTML = "아직 만든 스토리가 없습니다."; return; }
  el.innerHTML = "";
  list.forEach((s) => {
    const item = document.createElement("div");
    item.className = "import-item";
    item.innerHTML = `<span>#${s.id} ${s.title || s.premise.slice(0, 16)} (${s.status})</span><button>불러오기</button>`;
    item.querySelector("button").addEventListener("click", async () => {
      story = await getJSON(`/api/stories/${s.id}`);
      saveStoryId(s.id);
      renderProduce();
    });
    el.appendChild(item);
  });
}
function badge(sc) {
  const m = { done: ["ok", "완료"], rendering: ["warn", "렌더 중"], queued: ["queued", sc.queue_pos ? `대기 ${sc.queue_pos}번째` : "대기"], error: ["bad", "실패"], pending: ["idle", "렌더 안 됨"] };
  const [cls, txt] = m[sc.status] || ["idle", sc.status];
  return `<span class="badge ${cls}">${txt}</span>`;
}
function renderTimeline() {
  const wrap = $("timeline");
  wrap.innerHTML = "";
  story.scenes.forEach((sc, idx) => {
    const card = document.createElement("div");
    card.className = "tl-card";
    card.innerHTML = `
      <div class="tl-head">씬 ${idx + 1} ${badge(sc)} <span class="mini-note">${sc.background_name || ""}${sc.render_sec ? ` · ${fmtSec(sc.render_sec)} 걸림` : ""}</span>
        <span class="move">
          <button data-act="up" ${idx === 0 ? "disabled" : ""}>▲</button>
          <button data-act="down" ${idx === story.scenes.length - 1 ? "disabled" : ""}>▼</button>
        </span></div>
      ${sc.clip_url ? `<video src="${sc.clip_url}" controls preload="metadata"></video>`
        : `<div class="tl-noclip">${sc.error_message ? "⚠️ " + sc.error_message : "아직 렌더 안 됨"}</div>`}
      <div class="tl-lines">
        <div class="sit">${sc.situation || ""}</div>
        <div>1: ${sc.line1 || "—"}</div><div>2: ${sc.line2 || "—"}</div>
        <div class="mini-note">대사 수정은 "대본" 단계에서</div>
      </div>
      <div class="tl-edit">
        <label>이 씬으로 넘어올 때<select data-f="transition">${transitionOptions(sc.transition)}</select></label>
        <label>자막<input data-f="caption" type="text" value="${esc(sc.caption)}" placeholder="예: 3년 후" /></label>
      </div>
      <div class="scene-foot">
        <button data-act="edit" class="btn btn-sm">✎ 이 씬 대본 고치기</button>
        <button data-act="save" class="btn btn-sm">전환·자막 저장</button>
        <button data-act="render" class="btn btn-sm">${sc.clip_path ? "다시 렌더" : "이 씬 렌더"}</button>
      </div>`;
    const saveEdit = async () => {
      const updated = await patchJSON(`/api/stories/${story.id}/scenes/${sc.id}`, {
        transition: card.querySelector('[data-f="transition"]').value,
        caption: card.querySelector('[data-f="caption"]').value,
      });
      story.scenes[story.scenes.findIndex((x) => x.id === sc.id)] = updated;
    };
    card.querySelector('[data-act="save"]').addEventListener("click", () =>
      saveEdit().then(() => renderTimeline()).catch((e) => alert(e.message)));
    card.querySelector('[data-act="up"]').addEventListener("click", () => moveScene(idx, -1));
    card.querySelector('[data-act="down"]').addEventListener("click", () => moveScene(idx, 1));
    card.querySelector('[data-act="edit"]').addEventListener("click", () => {
      sceneIdx = idx;
      goStep("script");
    });
    card.querySelector('[data-act="render"]').addEventListener("click", async () => {
      try {
        await saveEdit();
        await postJSON(`/api/stories/${story.id}/scenes/${sc.id}/render`, {});
        sc.status = "queued";
        renderTimeline();
        ensureScenePoll();
      } catch (err) { alert("렌더 요청 실패: " + err.message); }
    });
    wrap.appendChild(card);
  });
}
async function moveScene(idx, dir) {
  const ids = story.scenes.map((x) => x.id);
  const j = idx + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[idx], ids[j]] = [ids[j], ids[idx]];
  story = await postJSON(`/api/stories/${story.id}/scenes/reorder`, { ordered_scene_ids: ids });
  if (wizStep === "script") renderStoryboard();
  else renderTimeline();
}
function renderFinal() {
  const el = $("final-result");
  if (story.final_video_url) {
    el.innerHTML = `
      <p><b>최종 영상</b> (상태: ${story.status})</p>
      <video src="${story.final_video_url}" controls></video><br/>
      <a href="${story.final_video_url}" target="_blank">다운로드 / 새 탭</a>
      ${story.error_message ? `<p class="warn-note">⚠️ ${story.error_message}</p>` : ""}`;
  } else {
    el.innerHTML = story.error_message ? `<p class="warn-note">⚠️ ${story.error_message}</p>` : "";
  }
}
function setupProduceWiring() {
  $("btn-render-all").addEventListener("click", async () => {
    if (!story) return;
    try {
      const { queued } = await postJSON(`/api/stories/${story.id}/render`, {});
      story.scenes.forEach((s) => { if (queued.includes(s.id)) s.status = "queued"; });
      renderTimeline();
      refreshRenderAllBtn();
      ensureScenePoll();
      toast(`씬 ${queued.length}개를 렌더 대기열에 넣었습니다. 하나씩 끝날 때마다 알려드립니다.`, "info");
    } catch (err) { alert("렌더 요청 실패: " + err.message); }
  });
  $("btn-delete-story").addEventListener("click", async () => {
    if (!story || !confirm(`스토리 #${story.id} 삭제? (씬 클립·최종 영상 포함)`)) return;
    await del(`/api/stories/${story.id}`);
    story = null; saveStoryId(null);
    renderProduce();
  });
  $("btn-assemble").addEventListener("click", async () => {
    if (!story) return;
    try {
      const { job_id } = await postJSON(`/api/stories/${story.id}/assemble`, {});
      pollStoryJob(job_id, "최종 영상 조립 중", () => renderProduce(), "produce-progress");
    } catch (err) { alert("조립 요청 실패: " + err.message); }
  });
  const saveMeta = debounce(async () => {
    if (!story) return;
    await patchJSON(`/api/stories/${story.id}`, {
      intro_text: $("story-intro").value,
      outro_text: $("story-outro").value,
      music_gain: parseFloat($("story-music-gain").value),
    });
  }, 600);
  ["story-intro", "story-outro"].forEach((id) => $(id).addEventListener("input", saveMeta));
  $("story-music-gain").addEventListener("input", () => {
    $("music-gain-val").textContent = $("story-music-gain").value;
    saveMeta();
  });
  $("story-music").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!story || !file) return;
    const fd = new FormData();
    fd.append("audio", file);
    fd.append("gain", $("story-music-gain").value);
    try {
      const r = await fetch(`/api/stories/${story.id}/music`, { method: "POST", body: fd });
      if (!r.ok) throw new Error(await r.text());
      story = await r.json();
      alert("배경 음악 업로드 완료. '최종 영상 만들기'를 누르면 반영됩니다.");
    } catch (err) { alert("음악 업로드 실패: " + err.message); }
  });
}

// =====================================================================
// health + init
// =====================================================================
const STEP_RENDER = {
  cast: renderCast,
  genre: renderGenre,
  template: renderTemplate,
  config: renderConfig,
  casting: renderCasting,
  script: renderScript,
  produce: renderProduce,
};

async function checkHealth() {
  try {
    const h = await getJSON("/api/health");
    $("dot-comfy").className = "dot " + (h.comfyui ? "dot-ok" : "dot-bad");
    $("dot-ollama").className = "dot " + (h.ollama ? "dot-ok" : "dot-bad");
  } catch {
    $("dot-comfy").className = "dot dot-bad";
    $("dot-ollama").className = "dot dot-bad";
  }
}

async function init() {
  setupStepper();
  setupCharUploadForm();
  setupVoiceUploadForm();
  setupVoiceRecorder();
  setupProduceWiring();
  $("cast-next").addEventListener("click", () => goStep("config"));
  $("config-next").addEventListener("click", () => goStep("casting"));
  $("btn-create-story").addEventListener("click", createStory);
  $("btn-new-story").addEventListener("click", newStory);
  $("btn-add-scene").addEventListener("click", () => addScene().catch((e) => alert(e.message)));
  $("scene-next").addEventListener("click", () => advanceScene(true));
  $("scene-skip").addEventListener("click", () => advanceScene(false));
  $("scene-prev").addEventListener("click", () => {
    if (sceneIdx > 0) gotoScene(sceneIdx - 1);
    else goStep("casting");
  });
  $("story-keyword").addEventListener("input", () => { if (wizStep === "casting") renderCastingStatus(); });

  await Promise.all([loadCharacters(), loadBackgrounds(), loadVoices()]);
  await loadImportLists();

  // 스토리 빌더는 화면에서 뺐음 (2026-10-07): 저장된 스토리를 다시 열지 않는다
  const savedId = null;
  if (savedId) {
    try {
      story = await getJSON(`/api/stories/${savedId}`);
      pick.sceneCount = story.scene_count;
      pick.totalSec = story.target_total_sec;
      pick.char1 = state.characters.find((c) => c.id === story.character1_id) || null;
      pick.char2 = state.characters.find((c) => c.id === story.character2_id) || null;
      goStep("script");
    } catch { saveStoryId(null); goStep("genre"); }
  } else {
    goStep("genre");
  }

  checkHealth();
  setInterval(checkHealth, 15000);
}

document.addEventListener("DOMContentLoaded", init);
