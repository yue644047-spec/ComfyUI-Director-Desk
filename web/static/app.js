// 导演台前端逻辑（Tailwind 设计 + 真实后端功能）
var scenes = [];
var curIdx = -1;
var docxB64 = null;
var docxFilename = "";
var docxName = "";
var genAllStop = false;
var needAudio = true;
var subtitleOn = true;
var chainFrames = false;
var bridgeOn = true;
var PAGE_SIZE = 1;
var curPage = 0;
var tagsMap = {};
var currentAssets = [];
var currentOutputs = [];
var character = { name: "", description: "", image: null };
var scene = { name: "", description: "" };
var globalNeg = "";
var videoModel = "MiniMax H3";
var WAN_MODE_STEPS = { "标准": 30, "均衡": 20, "高清": 40, "极速": 10 };
var WAN14_MODE_STEPS = { "标准": 20, "均衡": 20, "高清": 30, "极速": 10 };
var S2V_MODE_STEPS = { "标准": 10, "均衡": 10, "高清": 15, "极速": 6 };

var DEFAULT_SETTINGS = {
  theme: "violet",
  deepseek_key: "",
  deepseek_url: "https://api.deepseek.com",
  deepseek_model: "deepseek-chat",
  qwen_key: "",
  qwen_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  qwen_model: "qwen3-vl-plus",
  style_suffix: "电影质感，真实实拍风格，无字幕无水印。",
  script_prompt: "你是一名资深视频导演与编剧。根据用户主题生成一个 30 秒以内的短视频分镜脚本。必须输出 Markdown 表格，列名固定为：时间 | 画面 | 台词/文案 | 音效/BGM。5-8 个镜头，每镜 3-8 秒，时间连续（如 0-5s、5-10s）。画面描述要具体、有电影感；台词列用“画外音：…”或“字幕：…”。只输出分镜表本身，不要任何解释或多余文字。",
  tag_prompt: "用一句话描述这张画面，然后给出 4-6 个中文标签词（逗号分隔），格式：描述||标签1,标签2"
};
var settings = {};

function modelStepsFor() {
  if (videoModel === "Wan2.2 5B") return WAN_MODE_STEPS;
  if (videoModel === "Wan2.2 14B") return WAN14_MODE_STEPS;
  if (videoModel === "Wan2.2 S2V") return S2V_MODE_STEPS;
  return MODE_STEPS;
}

function loadVideoModel() {
  try { videoModel = localStorage.getItem("director_video_model") || "MiniMax H3"; } catch (e) {}
  if (videoModel !== "MiniMax H3" && videoModel !== "Wan2.2 5B" && videoModel !== "Wan2.2 14B" && videoModel !== "Wan2.2 S2V") videoModel = "MiniMax H3";
  var el = $("videoModelSel");
  if (el) el.value = videoModel;
}

function loadToggles() {
  try {
    needAudio = localStorage.getItem("director_need_audio") !== "0";
    subtitleOn = localStorage.getItem("director_subtitle") !== "0";
    chainFrames = localStorage.getItem("director_chain_frames") === "1";
    bridgeOn = localStorage.getItem("director_bridge") !== "0";
  } catch (e) {}
  if ($("gblNeedAudio")) $("gblNeedAudio").checked = needAudio;
  if ($("gblSubtitle")) $("gblSubtitle").checked = subtitleOn;
  if ($("gblChain")) $("gblChain").checked = chainFrames;
  if ($("gblBridge")) $("gblBridge").checked = bridgeOn;
}

function loadSettings() {
  try { settings = JSON.parse(localStorage.getItem("director_settings") || "{}"); } catch (e) { settings = {}; }
  for (var k in DEFAULT_SETTINGS) { if (settings[k] === undefined) settings[k] = DEFAULT_SETTINGS[k]; }
  applyTheme();
}
function saveSettings() {
  try { localStorage.setItem("director_settings", JSON.stringify(settings)); } catch (e) {}
}
var THEMES = [
  { id: "violet", name: "紫罗兰", c1: "#6d5cff", c2: "#a855f7" },
  { id: "ocean", name: "深海蓝", c1: "#0ea5e9", c2: "#22d3ee" },
  { id: "jade", name: "翡翠绿", c1: "#10b981", c2: "#2dd4bf" },
  { id: "ember", name: "熔岩橙", c1: "#f97316", c2: "#f59e0b" },
  { id: "rose", name: "蔷薇粉", c1: "#ec4899", c2: "#f43f5e" }
];
function applyTheme() {
  var t = settings.theme || "violet";
  if (!THEMES.some(function (x) { return x.id === t; })) t = "violet";
  settings.theme = t;
  document.documentElement.setAttribute("data-theme", t);
  syncThemePicker();
}
function syncThemePicker() {
  var picker = $("themePicker");
  if (!picker) return;
  picker.querySelectorAll("[data-theme]").forEach(function (b) {
    b.setAttribute("data-active", b.getAttribute("data-theme") === settings.theme ? "1" : "0");
  });
}
function fillSettingsForm() {
  $("set_deepseek_key").value = settings.deepseek_key;
  $("set_deepseek_url").value = settings.deepseek_url;
  $("set_deepseek_model").value = settings.deepseek_model;
  $("set_qwen_key").value = settings.qwen_key;
  $("set_qwen_url").value = settings.qwen_url;
  $("set_qwen_model").value = settings.qwen_model;
  $("set_style_suffix").value = settings.style_suffix;
  $("set_script_prompt").value = settings.script_prompt;
  $("set_tag_prompt").value = settings.tag_prompt;
  syncThemePicker();
}
function applySettings() {
  settings.deepseek_key = $("set_deepseek_key").value.trim();
  settings.deepseek_url = $("set_deepseek_url").value.trim();
  settings.deepseek_model = $("set_deepseek_model").value.trim();
  settings.qwen_key = $("set_qwen_key").value.trim();
  settings.qwen_url = $("set_qwen_url").value.trim();
  settings.qwen_model = $("set_qwen_model").value.trim();
  settings.style_suffix = $("set_style_suffix").value;
  settings.script_prompt = $("set_script_prompt").value;
  settings.tag_prompt = $("set_tag_prompt").value;
  saveSettings();
  $("settingsPanel").classList.add("hidden");
  toast("设置已保存", "ok");
}
function resetSettings() {
  settings = {};
  for (var k in DEFAULT_SETTINGS) { settings[k] = DEFAULT_SETTINGS[k]; }
  saveSettings();
  fillSettingsForm();
  applyTheme();
  toast("已恢复默认", "ok");
}

var SHOTS = ["远景", "全景", "中景", "近景", "特写", "大特写"];
var CAMERAS = ["固定镜头", "缓慢推近", "缓慢拉远", "水平摇镜", "垂直摇镜", "跟随镜头", "手持晃动", "环绕运镜"];
var ASPECTS = ["16:9", "9:16", "1:1", "21:9"];
var RESOLUTIONS = ["480p", "720p", "1080p", "2K", "4K"];
var ASPECT_RATIOS = { "16:9": [16, 9], "9:16": [9, 16], "1:1": [1, 1], "21:9": [21, 9] };
var RES_WH = { "480p": 480, "720p": 720, "1080p": 1080, "2K": 1440, "4K": 2160 };
var MODES = ["标准", "均衡", "高清", "极速"];
var MODE_STEPS = { "标准": 25, "均衡": 12, "高清": 32, "极速": 4 };

var SEL = "mt-1 w-full rounded-md border border-white/10 bg-[#111119] px-1.5 py-1.5 text-[11px] text-slate-300 outline-none";
var NUM = "mt-1 w-full rounded-md border border-white/10 bg-[#111119] px-2 py-1.5 text-[11px] text-slate-300 outline-none";
var LBL = "text-[10px] text-slate-500";

function $(id) { return document.getElementById(id); }

async function api(path, body) {
  var r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return await r.json();
}

function toast(msg, type, dur) {
  var t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  t.classList.toggle("border-red-400/40", type === "err");
  t.classList.toggle("border-violet-400/30", type !== "err");
  clearTimeout(t._tm);
  t._tm = setTimeout(function () { t.classList.add("hidden"); }, dur || 2600);
}

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function pad2(n) { return (n < 10 ? "0" : "") + n; }
function fmtTC(sec) {
  var m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return pad2(m) + ":" + pad2(s);
}
function shotStartSec(i) {
  var acc = 0;
  for (var k = 0; k < i; k++) { acc += (scenes[k].seconds || 5) + BRIDGE_SEC; }
  return acc;
}
function opts(arr, cur) {
  var h = "";
  for (var i = 0; i < arr.length; i++) {
    h += '<option value="' + arr[i] + '"' + (arr[i] === cur ? " selected" : "") + '>' + arr[i] + '</option>';
  }
  return h;
}
function clampInt(v, lo, hi, dflt) {
  var n = parseInt(v);
  if (isNaN(n)) n = dflt;
  if (n < lo) n = lo;
  if (n > hi) n = hi;
  return n;
}
function shortTitle(p) {
  var s = String(p || "").trim();
  var m = s.split(/[。，、；;,.：:！？!?]/)[0];
  if (!m) m = s;
  if (m.length > 12) m = m.slice(0, 12) + "…";
  return m || "镜头";
}
function whOf(s) {
  var r = ASPECT_RATIOS[s.aspect] || [16, 9];
  var short = RES_WH[s.resolution] || RES_WH["480p"];
  var snap = videoModel === "Wan2.2 5B" ? 32 : (videoModel === "Wan2.2 14B" || videoModel === "Wan2.2 S2V" ? 16 : 8);
  var w, h;
  if (r[0] >= r[1]) { h = short; w = Math.round(short * r[0] / r[1]); }
  else { w = short; h = Math.round(short * r[1] / r[0]); }
  w = Math.floor(w / snap) * snap;
  h = Math.floor(h / snap) * snap;
  return [Math.max(w, snap), Math.max(h, snap)];
}
function statusInfo(s) {
  if (s.status === "busy") return { text: "生成中", badge: "bg-amber-400/10 text-amber-300", dot: "bg-amber-400 animate-pulse", border: "border-amber-400/25", num: "bg-amber-400/15 text-amber-300" };
  if (s.status === "ok") return { text: "已生成", badge: "bg-emerald-400/10 text-emerald-300", dot: "bg-emerald-400", border: "border-emerald-400/20", num: "bg-emerald-400/15 text-emerald-300" };
  if (s.status === "err") return { text: "失败", badge: "bg-red-400/10 text-red-300", dot: "bg-red-400", border: "border-red-400/25", num: "bg-red-400/15 text-red-300" };
  return { text: "待生成", badge: "bg-slate-500/10 text-slate-500", dot: "bg-slate-500", border: "border-white/[0.08]", num: "bg-white/[0.07] text-slate-400" };
}

function fieldVal(id, field) {
  var el = document.querySelector('[data-id="' + id + '"][data-field="' + field + '"]');
  return el ? el.value : "";
}
var saveTimer = null;

function cleanScene(s) {
  var o = {};
  for (var k in s) { if (k !== "status" && k !== "startedAt") o[k] = s[k]; }
  return o;
}

function scheduleSaveState() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveStateNow, 800);
}

async function saveStateNow() {
  try {
    var r = await api("/api/save_scenes", { scenes: scenes.map(cleanScene) });
    if (r.ok) {
      var d = new Date();
      var el = $("lastSaved");
      if (el) el.textContent = "已保存 " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    }
  } catch (e) {}
}

function syncScene(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  s.prompt = fieldVal(id, "prompt");
  s.shot = fieldVal(id, "shot") || "中景";
  s.camera = fieldVal(id, "camera") || "固定镜头";
  s.aspect = fieldVal(id, "aspect") || "16:9";
  s.resolution = fieldVal(id, "resolution") || "480p";
  s.mode = fieldVal(id, "mode") || "标准";
  s.seconds = clampInt(fieldVal(id, "seconds"), 3, 15, 5);
  s.steps = clampInt(fieldVal(id, "steps"), 1, 50, 25);
  s.seed = String(fieldVal(id, "seed")).trim() !== "" ? parseInt(fieldVal(id, "seed")) : null;
  s.subtitle = fieldVal(id, "subtitle");
  s.negative_prompt = fieldVal(id, "negative_prompt");
  var cf = document.querySelector('[data-id="' + id + '"][data-field="use_character_frame"]');
  s.use_character_frame = cf ? cf.checked : !!s.use_character_frame;
  var am = document.querySelector('[data-id="' + id + '"][data-field="auto_match"]');
  s.auto_match = am ? am.checked : !!s.auto_match;
}

function renderScenes() {
  var box = $("scenes");
  box.innerHTML = "";
  $("scenesEmpty").classList.toggle("hidden", scenes.length > 0);
  var pages = Math.max(1, Math.ceil(scenes.length / PAGE_SIZE));
  if (curPage < 0 || curPage >= pages) curPage = pages - 1;
  var start = curPage * PAGE_SIZE;
  scenes.slice(start, start + PAGE_SIZE).forEach(function (s, i) {
    var div = document.createElement("div");
    div.innerHTML = shotCardHTML(s, start + i);
    var card = div.firstElementChild;
    if (start + i === curIdx) card.classList.add("ring-1", "ring-violet-500/60");
    box.appendChild(card);
  });
  $("shotCount").textContent = scenes.length ? scenes.length + " 个镜头" : "";
  renderPager(pages);
  renderTimeline();
}

function renderTimeline() {
  var box = $("timeline");
  if (!box) return;
  if (!scenes.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  var total = 0;
  scenes.forEach(function (s) { total += (s.seconds || 5); });
  $("timelineTotal").textContent = total + " 秒 · " + scenes.length + " 镜";
  function track(clsOf, titleOf) {
    return scenes.map(function (s, i) {
      var w = Math.max(0.6, ((s.seconds || 5) / total) * 100);
      return '<button data-idx="' + i + '" class="' + clsOf(s) + ' rounded-sm transition hover:brightness-125" style="width:' + w.toFixed(2) + '%" title="' + esc(titleOf(s, i)) + '"></button>';
    }).join("");
  }
  $("trackVideo").innerHTML = track(
    function (s) {
      return s.status === "ok" ? "bg-emerald-400/80" : s.status === "busy" ? "bg-amber-400/80 animate-pulse" : s.status === "err" ? "bg-red-400/80" : "bg-slate-500/60";
    },
    function (s, i) { return "镜头 " + (i + 1) + " · " + fmtTC(shotStartSec(i)) + " · " + (s.seconds || 5) + "s · " + shortTitle(s.prompt); }
  );
  $("trackSub").innerHTML = track(
    function (s) { return (s.subtitle && String(s.subtitle).trim()) ? "bg-amber-400/60" : "bg-white/[0.08]"; },
    function (s) { var t = String(s.subtitle || "").trim(); return t ? "字幕 · " + t : "无字幕"; }
  );
}

function renderPager(pages) {
  var pager = $("scenePager");
  if (!pager) return;
  if (pages > 1) { pager.classList.remove("hidden"); pager.classList.add("flex"); }
  else { pager.classList.add("hidden"); pager.classList.remove("flex"); }
  $("pageInfo").textContent = "第 " + (curPage + 1) + " / " + pages + " 面 · 每面最多 " + PAGE_SIZE + " 镜";
  $("btnPagePrev").disabled = curPage <= 0;
  $("btnPageNext").disabled = curPage >= pages - 1;
}

function shotCardHTML(s, i) {
  var st = statusInfo(s);
  var title = shortTitle(s.prompt);
  var vid = s.output
    ? '<video class="h-full w-full object-cover" src="/output/' + encodeURI(s.output) + '" muted playsinline></video>'
    : s.first_frame
      ? '<img class="h-full w-full object-cover" src="/input/' + encodeURI(s.first_frame) + '" alt="首帧">'
      : '<div class="flex h-full w-full items-center justify-center border border-dashed border-white/10 bg-black/30 text-slate-600"><iconify-icon icon="lucide:image" width="19"></iconify-icon></div>';
  var genBtn, genCls;
  if (s.status === "busy") {
    genBtn = '<iconify-icon class="animate-spin" icon="lucide:loader-circle" width="14"></iconify-icon>生成中…';
    genCls = "bg-amber-400/10 text-amber-200";
  } else if (s.status === "ok") {
    genBtn = '<iconify-icon icon="lucide:rotate-cw" width="14"></iconify-icon>重新生成';
    genCls = "border border-violet-400/30 bg-violet-400/10 text-violet-200 hover:bg-violet-400/20";
  } else {
    genBtn = '<iconify-icon icon="lucide:play" width="14"></iconify-icon>生成';
    genCls = "border border-white/10 bg-white/[0.05] text-slate-300 hover:border-violet-400/40 hover:bg-violet-400/10";
  }
  return '<article class="shot rounded-xl border ' + st.border + ' bg-[#1b1b25] p-3.5 cursor-pointer transition hover:border-white/20" data-id="' + s.id + '" data-index="' + i + '">'
    + '<div class="flex items-center justify-between">'
    + '<div class="flex min-w-0 items-center gap-2.5"><span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ' + st.num + ' text-xs font-bold">' + pad2(i + 1) + '</span><h4 class="truncate text-sm font-semibold">' + esc(title) + '</h4><span class="shrink-0 rounded bg-black/40 px-1.5 py-0.5 font-mono text-[10px] text-slate-500" title="成片中的起始时间">' + fmtTC(shotStartSec(i)) + '</span></div>'
    + '<div class="flex shrink-0 items-center gap-1.5">'
    + '<span id="st_' + s.id + '" class="flex items-center gap-1.5 rounded-full ' + st.badge + ' px-2 py-1 text-[10px] font-semibold"><span class="h-1.5 w-1.5 rounded-full ' + st.dot + '"></span>' + st.text + '</span>'
    + '<button data-action="up" ' + (i === 0 ? "disabled" : "") + ' class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:text-white disabled:opacity-30" title="上移"><iconify-icon icon="lucide:chevron-up" width="14"></iconify-icon></button>'
    + '<button data-action="down" ' + (i === scenes.length - 1 ? "disabled" : "") + ' class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:text-white disabled:opacity-30" title="下移"><iconify-icon icon="lucide:chevron-down" width="14"></iconify-icon></button>'
    + '<button data-action="del" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="删除"><iconify-icon icon="lucide:trash-2" width="14"></iconify-icon></button>'
    + '</div></div>'
    + '<div class="mt-3 grid grid-cols-[88px_1fr] gap-3">'
    + '<div class="relative aspect-video overflow-hidden rounded-lg bg-black" id="vid_' + s.id + '">' + vid + '</div>'
    + '<textarea data-id="' + s.id + '" data-field="prompt" class="h-[62px] resize-none rounded-lg border border-white/10 bg-black/20 p-2 text-xs leading-5 text-slate-300 outline-none focus:border-violet-400/60">' + esc(s.prompt) + '</textarea>'
    + '</div>'
    + '<div class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">景别<select data-id="' + s.id + '" data-field="shot" class="' + SEL + '">' + opts(SHOTS, s.shot) + '</select></label>'
    + '<label class="' + LBL + '">运镜<select data-id="' + s.id + '" data-field="camera" class="' + SEL + '">' + opts(CAMERAS, s.camera) + '</select></label>'
    + '<label class="' + LBL + '">模式<select data-id="' + s.id + '" data-field="mode" class="' + SEL + '">' + opts(MODES, s.mode) + '</select></label>'
    + '<label class="' + LBL + '">比例<select data-id="' + s.id + '" data-field="aspect" class="' + SEL + '">' + opts(ASPECTS, s.aspect) + '</select></label>'
    + '</div>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">清晰度<select data-id="' + s.id + '" data-field="resolution" class="' + SEL + '">' + opts(RESOLUTIONS, s.resolution) + '</select></label>'
    + '<label class="' + LBL + '">时长(秒)<input data-id="' + s.id + '" data-field="seconds" type="number" min="3" max="15" value="' + s.seconds + '" class="' + NUM + '"></label>'
    + '<label class="' + LBL + '">步数<input data-id="' + s.id + '" data-field="steps" type="number" min="1" max="50" value="' + (s.steps || 25) + '" class="' + NUM + '"></label>'
    + '<label class="' + LBL + '">种子<input data-id="' + s.id + '" data-field="seed" type="number" value="' + (s.seed || "") + '" placeholder="随机" class="' + NUM + '"></label>'
    + '</div>'
    + '<input data-id="' + s.id + '" data-field="subtitle" type="text" value="' + esc(s.subtitle) + '" placeholder="字幕（叠加在画面，可选）" class="mt-3 w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-300 outline-none focus:border-violet-400/60">'
    + '<input data-id="' + s.id + '" data-field="negative_prompt" type="text" value="' + esc(s.negative_prompt) + '" placeholder="负向提示词（不想出现的元素，可选）" class="mt-2 w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-400 outline-none focus:border-violet-400/60">'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="frame" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-cyan-400/40 hover:bg-cyan-400/10" title="上传一张图作为该镜头的第一帧，生成时图片+文字一起驱动">'
    + '<iconify-icon icon="lucide:image-plus" width="13"></iconify-icon>' + (s.first_frame ? '更换首帧' : '上传首帧（图+文生视频）') + '</button>'
    + (s.first_frame ? '<button data-action="frame-del" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="移除首帧">✕</button>' : '')
    + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="voice" ' + (!needAudio && videoModel !== "Wan2.2 S2V" ? 'disabled ' : '') + 'class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-emerald-400/40 hover:bg-emerald-400/10 disabled:cursor-not-allowed disabled:opacity-40" title="' + (videoModel === "Wan2.2 S2V" ? "上传说话语音，S2V 由语音驱动人物说话与动作（必需）" : (!needAudio ? "已关闭「需要音频」，配音不会合入" : "上传配音/音乐文件，生成时合入视频音轨（Wan2.2 无原生音频，必须用此方式配音）")) + '"><iconify-icon icon="lucide:mic" width="13"></iconify-icon>' + (s.voice ? '更换配音' : (videoModel === "Wan2.2 S2V" ? '上传说话语音（S2V 必需）' : '上传配音（画外音/音乐）')) + '</button>'
    + (s.voice ? '<button data-action="voice-del" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="移除配音">✕</button>' : '')
    + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="guide" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-amber-400/40 hover:bg-amber-400/10" title="多图参考（H3）：参考图抠主体后作主体定义（<Subject>），不占首帧"><iconify-icon icon="lucide:images" width="13"></iconify-icon>' + (s.guides && s.guides.length ? '参考图 ' + s.guides.length + ' 张 — 再加一张' : '多图参考（H3）') + '</button>'
    + '</div>'
    + (s.guides && s.guides.length
        ? '<div class="mt-2 flex items-center gap-1.5 overflow-x-auto">' + s.guides.map(function (g, gi) {
            return '<div class="relative h-12 w-20 shrink-0 overflow-hidden rounded-md border border-white/10 bg-black/40">'
              + '<img class="h-full w-full object-cover" src="/input/' + encodeURI(g) + '">'
              + '<button data-action="guide-del" data-gi="' + gi + '" class="absolute right-0 top-0 flex h-4 w-4 items-center justify-center rounded-bl bg-black/70 text-[9px] text-white">✕</button></div>';
          }).join("") + '</div>'
        : '')
    + '<label class="mt-2 flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer"><input data-id="' + s.id + '" data-field="auto_match" type="checkbox" class="h-3.5 w-3.5 accent-cyan-400"' + (s.auto_match ? " checked" : "") + '>自动匹配素材图（抠主体后作参考，不占首帧；仅 H3，需 ref2va+BiRefNet 模型）</label>'
    + (character.image ? '<label class="mt-2 flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer"><input data-id="' + s.id + '" data-field="use_character_frame" type="checkbox" class="h-3.5 w-3.5 accent-pink-400"' + (s.use_character_frame ? " checked" : "") + '>角色出场（用角色参考图做首帧）</label>' : '')
    + '<button data-action="gen" id="gen_' + s.id + '" ' + (s.status === "busy" ? "disabled" : "") + ' class="mt-3 flex w-full items-center justify-center gap-2 rounded-lg ' + genCls + ' py-2 text-xs font-semibold transition disabled:cursor-not-allowed">' + genBtn + '</button>'
    + '</article>';
}

function renderMonitor() {
  var mv = $("monitorVideo");
  var me = $("monitorEmpty");
  var s = scenes[curIdx];
  if (!s) {
    mv.pause(); mv.classList.add("hidden"); mv.removeAttribute("src"); mv.load();
    me.classList.remove("hidden");
    me.innerHTML = '<iconify-icon icon="lucide:clapperboard" width="42"></iconify-icon><p id="monitorEmptyText" class="px-6 text-center text-sm">解析脚本后，点选分镜在此预览</p>';
    $("monitorShot").textContent = "—";
    $("monitorSpec").textContent = "—";
    $("monitorRes").textContent = "—";
    return;
  }
  if (s.output) {
    mv.src = "/output/" + encodeURI(s.output);
    mv.classList.remove("hidden");
    me.classList.add("hidden");
    mv.play().catch(function () {});
  } else {
    mv.pause(); mv.classList.add("hidden"); mv.removeAttribute("src"); mv.load();
    me.classList.remove("hidden");
    if (s.first_frame) {
      me.innerHTML = '<img class="h-full w-full object-contain" src="/input/' + encodeURI(s.first_frame) + '" alt="首帧">';
    } else {
      me.innerHTML = '<iconify-icon icon="lucide:clapperboard" width="42"></iconify-icon><p id="monitorEmptyText" class="px-6 text-center text-sm">镜头 ' + (curIdx + 1) + ' 尚未生成</p>';
    }
  }
  $("monitorShot").textContent = "镜头 " + (curIdx + 1) + " · " + (s.shot || "中景");
  $("monitorSpec").textContent = (s.camera || "固定镜头") + " · " + s.seconds + " 秒";
  var w = whOf(s);
  $("monitorRes").textContent = w[0] + " × " + w[1];
}

function syncProgress() {
  var total = scenes.length;
  var done = scenes.filter(function (s) { return s.output; }).length;
  var pct = total ? Math.round(done / total * 100) : 0;
  $("progressBar").style.width = pct + "%";
  $("progressText").textContent = done + " / " + total;
  $("btnConcat").disabled = !(total > 0 && done === total);
}

function selectScene(idx) {
  if (idx < 0 || idx >= scenes.length) return;
  curIdx = idx;
  var page = Math.floor(idx / PAGE_SIZE);
  if (page !== curPage) { curPage = page; renderScenes(); }
  document.querySelectorAll(".shot").forEach(function (el) {
    var on = parseInt(el.getAttribute("data-index")) === curIdx;
    el.classList.toggle("ring-1", on);
    el.classList.toggle("ring-violet-500/60", on);
  });
  renderMonitor();
}

function prevShot() { if (scenes.length) selectScene((curIdx - 1 + scenes.length) % scenes.length); }
function nextShot() { if (scenes.length) selectScene((curIdx + 1) % scenes.length); }

function recoverStuckBusy() {
  var now = Date.now();
  scenes.forEach(function (s) {
    if (s.status === "busy" && s.startedAt && now - s.startedAt > 120000) {
      s.status = "pending";
      s.startedAt = null;
      setSceneStatus(s.id);
    }
  });
}

function setSceneStatus(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  var st = statusInfo(s);
  var badge = $("st_" + id);
  var btn = $("gen_" + id);
  var vid = $("vid_" + id);
  if (badge) {
    badge.className = "flex items-center gap-1.5 rounded-full " + st.badge + " px-2 py-1 text-[10px] font-semibold";
    badge.innerHTML = '<span class="h-1.5 w-1.5 rounded-full ' + st.dot + '"></span>' + st.text;
  }
  if (vid && s.output) {
    vid.innerHTML = '<video class="h-full w-full object-cover" src="/output/' + encodeURI(s.output) + '" muted playsinline></video>';
  } else if (vid && s.first_frame) {
    vid.innerHTML = '<img class="h-full w-full object-cover" src="/input/' + encodeURI(s.first_frame) + '" alt="首帧">';
  }
  var anyBusy = scenes.some(function (x) { return x.status === "busy"; });
  var bc = $("btnCancel");
  if (bc) { bc.classList.toggle("hidden", !anyBusy); bc.classList.toggle("flex", anyBusy); }
  syncProgress();
  if (scenes[curIdx] && scenes[curIdx].id === id) renderMonitor();
}

function addScene() {
  var max = 0;
  scenes.forEach(function (s) {
    var m = /^scene(\d+)$/.exec(s.id || "");
    if (m) max = Math.max(max, parseInt(m[1]));
  });
  var s = {
    id: "scene" + (max + 1),
    seconds: 5,
    prompt: "",
    subtitle: "",
    shot: "中景", camera: "固定镜头", aspect: "16:9", resolution: "480p",
    mode: "标准", steps: modelStepsFor()["标准"], seed: null, negative_prompt: "",
    first_frame: null, use_character_frame: false, output: null,
  };
  scenes.push(s);
  curIdx = scenes.length - 1;
  curPage = Math.floor(curIdx / PAGE_SIZE);
  renderScenes(); renderMonitor(); syncProgress();
  scheduleSaveState();
  toast("已新增镜头 " + (max + 1), "ok");
}

async function optimize() {
  var text = $("script").value.trim();
  if (!docxB64 && !text) { toast("请先粘贴脚本或上传文档", "err"); return; }
  var useAI = $("optAI") ? $("optAI").checked : true;
  var btn = $("btnOptimize");
  btn.disabled = true;
  btn.innerHTML = '<iconify-icon class="animate-spin" icon="lucide:loader-circle" width="16"></iconify-icon>' + (useAI ? "本地 AI 解析中…" : "解析中…");
  try {
    var append = $("optAppend") ? $("optAppend").checked : false;
    var payload = docxB64 ? { docx_b64: docxB64, filename: docxFilename, append: append, ai: useAI } : { text: text, append: append, ai: useAI };
    var r = await api("/api/optimize", payload);
    if (!r.ok) { toast("解析失败：" + (r.error || "未知错误"), "err"); return; }
    var newScenes = (r.scenes || []).map(function (s) { s.output = s.output || null; s.status = "pending"; return s; });
    scenes = append ? scenes.concat(newScenes) : newScenes;
    docxB64 = null; docxFilename = ""; docxName = "";
    $("docxName").textContent = "";
    $("btnReset").classList.remove("hidden");
    $("btnReset").classList.add("flex");
    curIdx = scenes.length ? (append ? scenes.length - newScenes.length : 0) : -1;
    curPage = Math.floor(curIdx / PAGE_SIZE);
    $("lastSaved").textContent = (append ? "已追加 " : "已解析 ") + newScenes.length + " 个镜头";
    renderScenes(); renderMonitor(); syncProgress();
    $("importPanel").classList.add("hidden");
    var byAI = r.engine === "ai";
    toast((append ? "已追加 " : "解析完成，共 ") + newScenes.length + " 个镜头（" + (byAI ? "本地 AI 分镜" : "规则拆分") + "）", "ok");
  } catch (e) {
    toast("解析请求失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<iconify-icon icon="lucide:sparkles" width="16"></iconify-icon>解析分镜';
  }
}

async function generateScene(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  if (s.status === "busy") { toast("该镜头正在生成中，请稍候；若长时间无变化请刷新页面", "err"); return; }
  if (videoModel === "Wan2.2 S2V" && !s.voice) { toast("S2V 需要说话语音：请先在镜头卡片上传配音（语音驱动画面与动作）", "err"); return; }
  syncScene(id);
  s.status = "busy";
  s.startedAt = Date.now();
  setSceneStatus(id);
  if (!window._hintShown) {
    window._hintShown = true;
    toast("已提交生成。速度档位：极速(4步)最快 · 均衡(12步)快约一倍、画质接近标准 · 标准(25步)最稳。5 秒视频按档位约 2~20 分钟，GPU 在跑就是在动", "ok", 12000);
  }
  try {
    var r = await api("/api/generate", {
      scene: { id: s.id, prompt: s.prompt, seconds: s.seconds, subtitle: s.subtitle, shot: s.shot, camera: s.camera, aspect: s.aspect, resolution: s.resolution, mode: s.mode, steps: s.steps, seed: s.seed, negative_prompt: s.negative_prompt || "", first_frame: s.first_frame || null, use_character_frame: !!s.use_character_frame, character: character, scene: scene, global_negative_prompt: globalNeg, model: videoModel, voice: s.voice || null, guides: s.guides || [], auto_match: !!s.auto_match, style_suffix: settings.style_suffix, need_audio: needAudio, chain_frames: chainFrames }
    });
    if (s.cancelled) {
      s.cancelled = false;
      s.status = "pending";
      toast("镜头 " + s.id + " 已取消（可稍后重新生成）", "ok");
    } else if (r.ok) {
      s.output = r.file; s.status = "ok";
      if (r.matched && r.matched.name) {
        toast("已自动匹配素材图：" + r.matched.name + "（已抠主体作参考）", "ok", 6000);
      } else if (r.matched && r.matched.error) {
        toast("自动匹配失败：" + r.matched.error, "err");
      } else if (r.chained) {
        toast("镜头 " + s.id + " 生成完成：已用上一镜尾帧作首帧续接", "ok");
      } else if (r.chain_skip) {
        toast("镜头 " + s.id + " 生成完成：" + r.chain_skip + "，未续接", "err", 8000);
      } else if (s.auto_match) {
        toast("未找到匹配的素材图，已按纯文字生成", "ok");
      } else {
        toast("镜头 " + s.id + " 生成完成", "ok");
      }
    }
    else { s.status = "err"; toast("镜头 " + s.id + " 生成失败：" + r.error, "err"); }
  } catch (e) {
    if (s.cancelled) {
      s.cancelled = false;
      s.status = "pending";
      toast("镜头 " + s.id + " 已取消（可稍后重新生成）", "ok");
    } else {
      s.status = "err";
      toast("镜头 " + s.id + " 请求失败：" + e.message, "err");
    }
  }
  s.startedAt = null;
  setSceneStatus(id);
}

function uploadFrame(id) {
  var inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/png,image/jpeg,image/webp";
  inp.onchange = function () {
    var f = inp.files && inp.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = async function () {
      var b64 = String(reader.result).split(",")[1] || "";
      var ext = (f.name.split(".").pop() || "png").toLowerCase();
      try {
        var r = await api("/api/upload_frame", { scene_id: id, data_b64: b64, ext: ext });
        if (!r.ok) { toast("上传失败：" + (r.error || ""), "err"); return; }
        var s = scenes.find(function (x) { return x.id === id; });
        if (s) { s.first_frame = r.file; renderScenes(); if (scenes[curIdx] && scenes[curIdx].id === id) renderMonitor(); }
        toast("首帧已就绪，生成时将按「图片+文字」出视频", "ok");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    };
    reader.readAsDataURL(f);
  };
  inp.click();
}

function clearFrame(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  s.first_frame = null;
  renderScenes();
  if (scenes[curIdx] && scenes[curIdx].id === id) renderMonitor();
  scheduleSaveState();
  toast("已移除首帧，恢复纯文字生成", "ok");
}

function syncGlobalButton() {
  var b = $("btnGlobal");
  if (!b) return;
  var active = !!(character.name || character.description || scene.name || scene.description);
  b.title = "全局设置" + (character.name ? " · 角色：" + character.name : "") + (scene.name ? " · 场景：" + scene.name : "");
  b.classList.toggle("bg-cyan-400/15", active);
  b.classList.toggle("text-cyan-300", active);
}

function fillGlobalForm() {
  if ($("gblNeedAudio")) $("gblNeedAudio").checked = needAudio;
  if ($("gblSubtitle")) $("gblSubtitle").checked = subtitleOn;
  if ($("gblChain")) $("gblChain").checked = chainFrames;
  if ($("gblBridge")) $("gblBridge").checked = bridgeOn;
  $("charName").value = character.name || "";
  $("charDesc").value = character.description || "";
  $("sceneName").value = scene.name || "";
  $("sceneDesc").value = scene.description || "";
  $("gblNegPrompt").value = globalNeg || "";
  renderCharImage();
}

function renderCharImage() {
  $("charImageEmpty").classList.toggle("hidden", !!character.image);
  var img = $("charImagePreview");
  if (character.image) { img.src = "/input/" + encodeURI(character.image); img.classList.remove("hidden"); }
  else { img.classList.add("hidden"); img.removeAttribute("src"); }
}

async function saveGlobal() {
  needAudio = $("gblNeedAudio").checked;
  subtitleOn = $("gblSubtitle").checked;
  chainFrames = $("gblChain").checked;
  bridgeOn = $("gblBridge").checked;
  try {
    localStorage.setItem("director_need_audio", needAudio ? "1" : "0");
    localStorage.setItem("director_subtitle", subtitleOn ? "1" : "0");
    localStorage.setItem("director_chain_frames", chainFrames ? "1" : "0");
    localStorage.setItem("director_bridge", bridgeOn ? "1" : "0");
  } catch (e) {}
  character.name = $("charName").value.trim();
  character.description = $("charDesc").value.trim();
  scene.name = $("sceneName").value.trim();
  scene.description = $("sceneDesc").value.trim();
  globalNeg = $("gblNegPrompt").value.trim();
  try {
    var r1 = await api("/api/character", { name: character.name, description: character.description, image: character.image });
    var r2 = await api("/api/scene", { name: scene.name, description: scene.description });
    var r3 = await api("/api/global", { negative_prompt: globalNeg });
    if (r1.ok && r2.ok && r3.ok) { $("globalPanel").classList.add("hidden"); syncGlobalButton(); renderScenes(); toast("全局设置已保存", "ok"); }
    else toast("保存失败：" + ((r1.error || r2.error || r3.error) || ""), "err");
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

function uploadCharImage() {
  var inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/png,image/jpeg,image/webp";
  inp.onchange = function () {
    var f = inp.files && inp.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = async function () {
      var b64 = String(reader.result).split(",")[1] || "";
      var ext = (f.name.split(".").pop() || "png").toLowerCase();
      try {
        var r = await api("/api/upload_frame", { scene_id: "character", data_b64: b64, ext: ext });
        if (!r.ok) { toast("上传失败：" + (r.error || ""), "err"); return; }
        character.image = r.file;
        await api("/api/character", { name: character.name, description: character.description, image: character.image });
        renderCharImage();
        renderScenes();
        toast("参考图已保存，可在镜头里勾选「角色出场」", "ok");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    };
    reader.readAsDataURL(f);
  };
  inp.click();
}

function delCharImage() {
  character.image = null;
  api("/api/character", { name: character.name, description: character.description, image: null });
  renderCharImage();
  renderScenes();
  toast("已移除参考图", "ok");
}

function uploadVoice(id) {
  var inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac";
  inp.onchange = function () {
    var f = inp.files && inp.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = async function () {
      var b64 = String(reader.result).split(",")[1] || "";
      var ext = (f.name.split(".").pop() || "mp3").toLowerCase();
      try {
        var r = await api("/api/upload_voice", { scene_id: id, data_b64: b64, ext: ext });
        if (!r.ok) { toast("上传失败：" + (r.error || ""), "err"); return; }
        var s = scenes.find(function (x) { return x.id === id; });
        if (s) { s.voice = r.file; renderScenes(); }
        toast("配音已就绪，生成时将合入视频音轨", "ok");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    };
    reader.readAsDataURL(f);
  };
  inp.click();
}

function clearVoice(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  s.voice = null;
  renderScenes();
  scheduleSaveState();
  toast("已移除配音", "ok");
}

function uploadGuide(id) {
  var inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/png,image/jpeg,image/webp";
  inp.onchange = function () {
    var f = inp.files && inp.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = async function () {
      var b64 = String(reader.result).split(",")[1] || "";
      var ext = (f.name.split(".").pop() || "png").toLowerCase();
      try {
        var r = await api("/api/upload_guide", { scene_id: id, data_b64: b64, ext: ext });
        if (!r.ok) { toast("上传失败：" + (r.error || ""), "err"); return; }
        var s = scenes.find(function (x) { return x.id === id; });
        if (s) { s.guides = s.guides || []; s.guides.push(r.file); renderScenes(); }
        toast("参考图已添加，共 " + (s ? s.guides.length : 0) + " 张（仅 H3 生效）", "ok");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    };
    reader.readAsDataURL(f);
  };
  inp.click();
}

function removeGuide(id, gi) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s || !s.guides || gi < 0 || gi >= s.guides.length) return;
  var f = s.guides[gi];
  s.guides.splice(gi, 1);
  renderScenes();
  scheduleSaveState();
  api("/api/remove_guide", { scene_id: id, file: f });
}

var genAllPaused = false;

function waitResume() {
  return new Promise(function (resolve) {
    var tm = setInterval(function () {
      if (!genAllPaused) { clearInterval(tm); resolve(); }
    }, 500);
  });
}

function setPauseButton() {
  var b = $("btnPause");
  if (!b) return;
  if (genAllPaused) {
    b.innerHTML = '<iconify-icon icon="lucide:play" width="16"></iconify-icon>继续';
  } else {
    b.innerHTML = '<iconify-icon icon="lucide:pause" width="16"></iconify-icon>暂停';
  }
}

function cancelCurrent() {
  var busy = scenes.filter(function (s) { return s.status === "busy"; });
  if (!busy.length) { toast("没有正在生成的任务", "ok"); return; }
  busy.forEach(function (s) { s.cancelled = true; });
  try {
    fetch("http://127.0.0.1:8188/interrupt", { method: "POST", mode: "no-cors" });
  } catch (e) {}
  toast("已发送取消指令，当前任务将被中断", "ok");
}

async function generateAll() {
  var pending = scenes.filter(function (s) { return !s.output; });
  if (!pending.length) { toast("没有待生成的镜头", "ok"); return; }
  $("btnGenAll").disabled = true;
  $("btnStop").classList.remove("hidden");
  $("btnStop").classList.add("flex");
  $("btnPause").classList.remove("hidden");
  $("btnPause").classList.add("flex");
  genAllStop = false;
  genAllPaused = false;
  setPauseButton();
  try {
    for (var i = 0; i < pending.length; i++) {
      if (genAllStop) break;
      while (genAllPaused) { await waitResume(); }
      await generateScene(pending[i].id);
    }
    toast(genAllStop ? "已停止" : "批量生成结束", "ok");
  } finally {
    $("btnGenAll").disabled = false;
    $("btnStop").classList.add("hidden");
    $("btnStop").classList.remove("flex");
    $("btnPause").classList.add("hidden");
    $("btnPause").classList.remove("flex");
    genAllStop = false;
    genAllPaused = false;
  }
}

var BRIDGE_SEC = 73 / 24;

async function concat() {
  if (!scenes.length || !scenes.every(function (s) { return s.output; })) { toast("还有镜头未生成", "err"); return; }
  var clips = scenes.map(function (s) { return s.output; });
  var subtitles = [];
  var acc = 0;
  scenes.forEach(function (s, i) {
    if (subtitleOn && s.subtitle && String(s.subtitle).trim()) {
      subtitles.push({ text: String(s.subtitle).trim(), start: acc, end: acc + s.seconds, fontsize: 44, position: "center" });
    }
    acc += s.seconds + (bridgeOn && i < scenes.length - 1 ? BRIDGE_SEC : 0);
  });
  var btn = $("btnConcat");
  btn.disabled = true;
  function done(label) {
    btn.disabled = false;
    btn.innerHTML = '<iconify-icon icon="lucide:film" width="16"></iconify-icon>' + label;
  }
  try {
    var start = await api("/api/concat", { clips: clips, subtitles: subtitles, bridge: bridgeOn, subtitle: subtitleOn, need_audio: needAudio });
    if (!start.ok) { toast("合成失败：" + (start.error || "未知错误"), "err"); done("合成成片"); return; }
    var timer = setInterval(async function () {
      var d;
      try { d = await api("/api/concat_status"); } catch (e) { return; }
      var st = (d && d.status) || {};
      if (st.running) {
        var total = st.total || 1;
        var pct = st.stage === "concat" ? 100 : Math.min(95, Math.round((st.done || 0) / total * 95));
        btn.innerHTML = '<iconify-icon class="animate-spin" icon="lucide:loader-circle" width="16"></iconify-icon>' + (st.current || st.stage) + ' ' + pct + '%';
      } else if (st.stage === "done" && st.result) {
        clearInterval(timer);
        var meta = scenes.length + " 个镜头 · " + acc.toFixed(1) + " 秒" + (bridgeOn ? "（含 " + (scenes.length - 1) + " 段补帧过渡）" : "（无补帧过渡）");
        $("finalModalMeta").textContent = meta;
        $("finalModalVideo").src = "/output/" + st.result;
        $("finalModalDownload").href = "/output/" + st.result;
        $("finalModal").classList.remove("hidden");
        $("finalModalVideo").play().catch(function () {});
        toast("合成完成，成片已弹出", "ok");
        done("合成成片");
      } else if (st.stage === "error") {
        clearInterval(timer);
        toast("合成失败：" + (st.error || "未知错误"), "err");
        done("合成成片");
      } else {
        clearInterval(timer);
        toast("合成已中断（服务可能已重启），请重新点击「合成成片」", "err");
        done("合成成片");
      }
    }, 3000);
  } catch (e) {
    toast("合成请求失败：" + e.message, "err");
    done("合成成片");
  }
}

function moveScene(id, dir) {
  var i = scenes.findIndex(function (x) { return x.id === id; });
  var j = i + dir;
  if (i < 0 || j < 0 || j >= scenes.length) return;
  var tmp = scenes[i]; scenes[i] = scenes[j]; scenes[j] = tmp;
  if (curIdx === i) curIdx = j;
  else if (curIdx === j) curIdx = i;
  renderScenes(); renderMonitor(); syncProgress();
  scheduleSaveState();
}

function removeScene(id) {
  var i = scenes.findIndex(function (x) { return x.id === id; });
  scenes = scenes.filter(function (x) { return x.id !== id; });
  if (curIdx === i) curIdx = Math.min(curIdx, scenes.length - 1);
  else if (curIdx > i) curIdx -= 1;
  if (curIdx < 0) curIdx = scenes.length ? 0 : -1;
  renderScenes(); renderMonitor(); syncProgress();
  scheduleSaveState();
}

function reset() {
  scenes = []; curIdx = -1; curPage = 0; docxB64 = null; docxFilename = ""; docxName = "";
  $("script").value = "";
  $("docxName").textContent = "";
  $("charCount").textContent = "0 字";
  $("btnReset").classList.add("hidden");
  $("btnReset").classList.remove("flex");
  $("lastSaved").textContent = "尚未解析脚本";
  renderScenes(); renderMonitor(); syncProgress();
}

// ---- 素材库 ----
function isVideo(p) { return /.(mp4|webm|mov|mkv|avi)$/i.test(p); }
function fmtSize(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}
function fmtTime(t) {
  var d = new Date(t * 1000);
  function p(x) { return (x < 10 ? "0" : "") + x; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

async function loadAssets() {
  try {
    var r = await api("/api/assets");
    if (!r.ok) { toast("读取素材库失败：" + (r.error || ""), "err"); return; }
    renderAssets(r.assets || []);
  } catch (e) { toast("读取素材库失败：" + e.message, "err"); }
}

function renderAssets(list) {
  currentAssets = list || [];
  var box = $("assets");
  box.innerHTML = "";
  $("assetsEmpty").classList.toggle("hidden", list.length > 0);
  $("assetCount").textContent = list.length ? list.length + " 个" : "";
  list.forEach(function (a) {
    var div = document.createElement("div");
    div.innerHTML = assetCardHTML(a);
    box.appendChild(div.firstElementChild);
  });
}

function assetCardHTML(a) {
  var media = isVideo(a.path)
    ? '<video class="h-full w-full object-cover" src="/assets/' + encodeURI(a.path) + '" preload="metadata" muted playsinline></video>'
    : '<img class="h-full w-full object-cover" src="/assets/' + encodeURI(a.path) + '" loading="lazy">';
  return '<div class="asset-card overflow-hidden rounded-xl border border-white/[0.08] bg-[#1b1b25] transition hover:border-white/20" data-path="' + esc(a.path) + '">'
    + '<div class="relative aspect-video bg-black">' + media + '<div class="pointer-events-none absolute inset-x-0 bottom-0 flex h-8 items-end bg-gradient-to-t from-black/80 to-transparent p-1.5 text-[10px] text-white/70">' + fmtSize(a.size) + '</div></div>'
    + '<div class="p-3">'
    + '<div class="truncate text-xs font-semibold" title="' + esc(a.path) + '">' + esc(a.name) + '</div>'
    + '<div class="mt-0.5 text-[10px] text-slate-500">' + fmtTime(a.mtime) + '</div>'
    + '<div class="mt-1 truncate text-[10px] text-violet-300/80" title="' + esc(tagsMap[a.path] || "") + '">' + esc(tagsMap[a.path] ? "🏷 " + tagsMap[a.path] : "") + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="asset-tag" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-violet-400/40 hover:text-violet-300" title="AI 打标签"><iconify-icon icon="lucide:brain" width="13"></iconify-icon></button>'
    + '<button data-action="asset-tag-manual" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-amber-400/40 hover:text-amber-300" title="手动打标签（离线可用）"><iconify-icon icon="lucide:tag" width="13"></iconify-icon></button>'
    + '<a class="flex flex-1 items-center justify-center gap-1 rounded-lg border border-white/10 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:bg-white/[0.06]" href="/assets/' + encodeURI(a.path) + '" download><iconify-icon icon="lucide:download" width="13"></iconify-icon>下载</a>'
    + '<button data-action="asset-del" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="删除"><iconify-icon icon="lucide:trash-2" width="13"></iconify-icon></button>'
    + '</div></div></div>';
}

async function deleteAsset(path) {
  if (!confirm("确定删除素材 " + path + " ？")) return;
  try {
    var r = await api("/api/assets/delete", { path: path });
    if (r.ok) { toast("已删除", "ok"); loadAssets(); }
    else { toast("删除失败：" + (r.error || ""), "err"); }
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

async function loadOutputs() {
  try {
    var r = await api("/api/outputs");
    if (!r.ok) { toast("读取保存区失败：" + (r.error || ""), "err"); return; }
    renderOutputs(r.outputs || []);
  } catch (e) { toast("读取保存区失败：" + e.message, "err"); }
}

function renderOutputs(list) {
  currentOutputs = list || [];
  var box = $("saved");
  box.innerHTML = "";
  $("savedEmpty").classList.toggle("hidden", list.length > 0);
  $("savedCount").textContent = list.length ? list.length + " 个" : "";
  list.forEach(function (o) {
    var div = document.createElement("div");
    div.innerHTML = outputCardHTML(o);
    box.appendChild(div.firstElementChild);
  });
}

function outputCardHTML(o) {
  return '<div class="saved-card overflow-hidden rounded-xl border border-white/[0.08] bg-[#1b1b25] transition hover:border-white/20" data-path="' + esc(o.path) + '">'
    + '<div class="relative aspect-video bg-black">'
    + '<video class="h-full w-full object-cover" src="/output/' + encodeURI(o.path) + '" preload="metadata" muted playsinline controls></video>'
    + '<div class="pointer-events-none absolute inset-x-0 bottom-0 flex h-8 items-end bg-gradient-to-t from-black/80 to-transparent p-1.5 text-[10px] text-white/70">' + fmtSize(o.size) + '</div></div>'
    + '<div class="p-3">'
    + '<div class="truncate text-xs font-semibold" title="' + esc(o.path) + '">' + esc(o.name) + '</div>'
    + '<div class="mt-0.5 text-[10px] text-slate-500">' + fmtTime(o.mtime) + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<a class="flex flex-1 items-center justify-center gap-1 rounded-lg border border-white/10 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:bg-white/[0.06]" href="/output/' + encodeURI(o.path) + '" download><iconify-icon icon="lucide:download" width="13"></iconify-icon>下载</a>'
    + '<button data-action="saved-del" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="删除"><iconify-icon icon="lucide:trash-2" width="13"></iconify-icon></button>'
    + '</div></div></div>';
}

async function deleteOutput(path) {
  if (!confirm("确定删除 " + path + " ？")) return;
  try {
    var r = await api("/api/outputs/delete", { path: path });
    if (r.ok) { toast("已删除", "ok"); loadOutputs(); }
    else { toast("删除失败：" + (r.error || ""), "err"); }
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

async function loadAssetsTags() {
  try {
    var r = await api("/api/assets/tags");
    if (r.ok) { tagsMap = r.tags || {}; renderAssets(currentAssets); }
  } catch (e) {}
}

async function searchAssets(q) {
  try {
    var r = await api("/api/assets/search?q=" + encodeURIComponent(q || ""));
    if (r.ok) renderAssets(r.assets || []);
    else toast("搜索失败：" + (r.error || ""), "err");
  } catch (e) { toast("搜索失败：" + e.message, "err"); }
}

async function tagAsset(path) {
  toast("素材大脑打标签中…", "ok");
  try {
    var r = await api("/api/assets/tag", { path: path, api_key: settings.qwen_key, base_url: settings.qwen_url, model: settings.qwen_model, prompt: settings.tag_prompt });
    if (r.ok) { toast("已打标签", "ok"); loadAssetsTags(); }
    else toast("打标签失败：" + (r.error || ""), "err");
  } catch (e) { toast("打标签失败：" + e.message, "err"); }
}

async function manualTagAsset(path) {
  var cur = tagsMap[path] || "";
  var v = prompt("给素材打标签（逗号分隔，用于自动匹配检索）：", cur);
  if (v === null) return;
  try {
    var r = await api("/api/assets/tag_manual", { path: path, tags: v.trim() });
    if (r.ok) { toast("标签已保存", "ok"); loadAssetsTags(); }
    else toast("保存失败：" + (r.error || ""), "err");
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

async function uploadAsset() {
  var input = $("assetFileInput");
  if (!input) { toast("上传控件未就绪", "err"); return; }
  input.value = "";
  input.click();
  input.onchange = async function () {
    var files = input.files;
    if (!files || !files.length) return;
    var okCount = 0;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      var ext = (f.name.split(".").pop() || "png").toLowerCase();
      if (["png", "jpg", "jpeg", "webp"].indexOf(ext) < 0) { toast("跳过不支持的格式：" + f.name, "err"); continue; }
      var b64 = await new Promise(function (resolve, reject) {
        var fr = new FileReader();
        fr.onload = function () { resolve(String(fr.result).split(",")[1]); };
        fr.onerror = function () { reject(new Error("读取失败")); };
        fr.readAsDataURL(f);
      });
      try {
        var r = await api("/api/assets/upload", { data_b64: b64, filename: f.name, ext: ext });
        if (r.ok) { okCount++; toast("素材已入库：" + f.name, "ok"); }
        else toast("上传失败：" + (r.error || ""), "err");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    }
    if (okCount) loadAssets();
  };
}

async function aiScript() {
  var topic = ($("scriptTopic") ? $("scriptTopic").value : "").trim();
  if (!topic) { toast("请先输入一句话主题/创意", "err"); return; }
  var btn = $("btnScriptAI");
  btn.disabled = true;
  btn.innerHTML = '<iconify-icon class="animate-spin" icon="lucide:loader-circle" width="16"></iconify-icon>剧本大脑生成中…';
  try {
    var r = await api("/api/script_ai", { topic: topic, api_key: settings.deepseek_key, base_url: settings.deepseek_url, model: settings.deepseek_model, sys_prompt: settings.script_prompt });
    if (!r.ok) { toast("剧本大脑失败：" + (r.error || ""), "err"); return; }
    $("script").value = r.script;
    $("charCount").textContent = r.script.length + " 字";
    toast("剧本已生成，点「解析分镜」", "ok");
  } catch (e) { toast("剧本大脑请求失败：" + e.message, "err"); }
  finally {
    btn.disabled = false;
    btn.innerHTML = '<iconify-icon icon="lucide:brain" width="16"></iconify-icon>AI 写剧本';
  }
}

async function restoreState() {
  try {
    var r = await api("/api/state");
    if (r.ok) {
      character = r.character || { name: "", description: "", image: null };
      scene = r.scene || { name: "", description: "" };
      globalNeg = r.global_negative_prompt || "";
      if (r.project && r.project.name) {
        var pn = $("projectName");
        if (pn) { pn.textContent = r.project.name; pn.classList.remove("hidden"); }
        document.title = r.project.name + " · 导演台";
      }
      syncGlobalButton();
    }
    if (r.ok && r.scenes && r.scenes.length) {
      scenes = r.scenes.map(function (s) {
        s.shot = s.shot || "中景";
        s.camera = s.camera || "固定镜头";
        s.aspect = s.aspect || "16:9";
        s.resolution = s.resolution || "480p";
        s.mode = s.mode || "标准";
        s.negative_prompt = s.negative_prompt || "";
        s.steps = s.steps || 25;
        s.seed = s.seed || null;
        s.negative_prompt = s.negative_prompt || "";
        s.output = s.output || null;
        s.first_frame = s.first_frame || null;
        s.use_character_frame = !!s.use_character_frame;
        s.voice = s.voice || null;
        s.guides = s.guides || [];
        s.auto_match = !!s.auto_match;
        s.status = s.output ? "ok" : "pending";
        return s;
      });
      curIdx = 0;
      curPage = 0;
      $("btnReset").classList.remove("hidden");
      $("btnReset").classList.add("flex");
      $("lastSaved").textContent = "已恢复 " + scenes.length + " 个镜头";
      renderScenes(); renderMonitor(); syncProgress();
    }
  } catch (e) {}
}

var graphData = { nodes: [], edges: [] };
var GINP = "w-full rounded-md border border-white/10 bg-[#111119] px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-violet-400/60";

async function loadGraph() {
  try {
    var r = await api("/api/knowledge");
    if (r.ok) graphData = r.graph || { nodes: [], edges: [] };
    else toast("读取剧本图谱失败：" + (r.error || ""), "err");
  } catch (e) { toast("读取剧本图谱失败：" + e.message, "err"); }
  try {
    var r2 = await api("/api/assets/graph");
    if (r2.ok) assetGraphData = r2.graph || { nodes: [], edges: [] };
  } catch (e) {}
  renderGraph();
}

var graphMode = "script";
var assetGraphData = { nodes: [], edges: [] };

function renderGraph() {
  if (graphMode === "assets") { renderAssetGraph(); return; }
  var box = $("graphNodes");
  box.innerHTML = "";
  graphData.nodes.forEach(function (n, i) {
    var div = document.createElement("div");
    div.innerHTML = graphNodeHTML(n, i);
    box.appendChild(div.firstElementChild);
  });
  $("graphEdges").value = graphData.edges.map(function (e) {
    return e.from + " -> " + e.to + " : " + (e.relation || "");
  }).join("\n");
  renderGraphVisual();
}

function renderAssetGraph() {
  var box = $("assetGraphNodes");
  box.innerHTML = "";
  var nodes = assetGraphData.nodes || [];
  var edges = assetGraphData.edges || [];
  var assets = nodes.filter(function (n) { return n.type === "asset"; });
  assets.forEach(function (n) {
    var tags = edges.filter(function (e) { return e.from === n.id && e.relation === "HAS_TAG"; }).map(function (e) { return e.to.replace(/^tag:/, ""); });
    var fits = edges.filter(function (e) { return e.from === n.id && e.relation.indexOf("FITS") === 0; }).map(function (e) { return e.to; });
    var div = document.createElement("div");
    div.innerHTML = '<div class="rounded-xl border border-white/[0.08] bg-[#1b1b25] p-3">'
      + '<div class="mb-1 truncate text-xs font-semibold text-violet-300" title="' + esc(n.path || "") + '">' + esc(n.id) + '</div>'
      + '<div class="mb-1 truncate text-[10px] text-slate-500">' + esc(n.path || "") + '</div>'
      + '<div class="flex flex-wrap gap-1">'
      + tags.map(function (t) { return '<span class="rounded-md border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-200">' + esc(t) + '</span>'; }).join("")
      + fits.map(function (f) { return '<span class="rounded-md border border-fuchsia-400/30 bg-fuchsia-400/10 px-1.5 py-0.5 text-[10px] text-fuchsia-200">FITS ' + esc(f) + '</span>'; }).join("")
      + '</div></div>';
    box.appendChild(div.firstElementChild);
  });
  if (!assets.length) box.innerHTML = '<p class="col-span-full py-6 text-center text-xs text-slate-600">暂无素材图谱，先上传素材并打标签</p>';
  renderAssetGraphVisual();
}

function renderAssetGraphVisual() {
  var container = $("graphCanvas");
  if (!container || !window.cytoscape) return;
  var elements = [];
  assetGraphData.nodes.forEach(function (n) {
    var color = n.type === "tag" ? "#f59e0b" : n.type === "kg" ? "#22d3ee" : "#6d5cff";
    elements.push({ data: { id: n.id, label: n.label, color: color } });
  });
  assetGraphData.edges.forEach(function (e, i) {
    if (e.from && e.to) elements.push({ data: { id: "ae" + i, source: e.from, target: e.to, label: e.relation || "" } });
  });
  drawGraph(elements);
}

var cy = null;
var TYPE_COLORS = { "景别": "#6d5cff", "运镜": "#22d3ee", "风格": "#f472b6", "结构": "#fbbf24" };

function drawGraph(elements) {
  var container = $("graphCanvas");
  if (!container || !window.cytoscape) return;
  if (cy) { cy.destroy(); }
  cy = window.cytoscape({
    container: container,
    elements: elements,
    style: [
      { selector: "node", style: { "background-color": "data(color)", "label": "data(label)", "color": "#ececf4", "font-size": 11, "text-valign": "bottom", "text-margin-y": 5, "text-wrap": "wrap", "text-max-width": 70, "width": 40, "height": 40, "border-width": 2, "border-color": "#a855f7" } },
      { selector: "edge", style: { "width": 2, "line-color": "#3a3a50", "target-arrow-color": "#8f8fa3", "target-arrow-shape": "triangle", "curve-style": "bezier", "label": "data(label)", "font-size": 9, "color": "#8f8fa3", "text-rotation": "autorotate", "text-background-color": "#16161f", "text-background-opacity": 0.85, "text-background-padding": "2px" } }
    ],
    layout: { name: "cose", animate: false, padding: 30, nodeRepulsion: 8000, idealEdgeLength: 100 },
    wheelSensitivity: 0.2,
  });
}

function renderGraphVisual() {
  var elements = [];
  graphData.nodes.forEach(function (n) {
    elements.push({ data: { id: n.id, label: n.label, color: TYPE_COLORS[n.type] || "#6d5cff", type: n.type || "" } });
  });
  graphData.edges.forEach(function (e, i) {
    if (e.from && e.to) elements.push({ data: { id: "e" + i, source: e.from, target: e.to, label: e.relation || "" } });
  });
  drawGraph(elements);
}

function setGraphMode(mode) {
  graphMode = mode;
  var script = mode === "script";
  $("btnGraphScript").setAttribute("data-active", script ? "1" : "0");
  $("btnGraphAssets").setAttribute("data-active", script ? "0" : "1");
  $("graphNodes").classList.toggle("hidden", !script);
  $("assetGraphNodes").classList.toggle("hidden", script);
  $("graphScriptTools").classList.toggle("hidden", !script);
  $("graphEdgesLabel").classList.toggle("hidden", !script);
  renderGraph();
}

function graphNodeHTML(n, i) {
  return '<div class="graph-node rounded-xl border border-white/[0.08] bg-[#1b1b25] p-3" data-idx="' + i + '">'
    + '<div class="mb-2 flex items-center justify-between"><span class="text-xs font-semibold text-fuchsia-300">节点 ' + (i + 1) + '</span><button data-action="graph-del" class="flex h-6 w-6 items-center justify-center rounded-md border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="删除">✕</button></div>'
    + '<div class="grid grid-cols-2 gap-2"><input data-f="label" value="' + esc(n.label) + '" placeholder="标签" class="' + GINP + '"><input data-f="type" value="' + esc(n.type) + '" placeholder="类型" class="' + GINP + '"></div>'
    + '<textarea data-f="text" rows="2" class="mt-2 ' + GINP + ' resize-y">' + esc(n.text) + '</textarea>'
    + '<input data-f="keywords" value="' + esc((n.keywords || []).join(",")) + '" placeholder="关键词（逗号分隔）" class="mt-2 ' + GINP + '">'
    + '</div>';
}

function addGraphNode() {
  graphData.nodes.push({ id: "n" + Date.now(), type: "风格", label: "新节点", text: "", keywords: [] });
  renderGraph();
}

function syncGraphFromForm() {
  document.querySelectorAll("#graphNodes .graph-node").forEach(function (el) {
    var idx = parseInt(el.getAttribute("data-idx"));
    var n = graphData.nodes[idx];
    if (!n) return;
    n.label = el.querySelector('[data-f="label"]').value.trim();
    n.type = el.querySelector('[data-f="type"]').value.trim();
    n.text = el.querySelector('[data-f="text"]').value;
    n.keywords = el.querySelector('[data-f="keywords"]').value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  });
  graphData.edges = $("graphEdges").value.split("\n").map(function (line) {
    line = line.trim();
    if (!line) return null;
    var arrow = line.indexOf("->");
    if (arrow < 0) return null;
    var from = line.slice(0, arrow).trim();
    var rest = line.slice(arrow + 2).trim();
    var colon = rest.indexOf(":");
    var to = colon >= 0 ? rest.slice(0, colon).trim() : rest;
    var relation = colon >= 0 ? rest.slice(colon + 1).trim() : "";
    return { from: from, to: to, relation: relation };
  }).filter(Boolean);
}

async function saveGraph() {
  syncGraphFromForm();
  try {
    var r = await api("/api/knowledge", { graph: graphData });
    if (r.ok) toast("图谱已保存", "ok");
    else toast("保存失败：" + (r.error || ""), "err");
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

// 分镜台事件委托
$("scenes").addEventListener("click", function (e) {
  var actionEl = e.target.closest("[data-action]");
  var card = e.target.closest(".shot");
  if (actionEl && card) {
    var id = card.getAttribute("data-id");
    var a = actionEl.getAttribute("data-action");
    e.stopPropagation();
    if (a === "gen") generateScene(id);
    else if (a === "frame") uploadFrame(id);
    else if (a === "frame-del") clearFrame(id);
    else if (a === "voice") uploadVoice(id);
    else if (a === "voice-del") clearVoice(id);
    else if (a === "guide") uploadGuide(id);
    else if (a === "guide-del") removeGuide(id, parseInt(actionEl.getAttribute("data-gi")));
    else if (a === "up") moveScene(id, -1);
    else if (a === "down") moveScene(id, 1);
    else if (a === "del") removeScene(id);
    return;
  }
  if (card) selectScene(parseInt(card.getAttribute("data-index")));
});
$("scenes").addEventListener("input", function (e) {
  var t = e.target;
  if (t.dataset && t.dataset.id && t.dataset.field) {
    var s = scenes.find(function (x) { return x.id === t.dataset.id; });
    if (!s) return;
    var f = t.dataset.field, v = t.value;
    if (f === "seconds") s.seconds = clampInt(v, 3, 15, 5);
    else if (f === "steps") s.steps = clampInt(v, 5, 50, 25);
    else if (f === "seed") s.seed = String(v).trim() !== "" ? parseInt(v) : null;
    else s[f] = v;
    if (f === "seconds" && scenes[curIdx] && scenes[curIdx].id === t.dataset.id) renderMonitor();
    scheduleSaveState();
  }
});
$("scenes").addEventListener("change", function (e) {
  var t = e.target;
  if (!(t.dataset && t.dataset.id && t.dataset.field)) return;
  var s = scenes.find(function (x) { return x.id === t.dataset.id; });
  if (!s) return;
  s[t.dataset.field] = t.type === "checkbox" ? t.checked : t.value;
  if (t.dataset.field === "mode") {
    var map = modelStepsFor();
    s.steps = map[t.value] || map["标准"];
    var stepsEl = document.querySelector('[data-id="' + t.dataset.id + '"][data-field="steps"]');
    if (stepsEl) stepsEl.value = s.steps;
  }
  if (scenes[curIdx] && scenes[curIdx].id === t.dataset.id) renderMonitor();
  scheduleSaveState();
});

// 素材库事件委托
$("assets").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action]");
  if (el) {
    var card = el.closest(".asset-card");
    if (!card) return;
    var a = el.getAttribute("data-action");
    if (a === "asset-del") deleteAsset(card.getAttribute("data-path"));
    else if (a === "asset-tag") tagAsset(card.getAttribute("data-path"));
    else if (a === "asset-tag-manual") manualTagAsset(card.getAttribute("data-path"));
  }
});

$("saved").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action]");
  if (el) {
    var card = el.closest(".saved-card");
    if (!card) return;
    var a = el.getAttribute("data-action");
    if (a === "saved-del") deleteOutput(card.getAttribute("data-path"));
  }
});

// 顶部/面板按钮
$("btnImport").addEventListener("click", function () {
  $("importPanel").classList.toggle("hidden");
  if (!$("importPanel").classList.contains("hidden")) $("script").focus();
});
$("btnImportClose").addEventListener("click", function () { $("importPanel").classList.add("hidden"); });
$("btnAssets").addEventListener("click", function () {
  var p = $("assetPanel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden")) { loadAssets(); loadAssetsTags(); }
});
$("btnAssetRefresh").addEventListener("click", loadAssets);
$("btnAssetUpload").addEventListener("click", uploadAsset);
$("btnSaved").addEventListener("click", function () {
  var p = $("savedPanel");
  p.classList.toggle("hidden");
  if (!p.classList.contains("hidden")) loadOutputs();
});
$("btnSavedRefresh").addEventListener("click", loadOutputs);
$("btnOptimize").addEventListener("click", optimize);
$("btnAddScene").addEventListener("click", addScene);
$("btnConcat").addEventListener("click", concat);
$("btnReset").addEventListener("click", reset);
$("btnPagePrev").addEventListener("click", function () { if (curPage > 0) { curPage -= 1; renderScenes(); } });
$("timeline").addEventListener("click", function (e) {
  var el = e.target.closest("[data-idx]");
  if (el) selectScene(parseInt(el.getAttribute("data-idx")));
});
$("btnPageNext").addEventListener("click", function () { if (curPage < Math.ceil(scenes.length / PAGE_SIZE) - 1) { curPage += 1; renderScenes(); } });
$("btnFinalClose").addEventListener("click", function () {
  $("finalModal").classList.add("hidden");
  $("finalModalVideo").pause();
});
$("btnPrev").addEventListener("click", prevShot);
$("btnNext").addEventListener("click", nextShot);
$("btnGenAll").addEventListener("click", generateAll);
$("btnStop").addEventListener("click", function () { genAllStop = true; });
$("btnCancel").addEventListener("click", cancelCurrent);
$("btnPause").addEventListener("click", function () {
  genAllPaused = !genAllPaused;
  setPauseButton();
  toast(genAllPaused ? "已暂停：当前镜头完成后不再继续" : "已继续生成", "ok");
});
$("btnDocx").addEventListener("click", function () { $("docx").click(); });
$("btnScriptAI").addEventListener("click", aiScript);
$("btnSettings").addEventListener("click", function () { fillSettingsForm(); $("settingsPanel").classList.remove("hidden"); });
$("btnSettingsClose").addEventListener("click", function () { $("settingsPanel").classList.add("hidden"); });
$("btnSettingsSave").addEventListener("click", applySettings);
$("btnSettingsReset").addEventListener("click", resetSettings);
$("themePicker").addEventListener("click", function (e) {
  var b = e.target.closest("[data-theme]");
  if (!b) return;
  settings.theme = b.getAttribute("data-theme");
  saveSettings();
  applyTheme();
});
$("btnGraph").addEventListener("click", function () { $("graphPanel").classList.remove("hidden"); loadGraph(); });
$("btnGraphScript").addEventListener("click", function () { setGraphMode("script"); });
$("btnGraphAssets").addEventListener("click", function () { setGraphMode("assets"); });
$("btnGraphClose").addEventListener("click", function () { $("graphPanel").classList.add("hidden"); });
$("btnGraphAdd").addEventListener("click", addGraphNode);
$("btnGraphSave").addEventListener("click", saveGraph);
$("videoModelSel").addEventListener("change", function () {
  videoModel = this.value;
  try { localStorage.setItem("director_video_model", videoModel); } catch (e) {}
  var map = modelStepsFor();
  scenes.forEach(function (s) {
    s.steps = map[s.mode] || map["标准"];
    var el = document.querySelector('[data-id="' + s.id + '"][data-field="steps"]');
    if (el) el.value = s.steps;
  });
  if (scenes[curIdx]) renderMonitor();
  toast("模型已切换：" + videoModel + "（各镜头步数已按档位重算）", "ok");
});
$("btnGlobal").addEventListener("click", function () { fillGlobalForm(); $("globalPanel").classList.remove("hidden"); });
$("btnGlobalClose").addEventListener("click", function () { $("globalPanel").classList.add("hidden"); });
$("btnGlobalSave").addEventListener("click", saveGlobal);
$("btnCharImage").addEventListener("click", uploadCharImage);
$("btnCharImageDel").addEventListener("click", delCharImage);
document.addEventListener("click", function (e) {
  var el = e.target.closest("[data-close]");
  if (el) { $(el.getAttribute("data-close")).classList.add("hidden"); }
});
$("graphNodes").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action='graph-del']");
  if (el) {
    var node = el.closest(".graph-node");
    if (node) { graphData.nodes.splice(parseInt(node.getAttribute("data-idx")), 1); renderGraph(); }
  }
});
$("btnAssetSearch").addEventListener("click", function () { searchAssets($("assetSearch").value); });
if ($("assetSearch")) $("assetSearch").addEventListener("keydown", function (e) { if (e.key === "Enter") searchAssets($("assetSearch").value); });

$("docx").addEventListener("change", function (e) {
  var f = e.target.files[0];
  if (!f) return;
  docxFilename = f.name;
  var reader = new FileReader();
  reader.onload = function () {
    docxB64 = reader.result.split(",")[1];
    docxName = f.name;
    $("docxName").textContent = "已选择：" + f.name;
  };
  reader.readAsDataURL(f);
});

$("script").addEventListener("input", function () { $("charCount").textContent = $("script").value.length + " 字"; });
$("script").addEventListener("keydown", function (e) {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); optimize(); }
});

async function checkComfy() {
  var dot = $("comfyDot");
  var st = $("comfyStatus");
  try {
    var r = await api("/api/status");
    if (r.ok) {
      dot.className = "h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_10px_#34d399]";
      st.textContent = "ComfyUI 在线 · 运行 " + r.running + " / 排队 " + r.pending;
      st.className = "text-slate-400";
      if (!r.running && !r.pending) recoverStuckBusy();
    } else {
      dot.className = "h-2 w-2 rounded-full bg-red-400";
      st.textContent = "ComfyUI 未连接";
      st.className = "text-red-400";
    }
  } catch (e) {
    dot.className = "h-2 w-2 rounded-full bg-red-400";
    st.textContent = "ComfyUI 未连接";
    st.className = "text-red-400";
  }
}

setInterval(function () {
  var now = Date.now();
  scenes.forEach(function (s) {
    if (s.status !== "busy" || !s.startedAt) return;
    var badge = $("st_" + s.id);
    if (!badge) return;
    var sec = Math.floor((now - s.startedAt) / 1000);
    var mm = Math.floor(sec / 60), ss = sec % 60;
    badge.innerHTML = '<span class="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse"></span>生成中 ' + mm + ":" + pad2(ss);
  });
}, 1000);

loadSettings();
loadVideoModel();
loadToggles();
restoreState();
checkComfy();
setInterval(checkComfy, 15000);
