// 导演台前端逻辑（Tailwind 设计 + 真实后端功能）
var scenes = [];
var curIdx = -1;
var docxB64 = null;
var docxFilename = "";
var docxReading = null;
var genAllStop = false;
var needAudio = true;
var subtitleOn = true;
var chainFrames = false;
var bridgeOn = true;
var PAGE_SIZE = 1;
var curPage = 0;
var tagsMap = {};
var currentAssets = [];
var character = { name: "", description: "", image: null };
var scene = { name: "", description: "", image: null };
var equipment = { name: "", description: "", image: null };   // 工业场景的主资产：设备/产品
// 素材池：角色/场景/设备不再是「每类一个」，而是任意条数的池子。上面三个单例保留为
// 「每类第一条」，全局设置面板继续编辑它们（见 restoreState 里的 fallbackAssets）。
// 每个镜头用 scene.refs 按顺序引用池子里的素材，顺序即 <Subject N> 的编号。
var assets = [];
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
  // 会调 API 的三个 agent 各配各的（engine/model/api_key/base_url），渲染在「设置 → Agent 模型」
  agent_models: {},
  // 本地 ComfyUI 的 agent 里只有视频模型有得选，其余各只装了一个权重，只读展示
  video_model: "MiniMax H3",
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

// 视频模型从 localStorage 挪进了「Agent 模型」表，旧键 director_video_model 不再读
var VIDEO_MODELS = ["MiniMax H3", "Wan2.2 5B", "Wan2.2 14B", "Wan2.2 S2V"];

function loadVideoModel() {
  videoModel = settings.video_model || "MiniMax H3";
  if (VIDEO_MODELS.indexOf(videoModel) < 0) videoModel = "MiniMax H3";
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
// 「Agent 模型」表：/api/agents 回来的注册表 + settings.agent_models 里的用户配置
var agentMeta = [];
var LLM_ENGINES = [["ollama", "本机 Ollama"], ["deepseek", "DeepSeek"], ["qwen", "通义千问"]];
var AM_INP = "w-full rounded-md border border-white/10 bg-[#111119] px-2 py-1 text-[11px] text-slate-200 outline-none focus:border-violet-400/60 disabled:opacity-30";

// 换引擎要把地址和模型一起带过去：留着上一家的地址，就会拿新 key 去敲旧端点，
// 报回来的 401/404 看不出是哪儿错了
var ENGINE_DEFAULTS = {
  ollama:   { url: "http://127.0.0.1:11434", model: "qwen3:8b" },
  deepseek: { url: "https://api.deepseek.com", model: "deepseek-chat" },
  qwen:     { url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen3-vl-plus" }
};

function agentModelCfg(id) {
  settings.agent_models = settings.agent_models || {};
  if (!settings.agent_models[id]) {
    // 老版本三个 agent 共用上面那张 DeepSeek 卡，第一次进来照着它起一份，不用重新填 key
    settings.agent_models[id] = { engine: "deepseek", model: settings.deepseek_model || "deepseek-chat",
      api_key: settings.deepseek_key || "", base_url: settings.deepseek_url || "https://api.deepseek.com" };
  }
  return settings.agent_models[id];
}

function renderAgentModelRows() {
  var box = $("agentModelRows");
  if (!box || !agentMeta.length) return;
  box.innerHTML = agentMeta.map(function (a) {
    var cfg = a.kind === "llm" ? agentModelCfg(a.id) : null;
    var head = '<td class="py-1.5 pr-3 whitespace-nowrap text-slate-300">'
      + '<iconify-icon icon="' + esc(a.icon || "lucide:box") + '" width="12" class="mr-1 align-[-2px] '
      + (a.kind === "llm" ? "text-violet-300" : "text-cyan-300") + '"></iconify-icon>'
      + esc(a.name) + '</td>';
    if (a.kind !== "llm") {
      // 本地 ComfyUI：权重文件工作是认名字的，装了几个就列几个，没得选就照实写出来
      var cell = (a.options && a.options.length)
        ? '<select data-video-model class="' + AM_INP + '">'
            + a.options.map(function (o) {
                return '<option value="' + esc(o) + '"' + (settings.video_model === o ? " selected" : "") + '>' + esc(o) + '</option>';
              }).join("") + '</select>'
        : '<span class="block truncate px-2 py-1 text-slate-400" title="' + esc(a.model) + '（本机只装了这一个权重）">' + esc(a.model) + '</span>';
      return '<tr class="border-t border-white/[0.06]">' + head
        + '<td class="py-1.5 pr-3 text-slate-500">本地 ComfyUI</td>'
        + '<td class="py-1.5 pr-3">' + cell + '</td>'
        + '<td class="py-1.5 pr-3 text-slate-600">—</td><td class="py-1.5 text-slate-600">—</td></tr>';
    }
    // 本机 Ollama 不吃 key，也没必要填 URL（默认就是本机地址），置灰免得看着像没生效
    var off = cfg.engine === "ollama" ? " disabled" : "";
    return '<tr class="border-t border-white/[0.06]">' + head
      + '<td class="py-1.5 pr-3"><select data-agent="' + a.id + '" data-f="engine" class="' + AM_INP + '">'
        + LLM_ENGINES.map(function (e) {
            return '<option value="' + e[0] + '"' + (cfg.engine === e[0] ? " selected" : "") + '>' + e[1] + '</option>';
          }).join("") + '</select></td>'
      + '<td class="py-1.5 pr-3"><input data-agent="' + a.id + '" data-f="model" value="' + esc(cfg.model || "") + '" class="' + AM_INP + '"></td>'
      + '<td class="py-1.5 pr-3"><input type="password" data-agent="' + a.id + '" data-f="api_key" value="' + esc(cfg.api_key || "") + '" placeholder="留空用环境变量" class="' + AM_INP + '"' + off + '></td>'
      + '<td class="py-1.5"><input data-agent="' + a.id + '" data-f="base_url" value="' + esc(cfg.base_url || "") + '" class="' + AM_INP + '"' + off + '></td></tr>';
  }).join("");
  box.querySelectorAll('select[data-f="engine"]').forEach(function (sel) {
    sel.addEventListener("change", function () { applyEngineDefault(sel.getAttribute("data-agent"), sel.value); });
  });
}

function applyEngineDefault(id, engine) {
  var cfg = agentModelCfg(id);
  var d = ENGINE_DEFAULTS[engine];
  cfg.engine = engine;
  if (d) {
    cfg.base_url = d.url;
    // 模型只在它还停在别家默认值时才跟着换，用户自己填过的就留着他填的
    var isStock = !cfg.model || Object.keys(ENGINE_DEFAULTS).some(function (k) { return ENGINE_DEFAULTS[k].model === cfg.model; });
    if (isStock) cfg.model = d.model;
  }
  renderAgentModelRows();
}

function syncAgentModels() {
  document.querySelectorAll("#agentModelRows [data-agent]").forEach(function (el) {
    var f = el.getAttribute("data-f");
    if (f) agentModelCfg(el.getAttribute("data-agent"))[f] = el.value.trim();
  });
  var vm = document.querySelector("#agentModelRows [data-video-model]");
  if (vm) { settings.video_model = vm.value; loadVideoModel(); }   // 顶部那个下拉跟着走
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
  if (agentMeta.length) renderAgentModelRows();
  else loadAgents();          // 拉回来之后 loadAgents 会自己把这张表渲染出来
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
  syncAgentModels();
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
var ANGLES = ["平视", "仰视", "俯视", "过肩", "鸟瞰"];
var ASPECTS = ["16:9", "9:16", "1:1", "21:9"];
var RESOLUTIONS = ["480p", "720p", "1080p", "2K", "4K"];
var ASPECT_RATIOS = { "16:9": [16, 9], "9:16": [9, 16], "1:1": [1, 1], "21:9": [21, 9] };
var RES_WH = { "480p": 480, "720p": 720, "1080p": 1080, "2K": 1440, "4K": 2160 };
var MODES = ["标准", "均衡", "高清", "极速"];
var MODE_STEPS = { "标准": 25, "均衡": 12, "高清": 32, "极速": 4 };

var SEL = "mt-1 w-full rounded-md border border-white/10 bg-[#111119] px-1.5 py-1.5 text-[11px] text-slate-300 outline-none";
var NUM = "mt-1 w-full rounded-md border border-white/10 bg-[#111119] px-2 py-1.5 text-[11px] text-slate-300 outline-none";
var INP = "mt-1 w-full rounded-md border border-white/10 bg-[#111119] px-2 py-1.5 text-[11px] text-slate-300 outline-none";
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
  for (var k = 0; k < i; k++) { acc += shotLength(scenes[k]) + (bridgeOn ? BRIDGE_SEC : 0); }
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
  for (var k in s) { if (k !== "status" && k !== "startedAt" && k !== "run") o[k] = s[k]; }
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
      applyLinkedScenes(r.scenes);     // 后端顺手重算的连线要抄回来，不然画布上的边是旧的
      var d = new Date();
      var el = $("lastSaved");
      if (el) el.textContent = "已保存 " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    }
  } catch (e) {}
}

function syncScene(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  if (!document.querySelector('[data-id="' + id + '"][data-field="prompt"]')) return;
  s.prompt = fieldVal(id, "prompt");
  s.shot = fieldVal(id, "shot") || "中景";
  s.camera = fieldVal(id, "camera") || "固定镜头";
  s.angle = fieldVal(id, "angle");
  s.subject = fieldVal(id, "subject");
  s.action = fieldVal(id, "action");
  s.environment = fieldVal(id, "environment");
  s.lighting = fieldVal(id, "lighting");
  s.sfx = fieldVal(id, "sfx");
  s.aspect = fieldVal(id, "aspect") || "16:9";
  s.resolution = fieldVal(id, "resolution") || "480p";
  s.mode = fieldVal(id, "mode") || "标准";
  s.seconds = clampInt(fieldVal(id, "seconds"), 4, 15, 5);   // 4 秒是 H3 训练域的下沿，3 秒会跑出模型见过的范围
  s.steps = clampInt(fieldVal(id, "steps"), 1, 50, 25);
  s.seed = String(fieldVal(id, "seed")).trim() !== "" ? parseInt(fieldVal(id, "seed")) : null;
  s.subtitle = fieldVal(id, "subtitle");
  s.negative_prompt = fieldVal(id, "negative_prompt");
  var cf = document.querySelector('[data-id="' + id + '"][data-field="use_character_frame"]');
  s.use_character_frame = cf ? cf.checked : !!s.use_character_frame;
  var am = document.querySelector('[data-id="' + id + '"][data-field="auto_match"]');
  s.auto_match = am ? am.checked : !!s.auto_match;
  var sp = document.querySelector('[data-id="' + id + '"][data-field="second_pass"]');
  s.second_pass = sp ? sp.checked : !!s.second_pass;
}

function renderScenes() {
  var box = $("scenes");
  box.innerHTML = "";
  $("scenesEmpty").classList.toggle("hidden", scenes.length > 0);
  var framesTotal = 0;
  scenes.forEach(function (s) { framesTotal += (s.states || []).length; });
  // 一次只铺当前这一镜：镜头一多，全部堆在一屏上谁也看不清；翻镜走上面那条分页条、
  // 监视器的上一镜/下一镜，或者下面的时间线。
  var cur = scenes[curIdx];
  if (cur) {
    var div = document.createElement("div");
    div.innerHTML = (cur.states || []).length ? shotGroupHTML(cur, curIdx) : shotCardHTML(cur, curIdx);
    box.appendChild(div.firstElementChild);
  }
  $("shotCount").textContent = scenes.length
    ? scenes.length + " 个镜头" + (framesTotal ? " · " + framesTotal + " 个变化帧" : "") : "";
  renderPager(scenes.length);
  renderTimeline();
  // 故事板默认就是打开的那一屏，所以场景一变它就得跟着重画；第一次还要自己摆正，
  // 否则新用户看到的是左上角一小块，以为什么都没生成出来。
  if (boardView === "board") {
    renderBoard();
    if (!boardInit) { boardInit = true; boardFit(); }
  }
}

// ---- 一个镜头算不算出完 ------------------------------------------------------------
//
// 镜头的产物有两种形态：没拆变化帧的是整镜成片（scene.output），拆过的是「相邻两帧一段」的
// 片段（states[i].clip，最后一段不需要）。判定、取片段、算时长三件事必须走同一套，
// 否则「生成全部」说完成了、进度条说还差镜头、合成又说还有镜头未生成。
function shotSegments(s) {
  var frames = s.states || [];
  var out = [];
  for (var i = 0; i < frames.length - 1; i++) out.push({ index: i, frame: frames[i] });
  return out;
}

function shotClips(s) {
  var segs = shotSegments(s);
  if (!segs.length) return s.output ? [s.output] : [];
  var out = [];
  for (var i = 0; i < segs.length; i++) {
    if (!segs[i].frame.clip) return [];
    out.push(segs[i].frame.clip);
  }
  return out;
}

function shotDone(s) {
  return shotSegments(s).length ? shotClips(s).length > 0 : !!s.output;
}

function shotLength(s) {
  var segs = shotSegments(s);
  if (!segs.length) return s.seconds || 5;
  var n = 0;
  segs.forEach(function (g) { n += (g.frame.seconds || 4); });
  return n || (s.seconds || 5);
}

function filmLenSec() {
  var acc = 0;
  scenes.forEach(function (s, i) {
    acc += shotLength(s);
    if (bridgeOn && i < scenes.length - 1) acc += BRIDGE_SEC;
  });
  return acc;
}

function renderTimeline() {
  var box = $("timeline");
  if (!box) return;
  if (!scenes.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  var film = filmLenSec();
  $("timelineTotal").textContent = fmtTC(film) + " · " + scenes.length + " 镜" + (bridgeOn && scenes.length > 1 ? " · 含过渡" : "");
  var gapW = bridgeOn ? (BRIDGE_SEC / film) * 100 : 0;
  function block(s, i, cls, title) {
    var w = Math.max(0.6, (shotLength(s) / film) * 100);
    var gap = bridgeOn && i < scenes.length - 1
      ? '<span class="shrink-0 bg-white/[0.04]" style="width:' + gapW.toFixed(2) + '%"></span>'
      : "";
    return '<button data-idx="' + i + '" class="' + cls + ' rounded-sm transition hover:brightness-125" style="width:' + w.toFixed(2) + '%" title="' + esc(title) + '"></button>' + gap;
  }
  $("trackVideo").innerHTML = scenes.map(function (s, i) {
    return block(s, i,
      s.status === "ok" ? "bg-emerald-400/80" : s.status === "busy" ? "bg-amber-400/80 animate-pulse" : s.status === "err" ? "bg-red-400/80" : "bg-slate-500/60",
      "镜头 " + (i + 1) + " · " + fmtTC(shotStartSec(i)) + "–" + fmtTC(shotStartSec(i) + shotLength(s)) + " · " + shotLength(s) + "s · " + shortTitle(s.prompt));
  }).join("");
  $("trackSub").innerHTML = scenes.map(function (s, i) {
    var t = String(s.subtitle || "").trim();
    return block(s, i, t ? "bg-amber-400/60" : "bg-white/[0.08]", t ? "字幕 · " + t : "无字幕");
  }).join("");
  renderTimelineRuler(film);
  highlightTimeline();
  movePlayhead(shotStartSec(curIdx >= 0 && curIdx < scenes.length ? curIdx : 0));
  syncTimelineShot();
}

function renderTimelineRuler(film) {
  var r = $("timelineRuler");
  if (!r) return;
  var step = film >= 180 ? 60 : 30;
  var h = "";
  for (var t = 0; t <= film; t += step) {
    h += '<span class="absolute top-0' + (t === 0 ? "" : " -translate-x-1/2") + '" style="left:' + Math.min(100, (t / film) * 100).toFixed(2) + '%">' + fmtTC(t) + '</span>';
  }
  if (film % step !== 0) h += '<span class="absolute top-0 -translate-x-full" style="left:100%">' + fmtTC(film) + '</span>';
  r.innerHTML = h;
}

function highlightTimeline() {
  document.querySelectorAll("#trackVideo [data-idx]").forEach(function (b) {
    var on = parseInt(b.getAttribute("data-idx")) === curIdx;
    b.classList.toggle("ring-1", on);
    b.classList.toggle("ring-white/80", on);
  });
}

function movePlayhead(sec) {
  var ph = $("timelinePlayhead");
  if (!ph) return;
  var film = filmLenSec() || 1;
  ph.style.left = Math.max(0, Math.min(100, (sec / film) * 100)).toFixed(2) + "%";
  var pos = $("timelinePos");
  if (pos) pos.textContent = fmtTC(sec);
}

function renderPager(pages) {
  var pager = $("scenePager");
  if (!pager) return;
  if (pages > 1) { pager.classList.remove("hidden"); pager.classList.add("flex"); }
  else { pager.classList.add("hidden"); pager.classList.remove("flex"); }
  $("pageInfo").textContent = "第 " + (curIdx + 1) + " / " + pages + " 镜";
  $("btnPagePrev").disabled = curIdx <= 0;
  $("btnPageNext").disabled = curIdx >= pages - 1;
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
    + '<label class="' + LBL + '">角度<select data-id="' + s.id + '" data-field="angle" class="' + SEL + '">' + opts(ANGLES, s.angle) + '</select></label>'
    + '<label class="' + LBL + '">比例<select data-id="' + s.id + '" data-field="aspect" class="' + SEL + '">' + opts(ASPECTS, s.aspect) + '</select></label>'
    + '</div>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">模式<select data-id="' + s.id + '" data-field="mode" class="' + SEL + '">' + opts(MODES, s.mode) + '</select></label>'
    + '<label class="' + LBL + '">清晰度<select data-id="' + s.id + '" data-field="resolution" class="' + SEL + '">' + opts(RESOLUTIONS, s.resolution) + '</select></label>'
    + '<label class="' + LBL + '">时长(秒)<input data-id="' + s.id + '" data-field="seconds" type="number" min="4" max="15" value="' + s.seconds + '" class="' + NUM + '"></label>'
    + '<label class="' + LBL + '">步数<input data-id="' + s.id + '" data-field="steps" type="number" min="1" max="50" value="' + (s.steps || 25) + '" class="' + NUM + '"></label>'
    + '</div>'
    + '<div class="mt-2 rounded-lg border border-white/[0.07] bg-black/20 p-2">'
    + '<p class="mb-1.5 text-[10px] text-slate-500">画面细节 · 交给语言模型编译成提示词</p>'
    + '<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">主体<input data-id="' + s.id + '" data-field="subject" type="text" value="' + esc(s.subject) + '" placeholder="画面里是谁/什么" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">动作<input data-id="' + s.id + '" data-field="action" type="text" value="' + esc(s.action) + '" placeholder="在做什么" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">环境<input data-id="' + s.id + '" data-field="environment" type="text" value="' + esc(s.environment) + '" placeholder="在哪里" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">光线<input data-id="' + s.id + '" data-field="lighting" type="text" value="' + esc(s.lighting) + '" placeholder="光从哪来" class="' + INP + '"></label>'
    + '</div>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">音效<input data-id="' + s.id + '" data-field="sfx" type="text" value="' + esc(s.sfx) + '" placeholder="现场声音" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">种子<input data-id="' + s.id + '" data-field="seed" type="number" value="' + (s.seed || "") + '" placeholder="随机" class="' + NUM + '"></label>'
    + '</div></div>'
    + '<div class="mt-2 rounded-lg border border-white/[0.07] bg-black/20 p-2">'
    + '<p class="mb-1.5 text-[10px] text-slate-500">参考图引用 · <b class="text-cyan-300">顺序即 &lt;Subject N&gt; 编号</b></p>'
    + '<div class="flex flex-wrap items-center gap-1.5" data-refbox="' + s.id + '">'
    + (refsHtml(s) || '<span class="text-[10px] text-slate-600">还没引用素材 —— 从下面加，顺序决定 &lt;Subject 1/2/3&gt; 是谁</span>')
    + '</div>'
    + '<select data-refadd="' + s.id + '" class="' + SEL + '" style="margin-top:6px">'
    + '<option value="">+ 从素材池添加参考图…</option>' + assetOptions()
    + '</select>'
    + (s.refs_manual ? '<button data-action="relink" class="mt-1.5 w-full rounded-md border border-white/10 py-1 text-[10px] text-slate-400 transition hover:border-cyan-400/40 hover:text-cyan-200">这一镜手动改过引用 · 点这里交回自动连线</button>' : '')
    + '</div>'
    + '<button data-action="compile" title="把上面填的字段交给语言模型，写回这段画面描述" class="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-violet-400/30 bg-violet-400/10 py-1.5 text-[11px] text-violet-200 transition hover:bg-violet-400/20"><iconify-icon icon="lucide:sparkles" width="14"></iconify-icon>编译提示词</button>'
    + '<input data-id="' + s.id + '" data-field="subtitle" type="text" value="' + esc(s.subtitle) + '" placeholder="字幕（叠加在画面，可选）" class="mt-3 w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-300 outline-none focus:border-violet-400/60">'
    + '<input data-id="' + s.id + '" data-field="negative_prompt" type="text" value="' + esc(s.negative_prompt) + '" placeholder="负向提示词（不想出现的元素，可选）" class="mt-2 w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-400 outline-none focus:border-violet-400/60">'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="frame" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-cyan-400/40 hover:bg-cyan-400/10" title="上传一张图作为该镜头的第一帧，生成时图片+文字一起驱动">'
    + '<iconify-icon icon="lucide:image-plus" width="13"></iconify-icon>' + (s.first_frame ? '更换首帧' : '上传首帧（图+文生视频）') + '</button>'
    + (s.first_frame ? '<button data-action="frame-del" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="移除首帧">✕</button>' : '')
    + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="continue" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-sky-400/40 hover:bg-sky-400/10" title="上传一段视频，用它的尾帧作新镜头首帧接着往下拍（新镜头自动插在本镜之后，按本镜提示词生成）"><iconify-icon icon="lucide:film" width="13"></iconify-icon>续接视频（上传素材）</button>'
    + (s.output ? '<button data-action="continue-own" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-sky-400/40 hover:text-sky-200" title="续接本镜成片：用本镜视频的尾帧生成下一镜"><iconify-icon icon="lucide:redo-2" width="13"></iconify-icon></button>' : '')
    + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="ding" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-violet-400/40 hover:bg-violet-400/10' + (s.dingBusy ? ' opacity-50' : '') + '" title="按该镜提示词用 SDXL 文生图出 4 张定帧，挑一张作为首帧（解决提示词元素不出现在画面里的问题）">'
    + '<iconify-icon icon="lucide:sparkles" width="13"></iconify-icon>' + (s.dingBusy ? '定帧生成中…' : (shotDingCandidates(s).length ? '再定帧 4 张' : '文生图定帧（提示词出首帧）')) + '</button>'
    + '<button data-action="keyframe" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-sky-400/40 hover:bg-sky-400/10' + (s.kfBusy ? ' opacity-50' : '') + '" title="故事版关键帧：以「全局设置」里的角色/场景资产图作参考，用 Flux Kontext 按本镜提示词把画面改出来；需先生成资产图">'
    + '<iconify-icon icon="lucide:clapperboard" width="13"></iconify-icon>' + (s.kfBusy ? '关键帧生成中…' : '故事版关键帧（资产图参考）') + '</button>'
    + '</div>'
    + (shotDingCandidates(s).length
        ? '<div class="mt-2 flex items-center gap-1.5 overflow-x-auto">' + shotDingCandidates(s).map(function (f) {
            var picked = shotDingFrame(s) === f;
            return '<div data-action="ding-preview" data-file="' + esc(f) + '" title="点击放大查看" class="relative h-16 w-28 shrink-0 cursor-zoom-in overflow-hidden rounded-md border ' + (picked ? 'border-violet-400/60' : 'border-white/10') + ' bg-black/40">'
              + '<img class="h-full w-full object-cover" src="/input/' + encodeURI(f) + '">'
              + '<button data-action="ding-pick" data-file="' + esc(f) + '" class="absolute inset-x-0 bottom-0 bg-black/70 py-0.5 text-[10px] ' + (picked ? 'text-violet-300' : 'text-slate-300 hover:text-white') + '">' + (picked ? '已选用 ✓' : '选用') + '</button></div>';
          }).join("") + '</div>'
        : '')
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
    + (videoModel === "MiniMax H3" ? '<label class="mt-2 flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer"><input data-id="' + s.id + '" data-field="second_pass" type="checkbox" class="h-3.5 w-3.5 accent-violet-400"' + (s.second_pass ? " checked" : "") + '>二采精炼（生成后抽首尾帧再精炼一遍，更稳更连贯，时间约翻倍）</label>' : '')
    + (character.image ? '<label class="mt-2 flex items-center gap-2 text-[11px] text-slate-400 cursor-pointer"><input data-id="' + s.id + '" data-field="use_character_frame" type="checkbox" class="h-3.5 w-3.5 accent-pink-400"' + (s.use_character_frame ? " checked" : "") + '>角色出场（用角色参考图做首帧）</label>' : '')
    + '<button data-action="gen" id="gen_' + s.id + '" ' + (s.status === "busy" ? "disabled" : "") + ' class="mt-3 flex w-full items-center justify-center gap-2 rounded-lg ' + genCls + ' py-2 text-xs font-semibold transition disabled:cursor-not-allowed">' + genBtn + '</button>'
    + '</article>';
}

// ---- 变化帧卡片 -------------------------------------------------------------------
//
// 一个镜头拆过变化帧之后，卡片列表跟着拆：有几个变化帧就有几张卡。画布上那一列和卡片
// 列表本来就是同一件事，不这么拆的话，画布上选好的图在卡片里看不到、卡片上改的字在
// 画布上也不生效。镜头级的东西（参考图、定帧、音效、整镜出片…）收进第一张帧卡的
// 「整镜设置」，两层控制不混在同一张卡上。

function frameCardHTML(s, i, k) {
  var frames = s.states || [];
  var fr = frames[k] || {};
  var next = k < frames.length - 1 ? frames[k + 1] : null;
  var kf = fr.keyframe || "";
  var cands = fr.candidates || [];
  var segReady = !!(kf && next && next.keyframe);
  var IN = 'w-full rounded-lg border border-white/10 bg-black/20 p-2 text-xs leading-5 text-slate-300 outline-none focus:border-violet-400/50';
  return '<article class="shot rounded-xl border border-white/10 bg-[#1b1b25] p-3.5 cursor-pointer transition hover:border-white/20" data-id="' + s.id + '" data-index="' + i + '" data-frame="' + k + '">'
    + '<div class="flex items-center justify-between">'
    + '<div class="flex min-w-0 items-center gap-2.5">'
    + '<span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white/[0.06] text-xs font-bold text-slate-300">' + pad2(i + 1) + '</span>'
    + '<h4 class="truncate text-sm font-semibold">' + esc(shortTitle(fr.prompt)) + '</h4>'
    + '<span class="shrink-0 rounded bg-black/40 px-1.5 py-0.5 text-[10px] text-slate-400">变化帧 ' + (k + 1) + '/' + frames.length + '</span>'
    + '</div>'
    + '<div class="flex shrink-0 items-center gap-1.5">'
    + '<span class="rounded-full bg-white/[0.06] px-2 py-1 text-[10px] text-slate-400">' + esc(fr.shot || "") + ' · ' + (fr.seconds || 0) + 's</span>'
    + '<button data-action="board-frame-del" data-idx="' + k + '" class="flex h-7 w-7 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="删掉这个变化帧">✕</button>'
    + '</div></div>'
    + '<div class="mt-3 grid grid-cols-[132px_1fr] gap-3">'
    + '<div class="relative aspect-video overflow-hidden rounded-lg bg-black">'
    + (kf ? '<img class="h-full w-full object-contain" src="/input/' + encodeURI(kf) + '" alt="关键帧">'
          : '<div class="flex h-full w-full items-center justify-center border border-dashed border-white/10 bg-black/30 text-[10px] text-slate-600">未定关键帧</div>')
    + '</div>'
    + '<div>'
    + '<textarea data-id="' + s.id + '" data-frame="' + k + '" data-field="prompt" class="h-[62px] resize-none ' + IN + '">' + esc(fr.prompt || "") + '</textarea>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">景别<select data-id="' + s.id + '" data-frame="' + k + '" data-field="shot" class="' + SEL + '">' + opts(SHOTS, fr.shot) + '</select></label>'
    + '<label class="' + LBL + '">运镜<select data-id="' + s.id + '" data-frame="' + k + '" data-field="camera" class="' + SEL + '">' + opts(CAMERAS, fr.camera) + '</select></label>'
    + '<label class="' + LBL + '">角度<select data-id="' + s.id + '" data-frame="' + k + '" data-field="angle" class="' + SEL + '">' + opts(ANGLES, fr.angle) + '</select></label>'
    + '<label class="' + LBL + '">时长(秒)<input data-id="' + s.id + '" data-frame="' + k + '" data-field="seconds" type="number" min="4" max="15" value="' + (fr.seconds || 4) + '" class="' + NUM + '"></label>'
    + '</div>'
    + '<input data-id="' + s.id + '" data-frame="' + k + '" data-field="subtitle" type="text" value="' + esc(fr.subtitle) + '" placeholder="字幕（这一帧的台词，可选）" class="mt-2 w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-300 outline-none focus:border-violet-400/50">'
    + '</div></div>'
    + '<div class="mt-3 flex items-center gap-2">'
    + '<button data-action="board-gen" data-scene="' + esc(s.id) + '" data-idx="' + k + '" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-sky-400/40 hover:bg-sky-400/10" title="用资产图当参考，出这一帧的关键帧"><iconify-icon icon="lucide:clapperboard" width="13"></iconify-icon>' + (boardBusy[boardKey(s.id, k)] ? '生成中…' : (cands.length ? '再出 4 张关键帧' : '生成关键帧')) + '</button>'
    + '<button data-action="board-seg" data-scene="' + esc(s.id) + '" data-idx="' + k + '" ' + (segReady ? '' : 'disabled ') + 'class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-violet-400/40 hover:bg-violet-400/10 disabled:opacity-40" title="' + (next ? '用这一帧和下一帧当首尾帧出这一段视频' : '最后一帧后面没有片段') + '"><iconify-icon icon="lucide:film" width="13"></iconify-icon>' + (boardBusy[boardKey(s.id, k) + "@seg"] ? '出片中…' : '出这一段') + '</button>'
    + '</div>'
    + (cands.length
        ? '<div class="mt-2 flex items-center gap-1.5 overflow-x-auto">' + cands.map(function (f) {
            var on = kf === f;
            return '<img data-action="board-pick" data-scene="' + esc(s.id) + '" data-idx="' + k + '" data-file="' + esc(f) + '" title="点这张定为这一帧的关键帧" class="h-12 w-20 shrink-0 cursor-pointer rounded-md border object-cover ' + (on ? 'border-violet-400/70' : 'border-white/10 hover:border-violet-400/40') + '" src="/input/' + encodeURI(f) + '">';
          }).join("") + '</div>'
        : '')
    + (fr.clip ? '<video class="mt-2 h-28 w-full rounded-lg bg-black object-contain" src="/output/' + encodeURI(fr.clip) + '" controls muted playsinline></video>' : '')
    + '</article>';
}

// 一个镜头一组：组头是镜头级信息与「整镜设置」，组内是它的变化帧，固定高度内自己滚。
// 拆过帧的镜头平铺开来是二十几张卡，一屏看不完两个镜头，只能靠「下一镜」一个个跳。
function shotGroupHTML(s, i) {
  var frames = s.states || [];
  var status = statusInfo(s);
  return '<section class="shot flex h-full min-h-0 flex-col rounded-xl border border-white/[0.10] bg-[#15151d] p-3 transition" data-id="' + s.id + '" data-index="' + i + '">'
    + '<div class="flex items-center gap-2">'
    + '<span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ' + status.num + ' text-xs font-bold">' + pad2(i + 1) + '</span>'
    + '<div class="min-w-0"><h4 class="truncate text-sm font-semibold">' + esc(shortTitle(s.prompt)) + '</h4>'
    + '<p class="truncate text-[10px] text-slate-500">' + esc(s.shot || "—") + ' · ' + (s.seconds || 0) + 's · '
    + frames.length + ' 个变化帧 · ' + fmtTC(shotStartSec(i)) + '</p></div>'
    + '<button data-action="board-split" data-scene="' + esc(s.id) + '" title="' + (frames.length ? "重新拆一遍变化帧（会整组替换，拆前先备份）" : "把这个镜头拆成变化帧") + '" class="ml-auto shrink-0 rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[10px] text-slate-300 transition hover:border-cyan-400/40 hover:text-cyan-200">' + (frames.length ? "重拆变化帧" : "拆变化帧") + '</button>'
    + '<span id="st_' + s.id + '" class="flex shrink-0 items-center gap-1.5 rounded-full ' + status.badge + ' px-2 py-1 text-[10px] font-semibold"><span class="h-1.5 w-1.5 rounded-full ' + status.dot + '"></span>' + status.text + '</span>'
    + '</div>'
    + sceneExtraHTML(s, i)
    + '<div class="mt-3 flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">'
    + frames.map(function (fr, k) { return frameCardHTML(s, i, k); }).join("")
    + '</div></section>';
}

// 镜头级控件：拆过帧的镜头收在组头的「整镜设置」里。data-* 和原来的镜头卡一样，
// 既有的输入/点击处理不用改。
function sceneExtraHTML(s, i) {
  var IN = 'w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-2 text-xs text-slate-300 outline-none focus:border-violet-400/50';
  var st = statusInfo(s);
  var genCls = s.status === "busy" ? "bg-amber-400/10 text-amber-200"
             : s.status === "ok" ? "border border-violet-400/30 bg-violet-400/10 text-violet-200 hover:bg-violet-400/20"
             : "border border-white/10 bg-white/[0.05] text-slate-300 hover:border-violet-400/40 hover:bg-violet-400/10";
  return '<details class="mt-3 rounded-lg border border-white/[0.07] bg-black/20">'
    + '<summary class="cursor-pointer list-none px-2.5 py-2 text-[11px] text-slate-400 transition hover:text-slate-200">整镜设置 · 参考图 / 定帧 / 音效 / 整镜出片</summary>'
    + '<div class="px-2.5 pb-2.5">'
    + '<div class="flex flex-wrap items-center gap-1.5" data-refbox="' + s.id + '">'
    + (refsHtml(s) || '<span class="text-[10px] text-slate-600">还没引用素材 —— 从下面加，顺序决定 &lt;Subject 1/2/3&gt; 是谁</span>')
    + '</div>'
    + '<select data-refadd="' + s.id + '" class="' + SEL + '" style="margin-top:6px"><option value="">+ 从素材池添加参考图…</option>' + assetOptions() + '</select>'
    + (s.refs_manual ? '<button data-action="relink" class="mt-1.5 w-full rounded-md border border-white/10 py-1 text-[10px] text-slate-400 transition hover:border-cyan-400/40 hover:text-cyan-200">这一镜手动改过引用 · 点这里交回自动连线</button>' : '')
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">主体<input data-id="' + s.id + '" data-field="subject" type="text" value="' + esc(s.subject) + '" placeholder="画面里是谁/什么" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">动作<input data-id="' + s.id + '" data-field="action" type="text" value="' + esc(s.action) + '" placeholder="在做什么" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">环境<input data-id="' + s.id + '" data-field="environment" type="text" value="' + esc(s.environment) + '" placeholder="在哪里" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">光线<input data-id="' + s.id + '" data-field="lighting" type="text" value="' + esc(s.lighting) + '" placeholder="光从哪来" class="' + INP + '"></label>'
    + '</div>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">比例<select data-id="' + s.id + '" data-field="aspect" class="' + SEL + '">' + opts(ASPECTS, s.aspect) + '</select></label>'
    + '<label class="' + LBL + '">模式<select data-id="' + s.id + '" data-field="mode" class="' + SEL + '">' + opts(MODES, s.mode) + '</select></label>'
    + '<label class="' + LBL + '">清晰度<select data-id="' + s.id + '" data-field="resolution" class="' + SEL + '">' + opts(RESOLUTIONS, s.resolution) + '</select></label>'
    + '<label class="' + LBL + '">步数<input data-id="' + s.id + '" data-field="steps" type="number" min="1" max="50" value="' + (s.steps || 25) + '" class="' + NUM + '"></label>'
    + '</div>'
    + '<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">'
    + '<label class="' + LBL + '">音效<input data-id="' + s.id + '" data-field="sfx" type="text" value="' + esc(s.sfx) + '" placeholder="现场声音" class="' + INP + '"></label>'
    + '<label class="' + LBL + '">种子<input data-id="' + s.id + '" data-field="seed" type="number" value="' + (s.seed || "") + '" placeholder="随机" class="' + NUM + '"></label>'
    + '</div>'
    + '<input data-id="' + s.id + '" data-field="negative_prompt" type="text" value="' + esc(s.negative_prompt) + '" placeholder="负向提示词（不想出现的元素，可选）" class="mt-2 ' + IN + '">'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="frame" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-cyan-400/40 hover:bg-cyan-400/10"><iconify-icon icon="lucide:image-plus" width="13"></iconify-icon>' + (s.first_frame ? '更换首帧' : '上传首帧（图+文生视频）') + '</button>'
    + (s.first_frame ? '<button data-action="frame-del" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="移除首帧">✕</button>' : '')
    + '</div>'
    + '<div class="mt-2 flex items-center gap-2">'
    + '<button data-action="ding" class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-violet-400/40 hover:bg-violet-400/10"><iconify-icon icon="lucide:sparkles" width="13"></iconify-icon>' + (s.dingBusy ? '定帧生成中…' : (shotDingCandidates(s).length ? '再定帧 4 张' : '文生图定帧（提示词出首帧）')) + '</button>'
    + '<button data-action="voice" ' + (!needAudio && videoModel !== "Wan2.2 S2V" ? 'disabled ' : '') + 'class="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] py-1.5 text-[11px] text-slate-300 transition hover:border-fuchsia-400/40 hover:bg-fuchsia-400/10 disabled:opacity-40"><iconify-icon icon="lucide:mic" width="13"></iconify-icon>' + (s.voice ? '更换配音' : '上传配音') + '</button>'
    + (s.voice ? '<button data-action="voice-del" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/10 text-slate-400 transition hover:border-red-400/40 hover:text-red-300" title="移除配音">✕</button>' : '')
    + '</div>'
    + (shotDingCandidates(s).length
        ? '<div class="mt-2 flex items-center gap-1.5 overflow-x-auto">' + shotDingCandidates(s).map(function (f) {
            var picked = shotDingFrame(s) === f;
            return '<div data-action="ding-preview" data-file="' + esc(f) + '" title="点击放大查看" class="relative h-16 w-28 shrink-0 cursor-zoom-in overflow-hidden rounded-md border ' + (picked ? 'border-violet-400/60' : 'border-white/10') + ' bg-black/40">'
              + '<img class="h-full w-full object-cover" src="/input/' + encodeURI(f) + '">'
              + '<button data-action="ding-pick" data-file="' + esc(f) + '" class="absolute inset-x-0 bottom-0 bg-black/70 py-0.5 text-[10px] ' + (picked ? 'text-violet-300' : 'text-slate-300 hover:text-white') + '">' + (picked ? '已选用 ✓' : '选用') + '</button></div>';
          }).join("") + '</div>'
        : '')
    + '<button data-action="gen" id="gen_' + s.id + '" ' + (s.status === "busy" ? "disabled" : "") + ' class="mt-3 flex w-full items-center justify-center gap-2 rounded-lg ' + genCls + ' py-2.5 text-sm font-semibold transition">' + (s.status === "busy" ? '整镜生成中…' : (s.status === "ok" ? '重新出整镜' : '整镜出片（按提示词）')) + '</button>'
    + '</div></details>';
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
  var done = scenes.filter(shotDone).length;
  var pct = total ? Math.round(done / total * 100) : 0;
  $("progressBar").style.width = pct + "%";
  $("progressText").textContent = done + " / " + total;
  $("btnConcat").disabled = !(total > 0 && done === total);
}

function selectScene(idx) {
  if (idx < 0 || idx >= scenes.length) return;
  if (idx !== curIdx) {
    // 列表一次只铺当前这一镜：换镜就得重画
    curIdx = idx;
    renderScenes();
    renderMonitor();
    return;
  }
  // 点的是当前这一镜（卡里的按钮）：只切高亮，不重画 —— 把正在操作的这张卡换掉会打断它
  var shown = document.querySelector("#scenes .shot");
  if (shown) shown.classList.add("ring-1", "ring-violet-500/60");
  renderMonitor();
  highlightTimeline();
  movePlayhead(shotStartSec(curIdx));
  syncTimelineShot();
}

function syncTimelineShot() {
  var tl = $("timelineShot");
  if (!tl) return;
  if (curIdx < 0 || curIdx >= scenes.length) { tl.textContent = "—"; return; }
  var cs = scenes[curIdx];
  tl.textContent = "镜头 " + (curIdx + 1) + " · " + fmtTC(shotStartSec(curIdx)) + "–" + fmtTC(shotStartSec(curIdx) + shotLength(cs));
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
  // 内容没变就别重建元素：重建会让浏览器取消在途请求、把已经下好的图再下一次
  var mediaSrc = s.output ? "/output/" + encodeURI(s.output)
    : s.first_frame ? "/input/" + encodeURI(s.first_frame) : "";
  if (vid && mediaSrc && vid.getAttribute("data-src") !== mediaSrc) {
    vid.setAttribute("data-src", mediaSrc);
    vid.innerHTML = s.output
      ? '<video class="h-full w-full object-cover" src="' + mediaSrc + '" muted playsinline></video>'
      : '<img class="h-full w-full object-cover" src="' + mediaSrc + '" alt="首帧">';
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
    first_frame: null, use_character_frame: false, second_pass: false, output: null,
  };
  scenes.push(s);
  curIdx = scenes.length - 1;
  renderScenes(); renderMonitor(); syncProgress();
  scheduleSaveState();
  toast("已新增镜头 " + (max + 1), "ok");
}

async function optimize() {
  var text = $("script").value.trim();
  if (!docxB64 && !text && docxFilename) {
    toast("文档读取中，请稍候…", "ok");
    await docxReading;
  }
  if (!docxB64 && !text) { toast("请先粘贴脚本或上传文档", "err"); return; }
  var useAI = $("optAI") ? $("optAI").checked : true;
  var btn = $("btnOptimize");
  btn.disabled = true;
  btn.innerHTML = '<iconify-icon class="animate-spin" icon="lucide:loader-circle" width="16"></iconify-icon>' + (useAI ? "AI 解析中…" : "解析中…");
  try {
    var append = $("optAppend") ? $("optAppend").checked : false;
    var payload = docxB64 ? { docx_b64: docxB64, filename: docxFilename, append: append, ai: useAI } : { text: text, append: append, ai: useAI };
    // 这里就是「拆分镜」agent，模型跟着「设置 → Agent 模型」里那一行走，不再单独配
    if (useAI) payload.agent_models = settings.agent_models || {};
    var r = await api("/api/optimize", payload);
    if (!r.ok) { toast("解析失败：" + (r.error || "未知错误"), "err"); return; }
    var newScenes = (r.scenes || []).map(function (s) { s.output = s.output || null; s.status = "pending"; return s; });
    scenes = append ? scenes.concat(newScenes) : newScenes;
    // 拆镜这一步会通读全文把实体认出来塞进资产池：链接、画布上的资产卡要立刻用上
    if (r.assets) { assets = r.assets; refreshAssetSlots(); }
    docxB64 = null; docxFilename = "";
    $("docxName").textContent = "";
    $("btnReset").classList.remove("hidden");
    $("btnReset").classList.add("flex");
    curIdx = scenes.length ? (append ? scenes.length - newScenes.length : 0) : -1;
    $("lastSaved").textContent = (append ? "已追加 " : "已解析 ") + newScenes.length + " 个镜头";
    renderScenes(); renderMonitor(); syncProgress();
    $("importPanel").classList.add("hidden");
    var label = r.engine_name || (r.engine === "ai" ? "AI 分镜" : "规则拆分");
    if (r.images) label += "，自动带入 " + r.images + " 张分镜图";
    if (r.assets_added) label += "，认出 " + r.assets_added + " 个实体（已进资产池并自动连线）";
    toast((append ? "已追加 " : "解析完成，共 ") + newScenes.length + " 个镜头（" + label + "）", "ok", r.assets_added ? 9000 : 4000);
    if (r.note) toast("AI 拆镜没成功，已按格式规则拆分：" + r.note, "err", 9000);
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
  s.cancelled = false;
  s.startedAt = Date.now();
  s.run = (s.run || 0) + 1;
  var run = s.run;
  setSceneStatus(id);
  if (!window._hintShown) {
    window._hintShown = true;
    toast("已提交生成。速度档位：极速(4步)最快 · 均衡(12步)快约一倍、画质接近标准 · 标准(25步)最稳。5 秒视频按档位约 2~20 分钟，GPU 在跑就是在动", "ok", 12000);
  }
  try {
    syncScene(id);
    syncRefEntries(s);
    var r = await api("/api/generate", {
      scene: { id: s.id, prompt: s.prompt, seconds: s.seconds, subtitle: s.subtitle, shot: s.shot, camera: s.camera, aspect: s.aspect, resolution: s.resolution, mode: s.mode, steps: s.steps, seed: s.seed, negative_prompt: s.negative_prompt || "", first_frame: linkedFirstFrame(s), use_character_frame: !!s.use_character_frame, ref_images: s.ref_images || [], global_negative_prompt: globalNeg, model: videoModel, voice: s.voice || null, guides: s.guides || [], auto_match: !!s.auto_match, second_pass: !!s.second_pass, style_suffix: settings.style_suffix, need_audio: needAudio, chain_frames: chainFrames }
    });
    if (run !== s.run) return;   // 这一轮已被取消并重新提交，状态归新一轮管，别把它改回待生成
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
    else if (r.busy) { s.status = "pending"; toast(r.error, "err", 9000); }
    else { s.status = "err"; toast("镜头 " + s.id + " 生成失败：" + r.error, "err"); }
  } catch (e) {
    if (run !== s.run) return;
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

function pickVideoFile() {
  return new Promise(function (resolve) {
    var inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.mkv,.webm";
    inp.onchange = function () { resolve((inp.files && inp.files[0]) || null); };
    inp.click();
  });
}

function fileToB64(file) {
  return new Promise(function (resolve, reject) {
    var rd = new FileReader();
    rd.onload = function () { resolve(String(rd.result).split(",")[1] || ""); };
    rd.onerror = function () { reject(new Error("读取视频文件失败")); };
    rd.readAsDataURL(file);
  });
}

async function continueVideo(id, ownOutput) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  if (videoModel !== "MiniMax H3") { toast("续接视频走 H3 首帧链路，请先把顶部模型切到 MiniMax H3", "err", 6000); return; }
  if (s.status === "busy") { toast("该镜头正在生成中，请等它完成再续接", "err"); return; }
  syncScene(id);
  var video = ownOutput || null;
  try {
    if (!video) {
      var f = await pickVideoFile();
      if (!f) return;
      var up = await api("/api/upload_video", { data_b64: await fileToB64(f), ext: (f.name.split(".").pop() || "mp4").toLowerCase() });
      if (!up.ok) { toast("视频上传失败：" + (up.error || ""), "err", 8000); return; }
      video = up.file;
    }
    var r = await api("/api/continue_video", { scene_id: id, video: video, prompt: s.prompt, seconds: s.seconds, mode: s.mode, seed: s.seed });
    if (!r.ok) { toast("续接失败：" + (r.error || ""), "err", 9000); return; }
    var ns = r.scene;
    ns.output = ns.output || null;
    ns.status = "pending";
    var idx = scenes.findIndex(function (x) { return x.id === id; });
    scenes.splice(idx + 1, 0, ns);
    renderScenes(); syncProgress();
    toast("已插入续接镜头（首帧=原视频尾帧），开始生成…", "ok", 8000);
    generateScene(ns.id);
  } catch (e) {
    toast("续接请求失败：" + e.message, "err", 9000);
  }
}

async function dingFrame(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  if (s.dingBusy) { toast("定帧正在生成中，请稍候", "err"); return; }
  syncScene(id);
  s.dingBusy = true;
  renderScenes();
  toast("文生图定帧生成中（SDXL 出 4 张，约 1~3 分钟）…", "ok", 6000);
  try {
    var r = await api("/api/ding_frame", {
      batch: 4,
      scene: { id: s.id, prompt: s.prompt, seconds: s.seconds, subtitle: s.subtitle, shot: s.shot, camera: s.camera, aspect: s.aspect, resolution: s.resolution, mode: s.mode, steps: s.steps, seed: s.seed, negative_prompt: s.negative_prompt || "", global_negative_prompt: globalNeg }
    });
    if (r.ok) {
      s.ding_candidates = r.files;
      renderScenes();
      toast("已出 " + r.files.length + " 张定帧图，点缩略图放大挑选，或直接点图下方「选用」", "ok", 8000);
      scheduleSaveState();
    } else {
      toast("定帧失败：" + (r.error || "未知错误"), "err", 9000);
    }
  } catch (e) {
    toast("定帧请求失败：" + e.message, "err");
  }
  s.dingBusy = false;
  renderScenes();
}

async function keyframeScene(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  if (s.kfBusy) { toast("关键帧正在生成中，请稍候", "err"); return; }
  syncScene(id);
  s.kfBusy = true;
  renderScenes();
  var refName = (character && character.image) ? "角色设定图" : ((scene && scene.image) ? "场景设定图" : "");
  toast(refName
    ? "故事版关键帧生成中（Flux Kontext 以" + refName + "为参考，约 1~4 分钟）…"
    : "故事版关键帧生成中（没有资产图，走纯文生图；建议先在全局设置里生成角色/场景设定图）…", "ok", 8000);
  try {
    var r = await api("/api/keyframe", {
      batch: 2,
      scene: { id: s.id, prompt: s.prompt, seconds: s.seconds, subtitle: s.subtitle, shot: s.shot, camera: s.camera, aspect: s.aspect, resolution: s.resolution, mode: s.mode, steps: s.steps, seed: s.seed, negative_prompt: s.negative_prompt || "", global_negative_prompt: globalNeg }
    });
    if (r.ok) {
      s.ding_candidates = r.files;
      renderScenes();
      toast("已出 " + r.files.length + " 张关键帧，点图下方「选用」定为首帧", "ok", 8000);
      scheduleSaveState();
    } else {
      toast("关键帧失败：" + (r.error || "未知错误"), "err", 9000);
    }
  } catch (e) {
    toast("关键帧请求失败：" + e.message, "err");
  }
  s.kfBusy = false;
  renderScenes();
}

async function pickDing(id, file) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  try {
    var r = await api("/api/pick_ding", { scene_id: id, file: file });
    if (!r.ok) { toast("选用失败：" + (r.error || ""), "err"); return; }
    s.first_frame = r.first_frame;
    s.ding_frame = r.first_frame;
    renderScenes();
    if (scenes[curIdx] && scenes[curIdx].id === id) renderMonitor();
    scheduleSaveState();
    toast("首帧已就绪：生成按这张定帧图走，合成过渡也会平滑落到这张图", "ok");
  } catch (e) { toast("选用失败：" + e.message, "err"); }
}

// ---- 大图查看器 --------------------------------------------------------------------
//
// 定帧候选条、画布上的关键帧、宫格里的每一格、资产底图，双击都进这一个查看器：左右翻页，
// 「用这一张」的动作随来源变（设为首帧 / 设为这一帧的关键帧 / 设为底图 / 设为当前版本），
// 没有动作就藏按钮。以前这套只服务定帧条，画布上的图只能缩在很小的卡片里看。
var previewState = null;   // { files, file, meta, onUse, useText }

function previewSrc(f) {
  return (/.(mp4|webm|mov|mkv)$/i.test(f) ? "/output/" : "/input/") + encodeURI(f);
}

function openImagePreview(files, file, meta, onUse, useText) {
  var list = (files || []).filter(Boolean);
  if (!file) file = list[0];
  if (!file) return;
  if (list.indexOf(file) < 0) list = [file].concat(list);
  previewState = { files: list, file: file, meta: meta || "",
                   onUse: onUse || null, useText: useText || "用这一张" };
  renderImagePreview();
  $("dingModal").classList.remove("hidden");
}

function closeImagePreview() {
  previewState = null;
  $("dingModal").classList.add("hidden");
  $("dingModalImg").removeAttribute("src");
}

function shiftImagePreview(step) {
  var st = previewState;
  if (!st || st.files.length < 2) return;
  st.file = st.files[(st.files.indexOf(st.file) + step + st.files.length) % st.files.length];
  renderImagePreview();
}

function renderImagePreview() {
  var st = previewState;
  if (!st) return;
  $("dingModalImg").src = previewSrc(st.file);
  $("dingModalMeta").textContent = st.meta;
  $("btnDingPrev").disabled = st.files.length < 2;
  $("btnDingNext").disabled = st.files.length < 2;
  $("btnDingUse").classList.toggle("hidden", !st.onUse);
  $("btnDingUseText").textContent = (typeof st.useText === "function") ? st.useText(st.file) : st.useText;
}

async function useImagePreview() {
  var st = previewState;
  if (!st || !st.onUse) return;
  await st.onUse(st.file);
  renderImagePreview();
}

function dingCandidatesOf(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  return s ? shotDingCandidates(s) : [];
}

function openDingPreview(id, file) {
  var s = scenes.find(function (x) { return x.id === id; });
  var list = dingCandidatesOf(id);
  if (!s || list.indexOf(file) < 0) return;
  openImagePreview(list, file,
    "第 " + (scenes.indexOf(s) + 1) + " 镜 · 第 " + (list.indexOf(file) + 1) + " / " + list.length + " 张",
    function (f) { return pickDing(id, f); },
    function (f) { return shotDingFrame(s) === f ? "已选用 ✓" : "选用为首帧"; });
}

// 画布上双击一张图 → 放大。翻页范围与「用这一张」按节点类型定：
// 资产卡＝设为底图，宫格＝设为当前版本，变化帧＝设为这一帧的关键帧，整镜＝设为首帧。
function openBoardPreview(node, rel) {
  if (node.batch) {
    var b = node.batch, files = (b.files || []).slice();
    if (!files.length) return;
    openImagePreview(files, rel,
      (BATCH_KIND_LABEL[b.kind || ""] || "生成") + " · 第 " + (files.indexOf(rel) + 1) + " / " + files.length + " 格",
      function (f) { return applyVersion(node.parent.scene.id, node.parent.idx === null ? "" : node.parent.idx, node.field, f); },
      "设为当前版本");
    return;
  }
  if (node.kind) {
    var a = node.asset || {};
    var alist = [a.file].concat(a.candidates || []).filter(Boolean);
    if (!alist.length) return;
    openImagePreview(alist, rel,
      ((ASSET_META[a.kind] || {}).label || "资产") + " · " + (a.name || "未命名") + " · 第 " + (alist.indexOf(rel) + 1) + " / " + alist.length + " 张",
      function (f) { return pickAssetImage(a.id, f); }, "设为底图");
    return;
  }
  var s = node.scene, x = node.state;
  var cur = x ? x.keyframe : (s.ding_frame || s.first_frame);
  var list = ((x ? x.candidates : s.ding_candidates) || []).slice();
  if (cur && list.indexOf(cur) < 0) list = [cur].concat(list);
  if (!list.length) return;
  var title = "镜" + (scenes.indexOf(s) + 1)
    + (node.idx === null ? " · 整镜" : " · 变化帧 " + (node.idx + 1) + "/" + (s.states || []).length);
  openImagePreview(list, rel,
    title + " · 第 " + (list.indexOf(rel) + 1) + " / " + list.length + " 张",
    function (f) { return boardPickKeyframe(s.id, node.idx === null ? "" : node.idx, f); },
    node.idx === null ? "设为首帧" : "设为这一帧的关键帧");
}

function boardNodeByKey(key) {
  var hit = null;
  boardAllNodes().forEach(function (n) { if (n.key === key) hit = n; });
  return hit;
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
  renderSceneImage();
}

function renderCharImage() {
  $("charImageEmpty").classList.toggle("hidden", !!character.image);
  var img = $("charImagePreview");
  if (character.image) { img.src = "/input/" + encodeURI(character.image); img.classList.remove("hidden"); }
  else { img.classList.add("hidden"); img.removeAttribute("src"); }
}

function renderSceneImage() {
  $("sceneImageEmpty").classList.toggle("hidden", !!scene.image);
  var img = $("sceneImagePreview");
  if (scene.image) { img.src = "/input/" + encodeURI(scene.image); img.classList.remove("hidden"); }
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
    var r2 = await api("/api/scene", { name: scene.name, description: scene.description, image: scene.image });
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

function uploadSceneImage() {
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
        var r = await api("/api/upload_frame", { scene_id: "scene", data_b64: b64, ext: ext });
        if (!r.ok) { toast("上传失败：" + (r.error || ""), "err"); return; }
        scene.image = r.file;
        await api("/api/scene", { name: scene.name, description: scene.description, image: scene.image });
        renderSceneImage();
        renderScenes();
        toast("场景参考图已保存，可在镜头里勾选「场景参考」", "ok");
      } catch (e) { toast("上传失败：" + e.message, "err"); }
    };
    reader.readAsDataURL(f);
  };
  inp.click();
}

function delSceneImage() {
  scene.image = null;
  api("/api/scene", { name: scene.name, description: scene.description, image: null });
  renderSceneImage();
  renderScenes();
  toast("已移除场景参考图", "ok");
}

// 资产分三类。工业场景里最先要锁住的是「设备」，角色是次要的
var ASSET_META = {
  equipment: { label: "设备资产", short: "设备/产品", icon: "factory", hint: "设备或产品的外形、结构、材质、颜色、铭牌等可辨识细节" },
  scene: { label: "场景资产", short: "场景", icon: "image", hint: "车间/产线/办公场所等空间：布局、设备摆位、材质、光源" },
  character: { label: "角色资产", short: "角色", icon: "user-round", hint: "人物外貌与服装（工业解说片里通常是出镜讲解者）" },
};
var ASSET_LABEL = { character: "角色设定图", scene: "场景设定图", equipment: "设备设定图" };
var ASSET_KINDS = Object.keys(ASSET_META);
var assetBusy = { character: false, scene: false, equipment: false };
// 资产六视图：上/下/前/后/左/右，由底图图生图得到；设备/产品用这个最合适，相当于六面图
var VIEW_ORDER = [["front", "前"], ["back", "后"], ["left", "左"], ["right", "右"], ["top", "上"], ["bottom", "下"]];
var assetViewBusy = { character: false, scene: false, equipment: false };
var assetViewLabel = { character: "", scene: "", equipment: "" };
var assetViewTimer = null;

async function genAssetViews(assetId, views) {
  var o = assetById(assetId);
  if (!o) { toast("资产不存在", "err"); return; }
  if (assetViewBusy[assetId]) { toast("六视图正在生成中", "err"); return; }
  if (!o.file) { toast("六视图是图生图：先给这条资产出一张底图，或点缩略图选一张", "err", 8000); return; }
  var one = (views && views.length === 1) ? views[0] : "";
  if (!confirm(one
      ? "只重出这一张（" + one + "）？约 1.5~2 分钟。"
      : "用当前底图生成六视图（上/下/前/后/左/右），每张约 1.5~2 分钟，共约 10 分钟。继续？")) return;
  assetViewBusy[assetId] = true;
  assetViewLabel[assetId] = "启动中…";
  renderBoard();
  try {
    var r = await api("/api/asset_views", { asset_id: assetId, steps: 20, views: views || null });
    if (!r.ok) { toast("启动失败：" + (r.error || ""), "err", 9000); assetViewBusy[assetId] = false; renderBoard(); return; }
  } catch (e) { toast("启动失败：" + e.message, "err"); assetViewBusy[assetId] = false; renderBoard(); return; }
  if (assetViewTimer) clearInterval(assetViewTimer);
  assetViewTimer = setInterval(async function () {
    try {
      var resp = await api("/api/asset_views_status");
      var s = (resp && resp.status) || {};
      var mine = String(s.asset_id || "") === String(assetId);
      assetViewLabel[assetId] = (s.running && mine) ? (s.current || "生成中") + " " + s.done + "/" + s.total : "";
      if (!s.running) {
        clearInterval(assetViewTimer);
        assetViewBusy[assetId] = false;
        if (mine) {
          o.views = Object.assign({}, o.views || {}, s.views || {});
          if (s.stage === "error") toast("六视图失败：" + (s.error || ""), "err", 12000);
          else toast("六视图完成，连线上的镜头已经用上新视图", "ok", 8000);
          scheduleSaveAssets();
        }
      }
      renderBoard();
    } catch (e) { clearInterval(assetViewTimer); assetViewBusy[assetId] = false; renderBoard(); }
  }, 5000);
}

function assetTarget(assetId) { return assetById(assetId); }

// 全局设置面板读的还是 character/scene/equipment 三个槽位，它们就是池子里 id 与类型同名的
// 那三条资产。改完资产同步一下，面板和画布才不会一个显示新图一个显示旧图。
function refreshAssetSlots() {
  ["character", "scene", "equipment"].forEach(function (k) {
    var a = assetById(k) || {};
    var slot = { name: a.name || "", description: a.description || "", image: a.file || null,
                 candidates: a.candidates || [], views: a.views || {} };
    if (k === "character") character = slot;
    else if (k === "scene") scene = slot;
    else equipment = slot;
  });
}

// 全局设置面板里只有角色/场景两块；设备资产的入口在画布上，所以这里要容忍元素不存在
var ASSET_PANEL = { character: ["btnCharGen", "btnCharGenLabel", "charCandRow"],
                    scene: ["btnSceneGen", "btnSceneGenLabel", "sceneCandRow"] };

function renderAsset(kind) {
  var ids = ASSET_PANEL[kind];
  if (!ids) return;
  var a = assetTarget(kind) || {};
  var meta = ASSET_META[kind];
  var busy = assetBusy[kind];
  var btn = $(ids[0]);
  if (btn) {
    btn.disabled = busy;
    btn.classList.toggle("opacity-50", busy);
    $(ids[1]).textContent = busy ? "生成中…" : (a.file ? "重新生成设定图" : "AI 生成设定图");
  }
  var files = a.candidates || [];
  var row = $(ids[2]);
  if (!row) return;
  row.className = "mt-3 gap-1.5 overflow-x-auto " + (files.length ? "flex" : "hidden");
  row.innerHTML = files.map(function (f) {
    var picked = a.file === f;
    return '<button data-action="asset-pick" data-kind="' + kind + '" data-file="' + esc(f) + '" title="选用这张作' + meta.short + '参考图" class="relative h-20 w-16 shrink-0 overflow-hidden rounded-md border ' + (picked ? "border-violet-400/60" : "border-white/10 hover:border-violet-400/40") + ' bg-black/40">'
      + '<img class="h-full w-full bg-black/40 object-contain" src="/input/' + encodeURI(f) + '" loading="lazy">'
      + '<span class="absolute inset-x-0 bottom-0 bg-black/70 py-0.5 text-center text-[10px] ' + (picked ? "text-violet-300" : "text-slate-300") + '">' + (picked ? "已选用 ✓" : "选用") + '</span></button>';
  }).join("");
}

async function genAssetImage(assetId) {
  var a = assetById(assetId);
  if (!a) { toast("资产不存在", "err"); return; }
  if (!a.description) { toast("请先填写" + (ASSET_META[a.kind] || {}).label + "的描述", "err"); return; }
  if (assetBusy[assetId]) return;
  assetBusy[assetId] = true;
  renderAsset(a.kind);
  toast("正在用 SDXL 生成" + ASSET_LABEL[a.kind] + "（4 张，约 1-2 分钟）…", "ok", 6000);
  try {
    var r = await api("/api/asset_image", { asset_id: assetId, batch: 4 });
    if (!r.ok) { toast("生成失败：" + (r.error || ""), "err", 9000); return; }
    a.candidates = r.files;
    toast(ASSET_LABEL[a.kind] + "已出 4 张，点缩略图选一张", "ok", 6000);
  } catch (e) { toast("生成失败：" + e.message, "err", 9000); }
  assetBusy[assetId] = false;
  renderAsset(a.kind);
  renderBoard();
}

async function pickAssetImage(assetId, file) {
  var a = assetById(assetId);
  if (!a) return;
  try {
    var r = await api("/api/pick_asset", { asset_id: assetId, file: file });
    if (!r.ok) { toast("选用失败：" + (r.error || ""), "err"); return; }
    a.file = r.image;
    applyLinkedScenes(r.scenes);
    if (a.kind === "character") renderCharImage();
    else if (a.kind === "scene") renderSceneImage();
    refreshAssetSlots();
    renderAsset(a.kind);
    renderScenes();
    toast(ASSET_META[a.kind].label + "已设为底图，连线上的镜头参考图同步更新", "ok");
  } catch (e) { toast("选用失败：" + e.message, "err"); }
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

async function cancelCurrent() {
  var busy = scenes.filter(function (s) { return s.status === "busy"; });
  if (!busy.length) { toast("没有正在生成的任务", "ok"); return; }
  // 先在本页把镜头放回待生成，后端 /api/cancel 会中断 ComfyUI 里的 prompt 并立刻放开生成位，
  // 这样取消完就能重新点生成，不用等后台轮询线程收尾
  busy.forEach(function (s) {
    s.cancelled = true;
    s.status = "pending";
    s.startedAt = null;
    setSceneStatus(s.id);
  });
  try {
    var r = await api("/api/cancel", {});
    if (r && r.cancelled) {
      // stopped=false 说明 ComfyUI 那边没接受中断（连不上/被拒），别报「已取消」骗人
      toast(r.stopped ? "已取消「" + r.scene + "」的生成，可以重新点生成" : "已放开生成位，但没能让 ComfyUI 中断：去 8188 队列看一眼", r.stopped ? "ok" : "err", r.stopped ? 4000 : 7000);
    } else {
      toast(r && r.stopped ? "已中断 ComfyUI 里正在跑的生成" : "ComfyUI 里没有在跑的生成任务", "ok", 4000);
    }
  } catch (e) {
    toast("取消失败：" + e.message, "err");
  }
}

// 给一个镜头的每一帧补齐关键帧：从左到右，后一帧以上一帧为编辑源（脸和背景接得住），
// 每帧出图后自动取第一张 —— 批量流程不让人挑图，和「文生图」agent 一个口径。
// 没有变化帧的镜头补的是整镜那张（定帧/首帧）。返回是否补齐。
async function ensureShotKeyframes(s) {
  var frames = s.states || [];
  if (!frames.length) {
    if (s.ding_frame || s.first_frame) return true;
    var made = await boardGenKeyframe(s.id, "");
    if (made && made.length) await boardPickKeyframe(s.id, "", made[0]);
    return !!(s.ding_frame || s.first_frame);
  }
  for (var i = 0; i < frames.length; i++) {
    if (frames[i].keyframe) continue;
    var files = await boardGenKeyframe(s.id, i);
    if (files && files.length) await boardPickKeyframe(s.id, i, files[0]);
    if (!((s.states[i] || {}).keyframe)) return false;
  }
  return true;
}

// 一键生成关键帧：所有镜头跑一遍 ensureShotKeyframes，不生成片段。
// 想先看静帧、再来决定哪一段要不要重出时用它。
async function genAllKeyframes() {
  var todo = scenes.filter(function (s) { return (s.prompt || "").trim(); });
  if (!todo.length) { toast("还没有镜头", "err"); return; }
  var need = todo.filter(function (s) {
    var frames = s.states || [];
    return frames.length ? frames.some(function (f) { return !f.keyframe; }) : !(s.ding_frame || s.first_frame);
  });
  if (!need.length) { toast("关键帧都齐了", "ok"); return; }
  if (!confirm("给 " + need.length + " 个镜头补齐关键帧？缺哪帧出哪帧，出的图自动取第一张，单张约 2~4 分钟。")) return;
  $("btnGenKfAll").disabled = true;
  $("btnStop").classList.remove("hidden");
  $("btnStop").classList.add("flex");
  $("btnPause").classList.remove("hidden");
  $("btnPause").classList.add("flex");
  genAllStop = false;
  genAllPaused = false;
  setPauseButton();
  var failed = 0;
  try {
    for (var i = 0; i < need.length; i++) {
      if (genAllStop) break;
      while (genAllPaused) { await waitResume(); }
      if (!(await ensureShotKeyframes(need[i]))) failed += 1;
    }
    toast(genAllStop ? "已停止" : (failed ? "关键帧生成结束：" + failed + " 镜没出全，看上面提示" : "关键帧已补齐"),
          failed ? "err" : "ok", failed ? 9000 : 4000);
  } finally {
    $("btnGenKfAll").disabled = false;
    $("btnStop").classList.add("hidden");
    $("btnStop").classList.remove("flex");
    $("btnPause").classList.add("hidden");
    $("btnPause").classList.remove("flex");
    genAllStop = false;
    genAllPaused = false;
  }
}

async function generateAll() {
  var pending = scenes.filter(function (s) { return !shotDone(s); });
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
    var stuck = [];
    for (var i = 0; i < pending.length; i++) {
      if (genAllStop) break;
      while (genAllPaused) { await waitResume(); }
      var s = pending[i];
      // 拆过变化帧的镜头按「相邻两帧一段」补齐，没拆的照旧整镜出片 —— 和画布、整板出片同一套账
      var segs = shotSegments(s);
      if (!segs.length) {
        await generateScene(s.id);
      } else {
        // 「生成全部」= 把这一镜的帧全部连起来：先把缺的帧图补齐，再把相邻两帧连成一段
        await ensureShotKeyframes(s);
        var frames = s.states || [];
        for (var k = 0; k < segs.length; k++) {
          if (genAllStop) break;
          while (genAllPaused) { await waitResume(); }
          if (!frames[k] || !frames[k + 1] || !frames[k].keyframe || !frames[k + 1].keyframe) continue;
          if (frames[k].clip) continue;
          await boardGenSegment(s.id, k);
        }
      }
      if (!genAllStop && !shotDone(s)) stuck.push("镜" + (scenes.indexOf(s) + 1));
    }
    toast(genAllStop ? "已停止" : (stuck.length ? "批量生成结束，" + stuck.join("、") + " 没出成（多半缺关键帧）" : "批量生成结束"),
          stuck.length ? "err" : "ok", stuck.length ? 9000 : 4000);
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
  if (!scenes.length || !scenes.every(shotDone)) { toast("还有镜头未生成", "err"); return; }
  // 拆过变化帧的镜头按段拼（和整板出片一致），没拆的用整镜成片
  var clips = [];
  scenes.forEach(function (s) { clips = clips.concat(shotClips(s)); });
  var subtitles = [];
  var acc = 0;
  scenes.forEach(function (s, i) {
    if (subtitleOn && s.subtitle && String(s.subtitle).trim()) {
      subtitles.push({ text: String(s.subtitle).trim(), start: acc, end: acc + shotLength(s) });
    }
    acc += shotLength(s) + (bridgeOn && i < scenes.length - 1 ? BRIDGE_SEC : 0);
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
    var misses = 0;
    var timer = setInterval(async function () {
      var d;
      try { d = await api("/api/concat_status"); misses = 0; }
      catch (e) {
        // 导播台被关掉或重启时状态查不到，别让按钮一直转圈显示"进行中"
        if (++misses >= 3) {
          clearInterval(timer);
          toast("查不到合成状态（导播台可能已重启或关闭），请刷新页面后重新合成", "err", 8000);
          done("合成成片");
        }
        return;
      }
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

function removeState(sceneId, idx) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s || !s.states || !s.states[idx]) return;
  var st = s.states[idx];
  var held = [];
  if (st.keyframe) held.push("关键帧");
  if (st.candidates && st.candidates.length) held.push("候选图");
  if (st.clip) held.push("这一段视频");
  if (held.length && !confirm("这个变化帧上已经挂了" + held.join("、")
      + "。删除后画布不再引用它们（文件仍留在磁盘上）。确定删除？")) return;

  // 段挂在起始帧上：第 idx 帧没了，它前面那帧到它的那一段也接不上了
  if (s.states[idx - 1]) delete s.states[idx - 1].clip;
  s.states.splice(idx, 1);

  // 位置是按「镜头#帧号」存的，后面的帧号整体前移一格；不搬位置，整列会跳号错位
  var pre = sceneId + "#";
  Object.keys(boardPos).forEach(function (k) {
    if (k.indexOf(pre) !== 0) return;
    var n = parseInt(k.slice(pre.length), 10);
    if (isNaN(n) || n < idx) return;
    var v = boardPos[k];
    delete boardPos[k];
    if (n > idx) boardPos[pre + (n - 1)] = v;
  });
  renderScenes(); renderMonitor(); syncProgress();
  scheduleSaveState();
  // 删到一帧不剩就等于撤销拆分：这一镜退回成单帧节点，随时可以重新拆
  toast(s.states.length ? "已删除变化帧 " + (idx + 1) + "，这一镜还剩 " + s.states.length + " 帧"
                        : "已撤销拆分，这一镜恢复成单帧", "ok", 5000);
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
  scenes = []; curIdx = -1; curPage = 0; docxB64 = null; docxFilename = "";
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

// 页面加载时记下这一页的前端版本；之后每半分钟对一次，服务端换了 app.js 就把徽标变成
// 「点这里刷新」。不然改完前端，界面上看到的还是旧的那一版，只能靠人记得刷新。
var pageBuild = null;

async function loadBuildTag() {
  try {
    var r = await api("/api/version");
    var el = $("buildTag");
    if (!el || !r.ok) return;
    if (!pageBuild) pageBuild = r.js;
    var bar = $("staleBar");
    if (r.js !== pageBuild) {
      el.className = "cursor-pointer rounded-md border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-200 transition hover:bg-amber-400/20";
      el.textContent = "前端已更新 " + r.js + " · 点这里刷新";
      el.title = "这一页加载的是 " + pageBuild + " 的前端，服务端已经是 " + r.js;
      el.onclick = function () { location.reload(); };
      // 徽标太小、容易看不见：顶上再横一条横幅，跑着旧前端这件事不能靠人自己发现
      if (bar) {
        $("staleBarOld").textContent = pageBuild;
        $("staleBarNew").textContent = r.js;
        bar.classList.remove("hidden");
        bar.classList.add("flex");
        $("staleBarBtn").onclick = function () { location.reload(); };
      }
      return;
    }
    if (bar) { bar.classList.add("hidden"); bar.classList.remove("flex"); }
    el.textContent = "前端 " + r.js + " · 后端 " + r.py;
  } catch (e) {}
}

async function restoreState() {
  try {
    var r = await api("/api/state");
    if (r.ok) {
      character = r.character || { name: "", description: "", image: null };
      scene = r.scene || { name: "", description: "", image: null };
      equipment = r.equipment || { name: "", description: "", image: null };
      globalNeg = r.global_negative_prompt || "";
      boardPos = r.board || {};
      assets = r.assets || [];
      // 后端已做过单例升级；这里再兜一次，保证老项目首次打开也有东西可引用
      if (!assets.length) { assets = fallbackAssets(); scheduleSaveAssets(); }
      refreshAssetSlots();
      boardDomMap = {};
      if ($("boardNodes")) $("boardNodes").innerHTML = "";
      if (r.project && r.project.name) {
        var pn = $("projectName");
        if (pn) { pn.textContent = r.project.name; pn.classList.remove("hidden"); }
        document.title = r.project.name + " · 导演台";
      }
      syncGlobalButton();
      renderAsset("character");
      renderAsset("scene");
    }
    if (r.ok && r.scenes && r.scenes.length) {
      scenes = r.scenes.map(function (s) {
        s.shot = s.shot || "中景";
        s.seconds = clampInt(s.seconds, 3, 15, 5);
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
        s.second_pass = !!s.second_pass;
        // 逐镜引用：有序的素材 id 列表，下标即 <Subject N> 编号。
        // 显式 refs 非空时不再跑 auto_match，否则自动挑的图会插在用户排好的顺序中间。
        s.refs = s.refs || [];
        if (s.refs.length) s.auto_match = false;
        s.status = s.output ? "ok" : "pending";
        return s;
      });
      curIdx = 0;
      curPage = 0;
      $("btnReset").classList.remove("hidden");
      $("btnReset").classList.add("flex");
      $("lastSaved").textContent = "已恢复 " + scenes.length + " 个镜头";
      renderScenes(); renderMonitor(); syncProgress();
      resumeActiveGen();
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

// 把一个镜头填的字段交给语言模型，写回该镜的画面描述。
// 景别/运镜不参与正文：compose_prompt() 会在末尾自己追加它们，写了就会重复一遍。
async function compileShot(id) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  syncScene(id);
  var btn = document.querySelector('.shot[data-id="' + id + '"] [data-action="compile"]');
  if (btn) { btn.disabled = true; btn.textContent = "编译中…"; }
  try {
    var r = await api("/api/compile_shot", { scene: s, agent_models: settings.agent_models || {} });
    if (r.ok && r.prompt) {
      s.prompt = r.prompt;
      var ta = document.querySelector('textarea[data-id="' + id + '"][data-field="prompt"]');
      if (ta) ta.value = r.prompt;
      scheduleSaveState();
      renderTimeline();
      toast("已编译 · 镜头 " + (scenes.indexOf(s) + 1), "ok");
    } else {
      toast("编译失败：" + (r.error || ""), "err");
    }
  } catch (e) {
    toast("编译失败：" + e.message, "err");
  } finally {
    var b = document.querySelector('.shot[data-id="' + id + '"] [data-action="compile"]');
    if (b) {
      b.disabled = false;
      b.innerHTML = '<iconify-icon icon="lucide:sparkles" width="14"></iconify-icon>编译提示词';
    }
  }
}

// ---- 素材池与逐镜引用 ------------------------------------------------------------
//
// scene.refs 是「哪些素材、什么顺序」。顺序是核心：_build_ref_prompt 按这个顺序生成
// <Subject 1>..<Subject N>，ref2va 也按同样顺序接 ref_images。所以下标就是引用编号，
// 和 LibTV「连线顺序即引用顺序」同构。

function fallbackAssets() {
  var out = [];
  [["character", character], ["scene", scene], ["equipment", equipment]].forEach(function (p) {
    var slot = p[1] || {};
    var name = (slot.name || "").trim(), desc = (slot.description || "").trim();
    if (slot.image || name || desc) {
      out.push({ id: p[0], kind: p[0], name: name, description: desc,
                 file: slot.image || null, tags: [] });
    }
  });
  return out;
}

function assetById(id) {
  return assets.filter(function (a) { return String(a.id) === String(id); })[0] || null;
}

function assetLabel(a) {
  return (ASSET_META[a.kind] ? ASSET_META[a.kind].label : a.kind) + (a.name ? " · " + a.name : "");
}

// 卡片上按顺序渲染引用徽章。<Subject N> 就是这条引用的编号，删一条后面的会自动重排。
function refsHtml(s) {
  var list = refEntries(s);
  if (!list.length) return "";
  var h = "";
  list.forEach(function (a, i) {
    h += '<span class="flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.05] px-1.5 py-1 text-[10px] text-slate-300">'
      + '<b class="text-cyan-300">&lt;Subject ' + (i + 1) + '&gt;</b>' + esc(a.name || a.id)
      + (a.file ? '' : ' <i class="text-amber-400" title="这个素材还没配图">无图</i>')
      + '<button data-ref="' + s.id + '" data-ri="' + i + '" data-act="up" class="ml-0.5 text-slate-500 transition hover:text-white" title="上移">↑</button>'
      + '<button data-ref="' + s.id + '" data-ri="' + i + '" data-act="down" class="text-slate-500 transition hover:text-white" title="下移">↓</button>'
      + '<button data-ref="' + s.id + '" data-ri="' + i + '" data-act="del" class="text-red-400/70 transition hover:text-red-300" title="去掉这个引用">×</button>'
      + '</span>';
  });
  return h;
}

function assetOptions() {
  var h = "";
  assets.forEach(function (a) {
    h += '<option value="' + esc(a.id) + '">' + esc(assetLabel(a)) + '</option>';
  });
  return h;
}

// 按 refs 顺序解析出 [{file, kind}]，生成时原样交给后端，由它按这个顺序编号。
function syncRefEntries(s) {
  // description 必须带上：镜头提示词里的角色/场景段落就是从这里注入的（见 compose_prompt）
  s.ref_images = refEntries(s).map(function (a) {
    return { id: a.id, file: a.file, kind: a.kind, name: a.name || a.id,
             description: a.description || "" };
  });
}

function refEntries(s) {
  var out = [];
  (s.refs || []).forEach(function (id) {
    var a = assetById(id);
    if (a) out.push(a);
  });
  return out;
}

// 卡片上那排定帧候选 = 本镜自己出的 + 画布上第一个变化帧出的。画布上出的那几张本来就
// 是要选的一张，不该逼用户回卡片里再出一遍。
function shotDingCandidates(s) {
  if (!s) return [];
  var out = (s.ding_candidates || []).slice();
  var first = (s.states || [])[0];
  ((first && first.candidates) || []).forEach(function (f) { if (out.indexOf(f) < 0) out.push(f); });
  return out;
}

// 这一镜的定帧：卡片上单独选过就用选的；没选过就认画布上第一个变化帧的关键帧 ——
// 在画布上把第一帧的图选好，分镜卡片这边本来就该跟着变，不用再选第二遍。
function shotDingFrame(s) {
  if (!s) return "";
  if (s.ding_frame) return s.ding_frame;
  var first = (s.states || [])[0];
  return (first && first.keyframe) || "";
}

// 「用角色设定图当首帧」按连线取：换资产不用重新勾一遍。用户自己传过的首帧优先。
function linkedFirstFrame(s) {
  if (s.first_frame) return s.first_frame;
  if (!s.use_character_frame) return null;
  var a = refEntries(s).filter(function (x) { return x.kind === "character"; })[0];
  if (!a) return null;
  var views = a.views || {};
  return a.file || views.front || views.back || null;
}

function refMove(id, i, act) {
  var s = scenes.find(function (x) { return x.id === id; });
  if (!s) return;
  s.refs_manual = true;      // 手动排过顺序：自动连线让位，点「自动连线」才交回去
  var list = s.refs || [];
  if (act === "del") list.splice(i, 1);
  else if (act === "up" && i > 0) { var t = list[i - 1]; list[i - 1] = list[i]; list[i] = t; }
  else if (act === "down" && i < list.length - 1) { var d = list[i + 1]; list[i + 1] = list[i]; list[i] = d; }
  else return;
  syncRefEntries(s);
  scheduleSaveState();
  renderScenes();
}

var assetSaveTimer = null;
function scheduleSaveAssets() {
  clearTimeout(assetSaveTimer);
  assetSaveTimer = setTimeout(async function () {
    try {
      var r = await api("/api/save_assets", { assets: assets });
      if (r && r.ok) applyLinkedScenes(r.scenes);
    } catch (e) {}
  }, 600);
}

// 后端算完连线会把 scenes 送回来，把 refs/ref_images 抄回本地，画布上的连线立刻跟着动
function applyLinkedScenes(list) {
  if (!list || !list.length) return;
  list.forEach(function (rs) {
    var s = scenes.find(function (x) { return x.id === rs.id; });
    if (!s) return;
    s.refs = rs.refs || [];
    if (rs.ref_images) s.ref_images = rs.ref_images;
    if (rs.refs_manual) s.refs_manual = true; else delete s.refs_manual;
  });
  renderScenes();
  renderBoard();
  renderMinimap();
}

// 手工排过引用的镜头，这一下就是把它交回自动连线
async function linkAssets(sceneId) {
  try {
    var r = await api("/api/link_assets", { scene_id: sceneId || "" });
    if (!r.ok) { toast("自动连线失败：" + (r.error || ""), "err"); return; }
    applyLinkedScenes(r.scenes);
    toast("已按资产名字重新连线（" + r.linked + " 个镜头有改动）", "ok");
  } catch (e) { toast("自动连线失败：" + e.message, "err"); }
}

async function deleteAsset(assetId) {
  var a = assetById(assetId);
  if (!a) return;
  if (!confirm("删掉资产「" + (a.name || "未命名") + "」？用到它的镜头会断开这条连线。")) return;
  try {
    var r = await api("/api/delete_asset", { asset_id: assetId });
    if (!r.ok) { toast("删除失败：" + (r.error || ""), "err"); return; }
    assets = r.assets || assets.filter(function (x) { return String(x.id) !== String(assetId); });
    applyLinkedScenes(r.scenes);
    refreshAssetSlots();
    renderBoard();
    toast("资产已删除", "ok");
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

// 加一条资产：先入库，再让它自己去连需要它的镜头
async function addAsset(kind) {
  var id = "a" + Date.now().toString(36) + Math.floor(Math.random() * 1000).toString(36);
  assets = assets.concat([{ id: id, kind: kind, name: "", description: "",
                            file: null, views: {}, candidates: [], tags: [] }]);
  try {
    var r = await api("/api/save_assets", { assets: assets });
    if (r && r.ok) {
      if (r.assets) assets = r.assets;
      applyLinkedScenes(r.scenes);
    }
  } catch (e) { toast("加资产失败：" + e.message, "err"); return; }
  boardEditKey = "@" + id;     // 直接进编辑态，接着填名字和描述
  renderBoard();
  toast(ASSET_META[kind].label + "已加入资产池，填好名称后会自动连到用它的镜头", "ok", 8000);
}

// ---- 一键出片 ----------------------------------------------------------------------
//
// 把界面里散着的步串成一条链：LLM 拆镜 -> 逐镜写分镜词 -> 逐镜文生图定帧 -> 落到故事板
// （可选再生成六维资产图、逐镜出视频）。后端 auto_direct 在后台线程里跑，这里只轮询
// 进度 —— 整条链要几分钟，阻塞式请求会把页面拖死。

var autoJob = null;
var autoTimer = null;

// Agent 流水线。注册表从 /api/agents 拉，所以后端加减 agent 前端不用改。
// 两种跑法：
//   单个「运行」   -> /api/agent_run，作用在当前项目里已有的分镜上
//   「串联执行」   -> /api/auto_direct，按勾选顺序跑，一个失败剩下的就不跑

function autoPayload() {
  var g = $("frameGranularity");
  // 模型/接口按 agent 各配各的，整张表随每次运行发过去（和 key 一样不落盘在后端）
  return {
    granularity: g ? g.value : "fine",
    agent_models: settings.agent_models || {}
  };
}

async function loadAgents() {
  var r;
  try { r = await api("/api/agents"); } catch (e) { return; }
  if (r && r.ok) { agentMeta = r.agents || []; renderAgents(agentMeta); renderAgentModelRows(); }
}

function renderAgents(list) {
  var box = $("agentList");
  if (!box) return;
  box.innerHTML = "";
  list.forEach(function (a, i) {
    var row = document.createElement("div");
    row.className = "flex items-center gap-2 rounded-lg border border-white/10 bg-black/20 px-2 py-1.5";
    row.innerHTML =
      '<input type="checkbox" class="align-middle" data-chain="' + a.id + '"' +
        (i < 2 ? " checked" : "") + ' title="加入串联">' +
      '<iconify-icon icon="' + (a.icon || "lucide:box") + '" width="14" class="shrink-0 text-violet-300"></iconify-icon>' +
      '<div class="min-w-0 flex-1">' +
        '<p class="truncate text-[11px] font-semibold text-slate-200">' + esc(a.name) + '</p>' +
        '<p class="truncate text-[10px] text-slate-500">' + esc(a.desc) + '</p>' +
      '</div>' +
      '<span data-st="' + a.id + '" class="shrink-0 text-[10px] text-slate-500">待运行</span>' +
      '<button data-run="' + a.id + '" class="shrink-0 rounded-md border border-white/10 px-2 py-1 text-[10px] text-slate-300 transition hover:border-violet-400/50 hover:text-violet-200">运行</button>';
    box.appendChild(row);
  });
  box.querySelectorAll("[data-run]").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      runAgent(b.getAttribute("data-run"));
    });
  });
}

function setAgentState(aid, text, kind) {
  var el = document.querySelector('[data-st="' + aid + '"]');
  if (!el) return;
  el.textContent = text;
  el.className = "shrink-0 text-[10px] " +
    (kind === "run" ? "text-amber-300" : kind === "ok" ? "text-emerald-300"
     : kind === "err" ? "text-red-300" : "text-slate-500");
}

function agentChain() {
  var out = [];
  document.querySelectorAll("[data-chain]").forEach(function (c) {
    if (c.checked) out.push(c.getAttribute("data-chain"));
  });
  return out;
}

async function runAgent(aid) {
  var r = await api("/api/agent_run", Object.assign({ agent: aid }, autoPayload()));
  if (!r.ok) { toast("启动失败：" + (r.error || ""), "err"); return; }
  autoJob = r.job;
  setAgentState(aid, "排队…", "run");
  $("autoPanel").classList.remove("hidden");
  $("importPanel").classList.add("hidden");
  autoTick();
}

async function startAuto() {
  var idea = ($("autoIdea").value || "").trim();
  if (!idea) { toast("先写一句你的想法或脚本", "err"); return; }
  var chain = agentChain();
  if (chain.indexOf("split") === -1) chain.unshift("split");   // 串联总得从拆镜开始
  var r = await api("/api/auto_direct", Object.assign({ idea: idea, chain: chain }, autoPayload()));
  if (!r.ok) { toast("启动失败：" + (r.error || ""), "err"); return; }
  autoJob = r.job;
  chain.forEach(function (a) { setAgentState(a, "排队…", "run"); });
  $("autoPanel").classList.remove("hidden");
  $("importPanel").classList.add("hidden");
  autoTick();
}

function autoTick() {
  clearTimeout(autoTimer);
  if (!autoJob) return;
  api("/api/auto_direct_status?job=" + encodeURIComponent(autoJob)).then(function (r) {
    if (!r.ok) { $("autoStatus").textContent = r.error || "任务没了"; return; }
    var pct = r.total ? Math.round(r.done / r.total * 100) : 5;
    $("autoBar").style.width = pct + "%";
    $("autoStatus").textContent = r.step
      + (r.total ? "  " + r.done + "/" + r.total : "")
      + (r.current ? "  " + r.current : "");
    if (r.agent) setAgentState(r.agent, r.step, "run");
    if (r.error) {
      if (r.agent) setAgentState(r.agent, "失败", "err");
      toast((r.agent ? _agentLabel(r.agent) : "流水线") + "失败：" + r.error, "err", 12000);
      return;
    }
    if (r.step === "完成") {
      if (r.agent) setAgentState(r.agent, "完成", "ok");
      $("autoStatus").textContent = (r.result || "完成")
        + " · 共 " + (r.scenes || []).length + " 镜";
      restoreState().then(function () {
        toast((r.result || "已完成") + "，已落到故事板", "ok", 6000);
      });
      return;
    }
    if (r.step === "失败" || r.step === "已取消") return;
    autoTimer = setTimeout(autoTick, 1500);
  }).catch(function (e) {
    $("autoStatus").textContent = "轮询失败：" + e.message;
  });
}

function _agentLabel(aid) {
  var el = document.querySelector('[data-st="' + aid + '"]');
  return el ? el.previousElementSibling.textContent : aid;
}

$("btnAuto").addEventListener("click", startAuto);
$("btnAutoCancel").addEventListener("click", async function () {
  if (autoJob) await api("/api/auto_direct_cancel", { job: autoJob });
  $("autoStatus").textContent = "已取消";
  clearTimeout(autoTimer);
});
loadAgents();

// 分镜台事件委托
$("scenes").addEventListener("click", function (e) {
  var actionEl = e.target.closest("[data-action]");
  var card = e.target.closest(".shot");
  if (actionEl && card) {
    var id = card.getAttribute("data-id");
    var a = actionEl.getAttribute("data-action");
    e.stopPropagation();
    // 引用徽章上的 ↑/↓/×：改的是同一个 scene.refs 顺序，重排后 <Subject N> 跟着变
    if (actionEl.getAttribute("data-ref")) {
      refMove(actionEl.getAttribute("data-ref"),
              parseInt(actionEl.getAttribute("data-ri")),
              actionEl.getAttribute("data-act"));
      return;
    }
    if (a === "relink") linkAssets(id);
    // 帧卡上的按钮直接复用画布那套动作：同一个变化帧，同一份逻辑
    else if (a === "board-split") boardSplit(id);
    else if (a === "board-gen") boardGenKeyframe(id, actionEl.getAttribute("data-idx"));
    else if (a === "board-seg") boardGenSegment(id, actionEl.getAttribute("data-idx"));
    else if (a === "board-pick") boardPickKeyframe(id, actionEl.getAttribute("data-idx"), actionEl.getAttribute("data-file"));
    else if (a === "board-frame-del") removeState(id, parseInt(actionEl.getAttribute("data-idx"), 10));
    else if (a === "compile") compileShot(id);
    else if (a === "gen") generateScene(id);
    else if (a === "frame") uploadFrame(id);
    else if (a === "frame-del") clearFrame(id);
    else if (a === "continue") continueVideo(id, null);
    else if (a === "continue-own") {
      var cs = scenes.find(function (x) { return x.id === id; });
      if (cs && cs.output) continueVideo(id, cs.output);
    }
    else if (a === "ding") dingFrame(id);
    else if (a === "keyframe") keyframeScene(id);
    else if (a === "ding-pick") pickDing(id, actionEl.getAttribute("data-file"));
    else if (a === "ding-preview") openDingPreview(id, actionEl.getAttribute("data-file"));
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
    // 帧卡上的输入写进那一个变化帧，镜头卡上的写进镜头
    var target = (t.dataset.frame === undefined || t.dataset.frame === "")
      ? s : (s.states || [])[parseInt(t.dataset.frame, 10)];
    if (!target) return;
    if (f === "seconds") target.seconds = clampInt(v, 4, 15, 5);
    else if (f === "steps") target.steps = clampInt(v, 5, 50, 25);
    else if (f === "seed") target.seed = String(v).trim() !== "" ? parseInt(v) : null;
    else target[f] = v;
    if (f === "seconds" && scenes[curIdx] && scenes[curIdx].id === t.dataset.id) renderMonitor();
    if (f === "seconds" || f === "subtitle") renderTimeline();
    scheduleSaveState();
  }
});
$("scenes").addEventListener("change", function (e) {
  var addSel = e.target;
  if (addSel && addSel.getAttribute && addSel.getAttribute("data-refadd")) {
    var sid = addSel.getAttribute("data-refadd");
    var sv = scenes.find(function (x) { return x.id === sid; });
    var aid = addSel.value;
    if (sv && aid && assetById(aid)) {
      if ((sv.refs || []).some(function (x) { return String(x) === String(aid); })) {
        toast("这个素材已经引用了", "err");
      } else {
        sv.refs = (sv.refs || []).concat([aid]);
        sv.refs_manual = true;      // 手动加进来的引用：这一镜不再自动连线
        syncRefEntries(sv);
        scheduleSaveState();
        renderScenes();
      }
    }
    addSel.value = "";
    return;
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
  if (t.dataset.field === "seconds" || t.dataset.field === "subtitle") renderTimeline();
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
$("btnPagePrev").addEventListener("click", function () { if (curIdx > 0) selectScene(curIdx - 1); });
$("timeline").addEventListener("click", function (e) {
  var el = e.target.closest("[data-idx]");
  if (el) selectScene(parseInt(el.getAttribute("data-idx")));
});
$("monitorVideo").addEventListener("timeupdate", function () {
  var s = scenes[curIdx];
  if (!s || !s.output) return;
  movePlayhead(shotStartSec(curIdx) + this.currentTime);
});
$("btnPageNext").addEventListener("click", function () { if (curIdx < scenes.length - 1) selectScene(curIdx + 1); });
$("btnFinalClose").addEventListener("click", function () {
  $("finalModal").classList.add("hidden");
  $("finalModalVideo").pause();
});
$("btnDingClose").addEventListener("click", closeImagePreview);
$("btnDingUse").addEventListener("click", useImagePreview);
$("btnDingPrev").addEventListener("click", function () { shiftImagePreview(-1); });
$("btnDingNext").addEventListener("click", function () { shiftImagePreview(1); });
document.addEventListener("keydown", function (e) {
  if ($("dingModal").classList.contains("hidden")) return;
  if (e.key === "Escape") closeImagePreview();
  else if (e.key === "ArrowLeft") shiftImagePreview(-1);
  else if (e.key === "ArrowRight") shiftImagePreview(1);
});
$("btnPrev").addEventListener("click", prevShot);
$("btnNext").addEventListener("click", nextShot);
$("btnGenAll").addEventListener("click", generateAll);
$("btnStop").addEventListener("click", function () { genAllStop = true; cancelCurrent(); });
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
  settings.video_model = videoModel;
  saveSettings();
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
// ---- 故事板画布（无限画布：每个镜头的「变化帧」按顺序连成链）----
var boardView = "board";   // 故事板画布是主视图，分镜卡片退成编辑单镜的备用视图
var boardPos = {};
var boardPan = { x: 40, y: 40 };
var boardZoom = 1;
var boardInit = false;          // 首次进画布时定位到当前镜头，之后保留用户自己的视角
var boardMiniView = null;       // 小地图的缩放/偏移，供点击换算坐标
var boardBusy = {};
var boardEditKey = null;   // 正在就地编辑的变化帧（卡片 key），null 表示没有
var boardDomMap = {};      // 卡片 key -> 已渲染的 DOM 元素，避免整块重绘

function boardOpts(list, cur) {
  return list.map(function (v) {
    return '<option value="' + esc(v) + '"' + (v === cur ? " selected" : "") + '>' + esc(v) + '</option>';
  }).join("");
}

function boardEditFormHtml(node) {
  var s = node.scene, x = node.state, i = node.idx;
  var IN = "w-full rounded border border-white/10 bg-[#0f131b] px-1.5 py-1 text-[10px] text-slate-200 outline-none focus:border-violet-400/50";
  return '<div class="px-2.5 py-2">'
    + '<textarea data-field="prompt" class="' + IN + ' h-[62px] resize-none leading-4" placeholder="这一帧的画面描述">' + esc(x.prompt || "") + '</textarea>'
    + '<div class="mt-1 flex gap-1">'
    + '<select data-field="shot" class="' + IN + '">' + boardOpts(SHOTS, x.shot) + '</select>'
    + '<select data-field="camera" class="' + IN + '">' + boardOpts(CAMERAS, x.camera) + '</select>'
    + '<input data-field="seconds" type="number" min="3" max="15" value="' + (x.seconds || 4) + '" class="' + IN + ' w-14 shrink-0" title="从这一帧推进到下一帧的秒数">'
    + '</div>'
    + '<input data-field="subtitle" value="' + esc(x.subtitle || "") + '" placeholder="台词字幕（可空）" class="' + IN + ' mt-1">'
    + '<div class="mt-1.5 flex gap-1.5">'
    + '<button data-action="board-edit-save" data-scene="' + esc(s.id) + '" data-idx="' + i + '" class="flex-1 rounded-md border border-emerald-400/30 bg-emerald-400/10 py-1 text-[10px] text-emerald-200 transition hover:bg-emerald-400/20">保存</button>'
    + '<button data-action="board-edit-cancel" class="rounded-md border border-white/10 px-3 py-1 text-[10px] text-slate-400 transition hover:text-slate-200">取消</button>'
    + '</div></div>';
}

// 画布坐标：故事主轴横着走（左→右），一个镜头占一列，列内变化帧往下叠；
// 资产栏在最左边。竖着排 29 个镜头会把画布拉到一万多像素高，横向才排得下。
var CARD_W = 252, CARD_H = 326;
var ASSET_CARD_H = 500; // 资产卡多了候选图与六视图网格，比镜头卡高；高度必须按真实内容算，不然两张资产卡会叠在一起、底下那排六视图还会被裁掉
var PORT_Y = 26;        // 卡片左右端口的中心高度：连线从这里进出，和 LibTV 的节点端口一样
var COL_W = 300;        // 一个镜头占一列的列宽
var ROW_H = 384;        // 列内变化帧之间的行距（留出写运镜标签的缝）
var ASSET_X = -320;     // 资产栏所在列的 x
var HEADER_H = 46;      // 列头高度
var HEADER_GAP = 8;     // 列头与卡片之间的缝
// 衍生节点（宫格）：每次生成都在父节点下面挂一个，虚线连回，多版本并存 —— 照着 LibTV 的做法
var VERSION_GAP = 16, VERSION_H = 98, TILE = 40, BATCH_PITCH = 96, BATCH_MAX = 3;

// 一个节点下面挂了几个宫格衍生节点（每次生成一个）
function boardBatchesOf(node) {
  if (node.batch || node.kind || node.header) return [];
  var slot = node.idx === null ? node.scene : node.state;
  var field = node.idx === null ? "clip" : "keyframe";
  var all = slot[field + "_batches"] || [];
  return all.slice(-BATCH_MAX).map(function (b) {
    return { key: node.key + "@b" + b.n, parent: node, batch: b, field: field, slot: slot };
  });
}

// 各类节点的真实高度，供排布与包围盒计算使用
function boardNodeH(n) {
  if (n.batch) return VERSION_H;
  if (n.header) return HEADER_H;
  return n.kind ? ASSET_CARD_H : CARD_H;
}

// 列内累计高度：前面的节点如果挂了宫格，下面那个变化帧要往下让
function boardColumnY(s, upTo) {
  var y = 0;
  var list = boardNodesOf(s);
  for (var i = 0; i < upTo && i < list.length; i++) {
    y += ROW_H + (boardBatchesOf(list[i]).length ? VERSION_H : 0);
  }
  return y;
}
var MIN_ZOOM = 0.05, MAX_ZOOM = 2.5;
var READABLE_ZOOM = 0.45;   // 低于这个比例卡片就看不清了

// 资产节点：角色/场景/道具/设备，摆在最左边一栏，从它们长出真正被引用的连线。
// 以前是 ASSET_KINDS.map —— 每类一个节点，等于「整部片子只能有一个角色一个场景」；
// 现在素材池里有几条就出几个节点。
function boardAssetNodes() {
  return (assets || []).map(function (a) {
    return { key: "@" + a.id, kind: a.kind, label: a.name || "未命名资产", asset: a, obj: a };
  });
}
// 列头：缩到很小的时候也能认出哪一列是第几个镜头
function boardHeaderNodes() {
  return scenes.map(function (s) { return { key: "@h" + s.id, header: true, scene: s }; });
}
function boardHeaderPos(s) {
  return { x: scenes.indexOf(s) * COL_W, y: -(HEADER_H + HEADER_GAP) };
}
function boardAssetDefaultPos(kind) {
  var i = ASSET_KINDS.indexOf(kind);
  return { x: ASSET_X, y: Math.max(0, i) * (ASSET_CARD_H + 40) };
}
function boardAllNodes() {
  var out = boardAssetNodes().concat(boardHeaderNodes());
  scenes.forEach(function (s) {
    boardNodesOf(s).forEach(function (n) {
      out.push(n);
      out = out.concat(boardBatchesOf(n));   // 宫格衍生节点也算画布上的节点
    });
  });
  return out;
}

function boardKey(sceneId, idx) { return idx === null ? sceneId : sceneId + "#" + idx; }

function boardNodesOf(s) {
  var st = (s.states && s.states.length) ? s.states : null;
  if (!st) return [{ key: boardKey(s.id, null), scene: s, idx: null, state: null }];
  return st.map(function (x, i) { return { key: boardKey(s.id, i), scene: s, idx: i, state: x }; });
}

function boardDefaultPos(key) {
  for (var k = 0; k < scenes.length; k++) {
    var s = scenes[k];
    if (key === "@h" + s.id) return boardHeaderPos(s);
    var list = boardNodesOf(s);
    for (var i = 0; i < list.length; i++) {
      var y = boardColumnY(s, i);
      if (list[i].key === key) return { x: k * COL_W, y: y };
      var bs = boardBatchesOf(list[i]);
      for (var j = 0; j < bs.length; j++) {
        if (bs[j].key === key) return { x: k * COL_W + j * BATCH_PITCH, y: y + CARD_H + VERSION_GAP };
      }
    }
  }
  return { x: 0, y: 0 };
}

function boardPosOf(key) {
  if (boardPos[key]) return boardPos[key];
  // 别把资产键写死成某几种：加一类资产（比如「设备」）就会掉进兜底分支、落到 (0,0) 跟镜头重叠
  if (key.charAt(0) === "@" && ASSET_KINDS.indexOf(key.slice(1)) >= 0) return boardAssetDefaultPos(key.slice(1));
  return boardDefaultPos(key);
}

function boardAssetCardHtml(node) {
  var o = node.obj || {};
  var aid = String((node.asset || {}).id || "");
  var busy = !!assetBusy[aid];
  var cands = o.candidates || [];
  var meta = ASSET_META[node.kind] || {};
  var views = o.views || {};
  var viewDone = VIEW_ORDER.filter(function (v) { return !!views[v[0]]; }).length;
  var viewBusy = !!assetViewBusy[aid];
  var viewLabel = assetViewLabel[aid];
  var p = boardPosOf(node.key);
  var head = '<div class="board-card absolute rounded-xl border ' + (o.file ? "border-white/20" : "border-white/10")
    + ' bg-[#2b2b2b] shadow-lg shadow-black/40" data-key="' + esc(node.key) + '" data-kind="' + node.kind
    + '" data-asset="' + esc(aid) + '" style="left:' + p.x
    + 'px;top:' + p.y + 'px;width:' + CARD_W + 'px;height:' + ASSET_CARD_H + 'px">'
    + '<span class="board-port" style="right:-5px;top:' + (PORT_Y - 5) + 'px"></span>'
    + '<div class="flex cursor-move items-center gap-1.5 px-2.5 py-2">'
    + '<iconify-icon class="shrink-0 text-slate-500" icon="lucide:' + (meta.icon || "image") + '" width="12"></iconify-icon>'
    + '<span class="truncate text-[11px] text-slate-300">' + esc(node.label) + '</span>'
    + '<span class="ml-auto shrink-0 truncate pl-2 text-[10px] text-slate-500">' + esc(meta.short || "") + '</span>'
    + '<button data-action="board-asset-del" data-asset="' + esc(aid) + '" title="删掉这条资产（用到它的镜头会断开这条连线）" class="shrink-0 rounded px-1 text-slate-500 transition hover:bg-white/10 hover:text-red-300">✕</button></div>';
  if (boardEditKey === node.key) {
    var IN = "w-full rounded border border-white/10 bg-[#0f131b] px-1.5 py-1 text-[10px] text-slate-200 outline-none focus:border-violet-400/50";
    return head
      + '<div class="relative mx-2 h-[152px] overflow-hidden rounded-lg bg-black/40">'
      + (o.file ? '<img data-file="' + esc(o.file) + '" title="双击放大" class="h-full w-full object-contain" src="/input/' + encodeURI(o.file) + '" loading="lazy">'
                 : '<div class="flex h-full items-center justify-center text-[10px] text-slate-600">还没有底图</div>')
      + '</div>'
      + '<div class="px-2.5 py-2">'
      + '<input data-field="name" value="' + esc(o.name || "") + '" placeholder="名称，例：三号灌装线" class="' + IN + '">'
      + '<textarea data-field="description" placeholder="' + esc(meta.hint || "描述") + '" class="' + IN + ' mt-1 h-[86px] resize-none leading-4">' + esc(o.description || "") + '</textarea>'
      + '<div class="mt-1.5 flex gap-1.5">'
      + '<button data-action="board-asset-save" data-asset="' + esc(aid) + '" class="flex-1 rounded-md border border-emerald-400/30 bg-emerald-400/10 py-1 text-[10px] text-emerald-200 transition hover:bg-emerald-400/20">保存</button>'
      + '<button data-action="board-asset-cancel" class="rounded-md border border-white/10 px-3 py-1 text-[10px] text-slate-400 transition hover:text-slate-200">取消</button>'
      + '</div></div></div>';
  }
  return head
    + '<div class="relative mx-2 h-[152px] overflow-hidden rounded-lg bg-black/40">'
    + (o.file ? '<img data-file="' + esc(o.file) + '" title="双击放大" class="h-full w-full object-contain" src="/input/' + encodeURI(o.file) + '" loading="lazy">'
               : '<div class="flex h-full flex-col items-center justify-center gap-1 text-slate-600"><iconify-icon icon="lucide:' + (meta.icon || "image") + '" width="22"></iconify-icon><span class="text-[10px]">未生成底图</span></div>')
    + (o.file ? '<span class="absolute left-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[9px] text-slate-300">AI生成</span>' : '')
    + (busy ? '<div class="absolute inset-0 flex items-center justify-center bg-black/70 text-[11px] text-slate-200">生成中…</div>' : '')
    + '</div>'
    + '<div class="cursor-text px-2.5 py-2" data-action="board-asset-edit" data-key="' + esc(node.key) + '" title="点这里改名称和描述">'
    + '<p class="line-clamp-3 h-[46px] overflow-hidden text-[11px] leading-4 text-slate-400">'
    + esc(o.description || ("（点这里填写" + (meta.short || "") + "描述）")) + '</p></div>'
    + '<div class="flex gap-1.5 px-2.5"><button data-action="board-asset-gen" data-asset="' + esc(aid) + '" class="flex-1 rounded-md border border-white/12 bg-white/[0.04] py-1 text-[10px] text-slate-300 transition hover:border-white/25 hover:text-white">AI 生成底图</button>'
    + '<button data-action="board-views" data-asset="' + esc(aid) + '" class="flex-1 rounded-md border border-white/12 bg-white/[0.04] py-1 text-[10px] text-slate-300 transition hover:border-white/25 hover:text-white">六视图（图生图）</button>'
    + '<button data-action="board-asset-edit" data-key="' + esc(node.key) + '" title="改名称和描述" class="shrink-0 rounded-md border border-white/12 bg-white/[0.04] px-2 py-1 text-[10px] text-slate-300 transition hover:border-white/25 hover:text-white">编辑</button></div>'
    + (cands.length ? '<div class="mt-1.5 flex gap-1 overflow-x-auto px-2.5">' + cands.map(function (f) {
        return '<img data-action="board-asset-pick" data-asset="' + esc(aid) + '" data-file="' + esc(f) + '" title="点这张设为底图" class="h-12 w-14 shrink-0 cursor-pointer rounded border bg-black/40 object-contain ' + (o.file === f ? "border-violet-400/70" : "border-white/10 hover:border-violet-400/40") + '" src="/input/' + encodeURI(f) + '">';
      }).join("") + '</div>' : '')
    + '<div class="mt-2 border-t border-white/[0.08] px-2.5 pb-2.5 pt-2">'
    + '<div class="mb-1 flex items-center justify-between text-[10px] text-slate-500"><span>六视图资产</span><span>' + viewDone + '/6</span></div>'
    + (viewBusy ? '<div class="rounded-md bg-black/40 py-2 text-center text-[10px] text-violet-200">' + esc(viewLabel || "生成中…") + '</div>'
      : '<div class="grid grid-cols-3 gap-1">' + VIEW_ORDER.map(function (v) {
          var rel = views[v[0]];
          return '<div data-action="board-view-one" data-asset="' + esc(aid) + '" data-view="' + v[0] + '" class="relative cursor-pointer overflow-hidden rounded border ' + (rel ? "border-violet-400/30" : "border-white/10") + ' bg-black/40 transition hover:border-violet-400/70" title="' + v[1] + '视图 · 点这里只重出这一张">'
            + (rel ? '<img class="h-[54px] w-full object-contain" src="/input/' + encodeURI(rel) + '" loading="lazy">'
                   : '<div class="flex h-[54px] items-center justify-center text-[9px] text-slate-600">' + v[1] + '</div>')
            + (rel ? '<span class="absolute inset-x-0 bottom-0 bg-black/60 text-center text-[9px] text-slate-300">' + v[1] + '</span>' : '')
            + '</div>';
        }).join("") + '</div>')
    + '</div>'
    + '</div>';
}

function boardApplyTransform() {
  $("boardWorld").style.transform = "translate(" + boardPan.x + "px," + boardPan.y + "px) scale(" + boardZoom + ")";
  $("boardZoomLabel").textContent = Math.round(boardZoom * 100) + "%";
  boardMiniViewport();
}

// 小地图：把整张画布缩成一小块，画出所有节点和当前视口，点哪跳哪
function renderMinimap() {
  var mini = $("boardMini");
  if (!mini || boardView !== "board" || !scenes.length) { if (mini) mini.innerHTML = ""; return; }
  var b = boardBounds();
  var W = mini.clientWidth || 180, H = mini.clientHeight || 118;
  // 横竖分开缩放：故事板又宽又扁，等比缩放会把小地图 2/3 的高度白白空掉，点起来只有几个像素
  var w = Math.max(1, b.maxX - b.minX), h = Math.max(1, b.maxY - b.minY);
  var sx = W / w, sy = H / h;
  var ox = -b.minX * sx, oy = -b.minY * sy;
  boardMiniView = { sx: sx, sy: sy, ox: ox, oy: oy };
  var html = boardAllNodes().map(function (n) {
    var p = boardPosOf(n.key);
    var color = n.header ? "#6a6a6a" : n.kind ? "#4f4f4f"
      : (n.state && n.state.keyframe ? "#9a9a9a" : "#383838");
    return '<i class="absolute block rounded-[2px]" style="left:' + (p.x * sx + ox).toFixed(1) + 'px;top:' + (p.y * sy + oy).toFixed(1)
      + 'px;width:' + Math.max(3, CARD_W * sx).toFixed(1) + 'px;height:' + Math.max(3, boardNodeH(n) * sy).toFixed(1)
      + 'px;background:' + color + ';opacity:.75"></i>';
  }).join("");
  mini.innerHTML = html + '<i id="boardMiniRect" class="pointer-events-none absolute border border-white/80 bg-white/10"></i>';
  boardMiniViewport();
}

// 只更新视口框，拖动时不重建整张小地图
function boardMiniViewport() {
  var mini = $("boardMini");
  if (!mini || boardView !== "board") return;
  var rect = $("boardMiniRect");
  if (!rect || !boardMiniView) return;
  var box = $("boardSurface").getBoundingClientRect();
  if (!box.width) return;
  var v = boardMiniView;
  rect.style.left = ((-boardPan.x / boardZoom) * v.sx + v.ox).toFixed(1) + "px";
  rect.style.top = ((-boardPan.y / boardZoom) * v.sy + v.oy).toFixed(1) + "px";
  rect.style.width = ((box.width / boardZoom) * v.sx).toFixed(1) + "px";
  rect.style.height = ((box.height / boardZoom) * v.sy).toFixed(1) + "px";
}

// 版本切换条：同一个节点上多次生成的结果可以来回切（LibTV 的「多版本并存」）
function boardVersionRowHtml(node) {
  var slot = node.idx === null ? node.scene : node.state;
  if (!slot) return "";
  var field = node.idx === null ? "clip" : "keyframe";
  var vers = slot[field + "_versions"] || [];
  if (vers.length < 2) return "";
  var vi = vers.indexOf(slot[field]);
  var btn = "h-5 w-5 shrink-0 rounded border border-white/10 text-slate-400 transition hover:text-emerald-200";
  return '<div class="mt-1.5 flex items-center gap-1 px-2.5">'
    + '<button data-action="board-ver" data-scene="' + esc(node.scene.id) + '" data-idx="' + (node.idx === null ? "" : node.idx)
    + '" data-field="' + field + '" data-step="-1" class="' + btn + '">‹</button>'
    + '<span class="flex-1 text-center text-[9px] text-emerald-300/80">版本 ' + (vi + 1) + ' / ' + vers.length + '</span>'
    + '<button data-action="board-ver" data-scene="' + esc(node.scene.id) + '" data-idx="' + (node.idx === null ? "" : node.idx)
    + '" data-field="' + field + '" data-step="1" class="' + btn + '">›</button></div>';
}

// 宫格衍生节点：一次生成 = 一个宫格，挂在父节点下面，虚线连回。
// 点其中一格就是「宫格切分 + 设为当前版本」——LibTV 里叫「选 N 个宫格 → 创建节点」，这里直接落成版本。
var BATCH_KIND_LABEL = { "": "生成", "segment": "重出", "edit": "换物体", "inpaint": "视频修补" };

function boardBatchNodeHtml(n) {
  var b = n.batch, p = boardPosOf(n.key);
  var cur = n.slot[n.field];
  var files = (b.files || []).slice(0, 4);
  var cols = files.length > 1 ? 2 : 1;
  var cells = files.map(function (f) {
    var on = f === cur;
    var isVid = /\.(mp4|webm|mov|mkv)$/i.test(f);
    return '<button data-action="board-setver" data-scene="' + esc(n.parent.scene.id)
      + '" data-idx="' + (n.parent.idx === null ? "" : n.parent.idx) + '" data-field="' + n.field
      + '" data-rel="' + esc(f) + '" title="点这一格：设为当前版本" class="relative overflow-hidden rounded border '
      + (on ? "border-white/70" : "border-white/12 hover:border-white/40") + ' bg-black/50" style="width:'
      + TILE + 'px;height:' + TILE + 'px">'
      + (isVid ? '<video class="h-full w-full object-cover" src="/output/' + encodeURI(f) + '" muted preload="metadata"></video>'
               : '<img class="h-full w-full object-cover" src="/input/' + encodeURI(f) + '" loading="lazy">')
      + (on ? '<span class="absolute inset-x-0 bottom-0 bg-white/85 text-center text-[9px] font-semibold text-black">当前</span>' : '')
      + '</button>';
  }).join("");
  // board-card 这个类不能少：画布上的拖动是挂在 .board-card 上的，少了它整块宫格就拖不动
  // （格子本身是 button，拖动要落在标题那一条上，所以标题给 cursor-move 提示）
  return '<div class="board-card absolute select-none" data-key="' + esc(n.key) + '" style="left:' + p.x + 'px;top:' + p.y
    + 'px;width:' + (cols * TILE + 12) + 'px">'
    + '<div class="rounded-lg border border-white/12 bg-[#242424] p-1">'
    + '<div class="mb-1 cursor-move px-0.5 text-[9px] text-slate-500">' + esc(BATCH_KIND_LABEL[b.kind || ""] || "生成")
    + ' · ' + files.length + ' 格</div>'
    + '<div class="grid gap-1" style="grid-template-columns:repeat(' + cols + ',minmax(0,1fr))">' + cells + '</div>'
    + '</div></div>';
}

function boardCardHtml(node) {
  var s = node.scene, x = node.state;
  var prompt = x ? x.prompt : s.prompt;
  var shot = x ? x.shot : s.shot;
  var camera = x ? x.camera : s.camera;
  var secs = x ? x.seconds : s.seconds;
  var kf = x ? x.keyframe : (s.ding_frame || s.first_frame);
  var cands = (x ? x.candidates : s.ding_candidates) || [];
  var busy = boardBusy[node.key];
  var clip = x ? x.clip : (s.output || null);
  var edit = (x ? x.edit : s.edit) || null;
  var nextKf = false;
  if (x && node.idx !== null && s.states[node.idx + 1]) nextKf = !!s.states[node.idx + 1].keyframe;
  var title = "镜" + (scenes.indexOf(s) + 1) + (node.idx === null ? "" : " · 变化帧 " + (node.idx + 1) + "/" + s.states.length);
  // 上下端口只在真的接得上时才画：首帧没有上一帧，末帧没有下一帧，单帧镜头上下都不接
  var stacked = node.idx !== null && s.states && s.states.length > 1;
  var hasPrev = stacked && node.idx > 0;
  var hasNext = stacked && node.idx < s.states.length - 1;
  var editing = !!(x && boardEditKey === node.key);
  var head = '<div class="board-card absolute rounded-xl border ' + (kf ? "border-white/20" : "border-white/10")
    + ' bg-[#2b2b2b] shadow-lg shadow-black/40" data-key="' + esc(node.key) + '" style="left:' + boardPosOf(node.key).x
    + 'px;top:' + boardPosOf(node.key).y + 'px;width:' + CARD_W + 'px;min-height:' + CARD_H + 'px">'
    + '<span class="board-port" style="left:-5px;top:' + (PORT_Y - 5) + 'px"></span>'
    + '<span class="board-port" style="right:-5px;top:' + (PORT_Y - 5) + 'px"></span>'
    + (hasPrev ? '<span class="board-port" style="left:calc(50% - 5px);top:-5px"></span>' : '')
    + (hasNext ? '<span class="board-port" style="left:calc(50% - 5px);bottom:-5px"></span>' : '')
    + '<div class="flex cursor-move items-center gap-1.5 px-2.5 py-2">'
    + '<iconify-icon class="shrink-0 text-slate-500" icon="' + (clip ? "lucide:film" : "lucide:image") + '" width="12"></iconify-icon>'
    + '<span class="truncate text-[11px] text-slate-300">' + esc(title) + '</span>'
    + '<span class="ml-auto shrink-0 text-[10px] text-slate-500">' + esc(shot || "—") + ' · ' + (secs || "—") + 's</span>'
    + (node.idx === null ? ''
        : '<button data-action="board-frame-del" data-scene="' + esc(s.id) + '" data-idx="' + node.idx
          + '" title="删除这个变化帧" class="flex h-4 w-4 shrink-0 items-center justify-center rounded text-slate-500 transition hover:bg-white/10 hover:text-red-300"><iconify-icon icon="lucide:x" width="11"></iconify-icon></button>')
    + '</div>'
    + '<div class="relative mx-2 h-[132px] overflow-hidden rounded-lg bg-black/40">'
    + (kf ? '<img data-file="' + esc(kf) + '" title="双击放大" class="h-full w-full object-cover" src="/input/' + encodeURI(kf) + '" loading="lazy">'
          : '<div class="flex h-full flex-col items-center justify-center gap-1 text-slate-600"><iconify-icon icon="lucide:image" width="22"></iconify-icon><span class="text-[10px]">未生成关键帧</span></div>')
    + (kf ? '<span class="absolute left-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[9px] text-slate-300">AI生成</span>' : '')
    + (busy ? '<div class="absolute inset-0 flex items-center justify-center bg-black/70 text-[11px] text-slate-200">生成中…</div>' : '')
    + '</div>';
  if (editing) return head + boardEditFormHtml(node) + '</div>';
  return head
    + '<div class="px-2.5 pb-1 pt-2"><p class="line-clamp-3 h-[46px] overflow-hidden text-[11px] leading-4 text-slate-400">' + esc(prompt || "（无描述）") + '</p>'
    + '<div class="mt-1.5 flex items-center gap-1.5 text-[10px] text-slate-500"><iconify-icon icon="lucide:video" width="11"></iconify-icon><span>' + esc(camera || "—") + '</span></div></div>'
    + '<div class="flex gap-1.5 px-2.5">'
    + '<button data-action="board-gen" data-key="' + esc(node.key) + '" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" class="flex-1 rounded-md border border-white/10 bg-white/[0.03] py-1 text-[10px] text-slate-300 transition hover:border-sky-400/40 hover:text-sky-200">生成关键帧</button>'
    + (node.idx === null
        ? '<button data-action="board-split" data-scene="' + esc(s.id) + '" class="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[10px] text-slate-300 transition hover:border-cyan-400/40 hover:text-cyan-200">拆变化帧</button>'
        : '<button data-action="board-edit" data-scene="' + esc(s.id) + '" data-idx="' + node.idx + '" class="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[10px] text-slate-300 transition hover:border-violet-400/40 hover:text-violet-200">编辑</button>')
    + '</div>'
    + (clip
        ? '<div class="mt-1.5 flex items-center gap-1.5 px-2.5"><button data-action="board-play" data-file="' + esc(clip) + '" class="flex flex-1 items-center justify-center gap-1 rounded-md border border-emerald-400/30 bg-emerald-400/10 py-1 text-[10px] text-emerald-200 transition hover:bg-emerald-400/20"><iconify-icon icon="lucide:play" width="11"></iconify-icon>播放</button>'
          + '<button data-action="board-edit-obj" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" class="flex flex-1 items-center justify-center gap-1 rounded-md border border-amber-400/30 bg-amber-400/10 py-1 text-[10px] text-amber-200 transition hover:bg-amber-400/20" title="改关键帧再重出：抽出首尾帧 → 按你的描述改 → 用改过的帧重出这一段"><iconify-icon icon="lucide:wand-2" width="11"></iconify-icon>换物体</button>'
          + '</div>'
          + '<div class="mt-1.5 flex items-center gap-1.5 px-2.5"><button data-action="board-inpaint" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" class="flex flex-1 items-center justify-center gap-1 rounded-md border border-rose-400/30 bg-rose-400/10 py-1 text-[10px] text-rose-200 transition hover:bg-rose-400/20" title="视频修补：SAM3 按文字圈出目标，VACE 只在那一块重画，其余画面一个像素都不动"><iconify-icon icon="lucide:eraser" width="11"></iconify-icon>视频修补</button>'
          + '<button data-action="board-seg" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" class="rounded-md border border-white/10 px-2 py-1 text-[10px] text-slate-400 transition hover:text-slate-200">重出</button></div>'
        : (nextKf
          ? '<div class="mt-1.5 px-2.5"><button data-action="board-seg" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" class="flex w-full items-center justify-center gap-1 rounded-md border border-white/10 bg-white/[0.03] py-1 text-[10px] text-slate-300 transition hover:border-emerald-400/40 hover:text-emerald-200"><iconify-icon icon="lucide:film" width="11"></iconify-icon>用这帧→下一帧出视频</button></div>'
          : ''))
    + (cands.length && !boardBatchesOf(node).length
        ? '<div class="mt-1.5 flex gap-1 overflow-x-auto px-2.5 pb-2">' + cands.map(function (f) {
            return '<img data-action="board-pick" data-scene="' + esc(s.id) + '" data-idx="' + (node.idx === null ? "" : node.idx) + '" data-file="' + esc(f) + '" title="点这张设为该变化帧" class="h-9 w-14 shrink-0 cursor-pointer rounded border ' + (kf === f ? "border-violet-400/70" : "border-white/10 hover:border-violet-400/40") + ' object-cover" src="/input/' + encodeURI(f) + '">';
          }).join("") + '</div>' : '')
    + boardVersionRowHtml(node)
    + boardEditBoxHtml(node, edit)
    + '</div>';
}

// 卡片内容指纹：只要指纹没变就复用已有 DOM，不重建 <img>/<video>
// ——整块 innerHTML 重绘会把在途的图片请求全部取消，一屏几十张 1MB 的图就变成「加载失败」
// 「换物体」的候选帧区：首帧必须选，片段的尾帧也要选（物体在动，只改首帧重出会弹回原样）
function boardEditBoxHtml(node, edit) {
  if (!edit || !edit.frames) return "";
  var s = node.scene, idxAttr = (node.idx === null ? "" : node.idx);
  var pick = edit.pick || {};
  function row(label, key, files) {
    if (!files || !files.length) return "";
    return '<div class="mt-2 px-2.5"><div class="mb-1 flex items-center justify-between text-[10px] text-slate-500">'
      + '<span>' + label + '</span><span>' + (pick[key] ? "已选" : "点一张选用") + '</span></div>'
      + '<div class="flex gap-1 overflow-x-auto">' + files.map(function (f) {
          return '<img data-action="board-edit-pick" data-scene="' + esc(s.id) + '" data-idx="' + idxAttr
            + '" data-slot="' + key + '" data-file="' + esc(f) + '" title="' + label + '：点这张选用" class="h-10 w-16 shrink-0 cursor-pointer rounded border '
            + (pick[key] === f ? "border-amber-400/80" : "border-white/10 hover:border-amber-400/50") + ' object-cover" src="/input/' + encodeURI(f) + '">';
        }).join("") + '</div></div>';
  }
  var need = ["first"].concat(edit.frames.last ? ["last"] : []);
  var ready = need.every(function (k) { return !!pick[k]; });
  return '<div class="mt-2 border-t border-amber-400/20 bg-amber-400/[0.04] pb-2.5 pt-1.5">'
    + '<div class="px-2.5 text-[10px] leading-4 text-amber-200/80">换物体：' + esc((edit.instruction || "").slice(0, 40)) + '</div>'
    + row("首帧（改过的）", "first", edit.frames.first)
    + row("尾帧（改过的）", "last", edit.frames.last)
    + '<div class="mt-2 px-2.5"><button data-action="board-apply-edit" data-scene="' + esc(s.id) + '" data-idx="' + idxAttr
    + '" class="w-full rounded-md border border-amber-400/40 bg-amber-400/15 py-1 text-[10px] font-semibold text-amber-100 transition hover:bg-amber-400/25'
    + (ready ? '' : ' opacity-40') + '">' + (ready ? '用这些帧重出这一段' : '先把上面要用的帧都选上') + '</button></div></div>';
}

function boardHeaderHtml(n) {
  var s = n.scene, p = boardPosOf(n.key);
  var n2 = scenes.indexOf(s) + 1;
  var nf = (s.states && s.states.length) ? s.states.length + " 帧" : "未拆帧";
  return '<div class="board-card absolute select-none" data-key="' + esc(n.key) + '" style="left:' + p.x + 'px;top:' + p.y
    + 'px;width:' + CARD_W + 'px;height:' + HEADER_H + 'px">'
    + '<div class="flex h-full items-center gap-2 rounded-lg border border-white/10 bg-[#2b2b2b] px-2.5">'
    + '<span class="flex h-5 w-5 shrink-0 items-center justify-center rounded bg-white/10 text-[10px] font-bold text-slate-300">' + n2 + '</span>'
    + '<span class="truncate text-[11px] font-semibold text-slate-300">' + esc(s.shot || "—") + '</span>'
    + '<span class="shrink-0 text-[10px] text-slate-500">' + esc(nf) + '</span></div></div>';
}

function boardNodeSig(n) {
  if (n.batch) {
    return ["batch", n.key, (n.batch.files || []).join(","), n.batch.kind || "", n.slot[n.field] || ""].join("|");
  }
  if (n.header) return ["hdr", scenes.indexOf(n.scene), n.scene.shot, (n.scene.states || []).length].join("|");
  if (n.kind) {
    var o = n.obj || {};
    return ["asset", n.kind, o.file, o.name, o.description, (o.candidates || []).join(","),
            JSON.stringify(o.views || {}), assetBusy[o.id], assetViewBusy[o.id], assetViewLabel[o.id],
            boardEditKey === n.key].join("|");
  }
  var s = n.scene, x = n.state, nx = null;
  if (x && n.idx !== null && s.states[n.idx + 1]) nx = s.states[n.idx + 1].keyframe || "";
  // 注意三元的优先级：写成 (x ? x.candidates : s.ding_candidates || []) 时，
  // x 存在但 x.candidates 未定义就会 undefined.join() 抛错，整个画布渲染中断
  return ["scene", scenes.indexOf(s), n.idx, x ? x.keyframe : (s.ding_frame || s.first_frame),
          (x ? (x.candidates || []) : (s.ding_candidates || [])).join(","),
          (x ? (x.keyframe_versions || []) : (s.clip_versions || [])).length,
          x ? x.prompt : s.prompt, x ? x.shot : s.shot, x ? x.camera : s.camera, x ? x.seconds : s.seconds,
          x ? x.clip : s.output, !!boardBusy[n.key], boardEditKey === n.key,
          JSON.stringify((x ? x.edit : s.edit) || null),
          n.idx === null ? "" : s.states.length + "/" + nx].join("|");
}

function renderBoard() {
  if (boardView !== "board") return;
  var box = $("boardNodes");
  var nodes = boardAllNodes();   // 含宫格衍生节点，别自己再拼一遍列表
  var seen = {};
  nodes.forEach(function (n) {
    seen[n.key] = 1;
    var p = boardPosOf(n.key);
    var el = boardDomMap[n.key];
    var sig = boardNodeSig(n);
    if (el && el.parentNode === box && el.getAttribute("data-sig") === sig) {
      el.style.left = p.x + "px";
      el.style.top = p.y + "px";
      return;
    }
    var tmp = document.createElement("div");
    tmp.innerHTML = n.header ? boardHeaderHtml(n) : n.kind ? boardAssetCardHtml(n)
      : n.batch ? boardBatchNodeHtml(n) : boardCardHtml(n);
    var fresh = tmp.firstElementChild;
    fresh.setAttribute("data-sig", sig);
    if (el && el.parentNode === box) box.replaceChild(fresh, el);
    else box.appendChild(fresh);
    boardDomMap[n.key] = fresh;
  });
  Object.keys(boardDomMap).forEach(function (k) {
    if (seen[k]) return;
    var e = boardDomMap[k];
    if (e && e.parentNode) e.parentNode.removeChild(e);
    delete boardDomMap[k];
  });
  boardRenderEdges();
  boardApplyTransform();
  renderMinimap();
  boardEdgesSoon();
}

// Tailwind 是运行时 JIT：刚插进 DOM 的节点要等它扫一遍才拿到样式，卡片高度到那时才定型。
// 端口长在卡片边缘，高度差几十像素线就落不到端口上，所以样式生效后再量一次重画连线。
var boardEdgesTimer = 0;
function boardEdgesSoon() {
  clearTimeout(boardEdgesTimer);
  boardEdgesTimer = setTimeout(boardRenderEdges, 80);
}

function boardBoxOf(n) {
  var p = boardPosOf(n.key);
  var el = boardDomMap[n.key];
  return { x: p.x, y: p.y, w: CARD_W, h: (el && el.offsetHeight) || boardNodeH(n) };
}

function boardRenderEdges() {
  var paths = [];
  // 列内：这一帧 → 下一帧，竖着往下走，标签写在两帧之间的缝里
  scenes.forEach(function (s) {
    var list = boardNodesOf(s);
    if (list.length < 2) return;
    for (var i = 0; i < list.length - 1; i++) {
      var ba = boardBoxOf(list[i]), bb = boardBoxOf(list[i + 1]);
      var x1 = ba.x + ba.w / 2, y1 = ba.y + ba.h;
      var x2 = bb.x + bb.w / 2, y2 = bb.y;
      var dy = Math.max(18, Math.abs(y2 - y1) * 0.4);
      paths.push('<path d="M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + (y1 + dy) + ' ' + x2 + ' ' + (y2 - dy) + ' ' + x2 + ' ' + y2
        + '" fill="none" stroke="#4d4d4d" stroke-width="1.5"/>');
      paths.push('<text x="' + ((x1 + x2) / 2 + 10) + '" y="' + ((y1 + y2) / 2 + 4) + '" fill="#8a8a8a" font-size="10">'
        + esc((list[i].state && list[i].state.camera) || "") + " · " + ((list[i].state && list[i].state.seconds) || "") + 's</text>');
    }
  });
  // 镜头之间：上一镜的首帧 → 下一镜的首帧，横着连（故事主轴）
  for (var k = 0; k < scenes.length - 1; k++) {
    var la = boardNodesOf(scenes[k]), lb = boardNodesOf(scenes[k + 1]);
    var a = boardPosOf(la[0].key), b = boardPosOf(lb[0].key);
    var x1 = a.x + CARD_W, y1 = a.y + PORT_Y, x2 = b.x, y2 = b.y + PORT_Y;
    var dx = Math.max(30, Math.abs(x2 - x1) * 0.45);
    paths.push('<path d="M' + x1 + ' ' + y1 + ' C' + (x1 + dx) + ' ' + y1
      + ' ' + (x2 - dx) + ' ' + y2 + ' ' + x2 + ' ' + y2
      + '" fill="none" stroke="#555555" stroke-width="1.5"/>');
  }
  // 父节点 → 宫格衍生节点：绿色虚线，表示「这次生成是从它分叉出来的」（LibTV 的节点分叉）
  boardAllNodes().forEach(function (n) {
    if (!n.batch) return;
    var pp = boardPosOf(n.parent.key), bp = boardPosOf(n.key);
    var x1 = pp.x + 26, y1 = pp.y + CARD_H, x2 = bp.x + 26, y2 = bp.y;
    paths.push('<path d="M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + (y1 + 14) + ' ' + x2 + ' ' + (y2 - 14) + ' ' + x2 + ' ' + y2
      + '" fill="none" stroke="#5a5a5a" stroke-width="1.5" stroke-dasharray="4 3" stroke-opacity="0.8"/>');
  });
  // 资产栏 → 第一个镜头：角色与场景设定会注入每个镜头的提示词，只画一条免得几十条线糊成一片
  // 素材 -> 引用了它的镜头。以前每个素材只画一条边、且只连到第一个镜头，画布上看不出
  // 「哪个角色被哪些镜头用了」；现在按 scene.refs 逐条画，并把引用顺序标在边上 —— 这就是
  // LibTV「连线顺序即引用顺序」落到画布上的样子。
  var usesByAsset = {};
  var subjectNo = {};        // "镜头id|资产id" -> 这条引用在该镜的 <Subject N> 编号
  scenes.forEach(function (s) {
    var n = 0;
    (s.refs || []).forEach(function (aid, order) {
      var a = assetById(aid);
      if (a && a.kind !== "scene") { n += 1; subjectNo[s.id + "|" + aid] = n; }
      var k = String(aid);
      (usesByAsset[k] = usesByAsset[k] || []).push({ scene: s, order: order });
    });
  });
  boardAssetNodes().forEach(function (an) {
    if (!an.asset || !an.asset.file) return;
    var uses = usesByAsset[String(an.asset.id)] || [];
    if (!uses.length) return;                      // 没被任何镜头引用的素材不画边
    var ap = boardPosOf(an.key);
    var ax = ap.x + CARD_W, ay = ap.y + PORT_Y;
    uses.sort(function (p, q) { return p.order - q.order; });
    uses.forEach(function (u, i) {
      var n = boardNodesOf(u.scene)[0];
      if (!n) return;
      var bp = boardPosOf(n.key);
      var ty = bp.y + PORT_Y + i * 12;   // 同一镜头的多条边按引用顺序自上而下排开
      paths.push('<path d="M' + ax + ' ' + ay + ' C' + (ax + 40) + ' ' + ay
        + ' ' + (bp.x - 40) + ' ' + ty + ' ' + bp.x + ' ' + ty
        + '" fill="none" stroke="#555555" stroke-width="1.5" stroke-opacity="'
        + (u.order === 0 ? "0.95" : "0.55") + '"/>');
      // 标注要跟提示词里对得上：场景资产占的是 <Picture> 那个环境位，不占 <Subject N>
      // （见 build_graph_h3 的 ref_entries 分流），所以单独标「环境」。
      // 位置放在曲线中段：同一张资产连十几个镜头时，挤在资产口上会叠成一团。
      var label = an.asset.kind === "scene" ? "环境"
                                             : "Subject " + (subjectNo[u.scene.id + "|" + an.asset.id] || 1);
      paths.push('<text x="' + Math.round((ax + bp.x) / 2) + '" y="' + Math.round((ay + ty) / 2 - 4)
        + '" fill="#9a9a9a" font-size="10">' + esc(label) + '</text>');
    });
  });
  $("boardEdges").innerHTML = paths.join("");
}

function boardSavePositions() {
  clearTimeout(boardSavePositions._t);
  boardSavePositions._t = setTimeout(function () {
    api("/api/board", { positions: boardPos }).catch(function () {});
  }, 800);
}

function boardBounds() {
  var minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
  boardAllNodes().forEach(function (n) {
    var p = boardPosOf(n.key);
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + CARD_W); maxY = Math.max(maxY, p.y + boardNodeH(n));
  });
  return { minX: minX, minY: minY, maxX: maxX, maxY: maxY };
}

// 把某个节点摆到视口正中
function boardFocus(key, z) {
  var box = $("boardSurface").getBoundingClientRect();
  if (!box.width || !key) return;
  var p = boardPosOf(key);
  if (z) boardZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
  boardPan = { x: box.width / 2 - (p.x + CARD_W / 2) * boardZoom,
               y: box.height / 2 - (p.y + CARD_H / 2) * boardZoom };
  boardApplyTransform();
}

function boardCurrentKey() {
  var s = scenes[curIdx];
  if (!s) return null;
  var list = boardNodesOf(s);
  return list.length ? list[0].key : null;
}

// 适应全部：缩到能装下为止，但不会缩到看不清（再小就只剩色块了）
function boardFit() {
  var nodes = boardAllNodes();
  if (!nodes.length) { boardPan = { x: 40, y: 40 }; boardZoom = 1; boardApplyTransform(); return; }
  var b = boardBounds();
  var box = $("boardSurface").getBoundingClientRect();
  var z = Math.min((box.width - 60) / Math.max(1, b.maxX - b.minX),
                   (box.height - 60) / Math.max(1, b.maxY - b.minY), 1.2);
  boardZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
  boardPan = { x: 30 - b.minX * boardZoom, y: 30 - b.minY * boardZoom };
  boardApplyTransform();
  if (z < READABLE_ZOOM) {
    toast("内容比可视区大得多，已缩到 " + Math.round(boardZoom * 100) + "%（再小就看不清了）；用右下角小地图或「定位当前镜头」跳转", "ok", 9000);
  }
}

function showBoardView(which) {
  boardView = which;
  // listView 里那层滚动容器靠的是 flex-1 + min-h-0。只切换 hidden 的话它是块级盒子，
  // 里层撑到内容那么高、外层 overflow-hidden 一剪，卡片多了就滚不动也翻不了页。
  $("listView").classList.toggle("hidden", which !== "list");
  $("listView").classList.toggle("flex", which === "list");
  $("boardView").classList.toggle("hidden", which !== "board");
  $("viewList").className = "rounded-md px-3 py-1.5 text-xs font-semibold transition " + (which === "list" ? "bg-white/[0.10] text-slate-100" : "text-slate-400 hover:text-slate-200");
  $("viewBoard").className = "rounded-md px-3 py-1.5 text-xs font-semibold transition " + (which === "board" ? "bg-white/[0.10] text-slate-100" : "text-slate-400 hover:text-slate-200");
  var mini = $("boardMini");
  if (mini) mini.classList.toggle("hidden", which !== "board");
  if (which !== "board") return;
  renderBoard();
  var key = boardCurrentKey();
  // 首次进来：100% 对准当前镜头；之后保留用户自己拖/缩出来的视角
  if (!boardInit) { boardInit = true; key ? boardFocus(key, 1) : boardFit(); }
  else boardApplyTransform();
  renderMinimap();
}

async function boardSplit(sceneId) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s) return;
  if (!s.prompt) { toast("这个镜头还没有画面描述，先在分镜卡片里写一句", "err"); return; }
  toast("正在把这个镜头拆成变化帧…", "ok", 4000);
  try {
    // 画布上这个按钮就是「分镜提示词生帧」agent，模型跟着全局设置里那一行走
    var r = await api("/api/split_states", { scene_id: sceneId, seconds: s.seconds, agent_models: settings.agent_models || {} });
    if (!r.ok) { toast("拆变化帧失败：" + (r.error || ""), "err", 9000); return; }
    var fresh = scenes.find(function (x) { return x.id === sceneId; });
    if (fresh) { fresh.states = r.states; delete boardPos[boardKey(sceneId, null)]; }
    renderBoard(); boardFit();
    toast("已拆成 " + r.states.length + " 个变化帧（" + (r.engine || "") + "）", "ok", 6000);
  } catch (e) { toast("拆变化帧请求失败：" + e.message, "err"); }
}

async function boardGenKeyframe(sceneId, idx) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s) return [];
  var key = boardKey(sceneId, idx === "" || idx === null ? null : parseInt(idx, 10));
  if (boardBusy[key]) { toast("这一帧正在生成中", "err"); return []; }
  boardBusy[key] = true;
  renderBoard();
  toast("关键帧生成中（Flux Kontext 出 4 张 = 一个 2×2 宫格，约 3~6 分钟）…", "ok", 7000);
  var payload = { batch: 4, steps: 20, scene: { id: s.id, prompt: s.prompt, seconds: s.seconds, subtitle: s.subtitle, shot: s.shot, camera: s.camera, subject: s.subject, environment: s.environment, lighting: s.lighting, aspect: s.aspect, resolution: s.resolution, mode: s.mode, steps: s.steps, seed: s.seed, negative_prompt: s.negative_prompt || "", refs: s.refs || [], ref_images: s.ref_images || [], global_negative_prompt: globalNeg, states: s.states || [] } };
  if (idx !== "" && idx !== null) payload.state_index = parseInt(idx, 10);
  try {
    var r = await api("/api/keyframe", payload);
    if (!r.ok) { toast("关键帧失败：" + (r.error || ""), "err", 9000); }
    else {
      var fresh = scenes.find(function (x) { return x.id === sceneId; });
      if (fresh) {
        if (idx === "" || idx === null) fresh.ding_candidates = r.files;
        else if (fresh.states && fresh.states[parseInt(idx, 10)]) {
          fresh.states[parseInt(idx, 10)].candidates = r.files;
          // 第一个变化帧的图同时进分镜卡片的定帧候选，卡片那边不用再出一遍
          if (parseInt(idx, 10) === 0) fresh.ding_candidates = r.files;
        }
        renderScenes();
        if (scenes[curIdx] && scenes[curIdx].id === sceneId) renderMonitor();
      }
      toast("已出 " + r.files.length + " 张，点缩略图选用", "ok", 6000);
      scheduleSaveState();
      boardBusy[key] = false;
      renderBoard();
      return r.files;          // 批量流程（生成全部）要拿它自动选第一张
    }
  } catch (e) { toast("关键帧请求失败：" + e.message, "err"); }
  boardBusy[key] = false;
  renderBoard();
  return [];
}

async function boardSaveAsset(assetId) {
  var card = document.querySelector('.board-card[data-asset="' + assetId + '"]');
  if (!card) return;
  var val = function (f) { var el = card.querySelector('[data-field="' + f + '"]'); return el ? el.value : ""; };
  var o = assetById(assetId);
  if (!o) return;
  try {
    var r = await api("/api/asset_info", { asset_id: assetId, name: val("name").trim(),
                                           description: val("description").trim() });
    if (!r.ok) { toast("保存失败：" + (r.error || ""), "err", 9000); return; }
    o.name = r.asset.name;
    o.description = r.asset.description;
    applyLinkedScenes(r.scenes);     // 名字或描述改了，连线跟着重算
    refreshAssetSlots();
    boardEditKey = null;
    renderBoard();
    renderAsset(o.kind);
    toast((ASSET_META[o.kind] || {}).label + "已保存，用到它的镜头自动连上了", "ok", 6000);
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

// 取到「这一镜的成片」或「这一段的片段」所在的那个对象
function boardClipSlot(sceneId, idx) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s) return null;
  var i = (idx === "" || idx === null) ? null : parseInt(idx, 10);
  var slot = i === null ? s : (s.states || [])[i];
  if (!slot) return null;
  return { scene: s, index: i, slot: slot, video: i === null ? s.output : slot.clip };
}

async function boardEditObject(sceneId, idx) {
  var t = boardClipSlot(sceneId, idx);
  if (!t) return;
  if (!t.video) { toast("这一镜还没有成片，先出视频再来改", "err"); return; }
  var old = (t.slot.edit && t.slot.edit.instruction) || "";
  var ins = prompt("要把画面里的什么换成什么？\n例如：把红色杯子换成蓝色玻璃瓶", old);
  if (!ins || !ins.trim()) return;
  var key = boardKey(sceneId, t.index) + "@edit";
  if (boardBusy[key]) { toast("编辑帧正在生成中", "err"); return; }
  boardBusy[key] = true;
  renderBoard();
  toast("正在抽帧并按描述改写（Flux Kontext，片段要改首尾两帧，约 3~6 分钟）…", "ok", 9000);
  try {
    var r = await api("/api/edit_frames", { scene_id: sceneId, index: t.index === null ? "" : t.index,
                                            instruction: ins.trim(), steps: 20 });
    if (!r.ok) { toast("换物体失败：" + (r.error || ""), "err", 12000); }
    else {
      t.slot.edit = { instruction: ins.trim(), prompt_en: r.prompt_en, video: r.video, frames: r.frames, pick: {} };
      toast("已出改好的帧：选首帧" + (r.frames.last ? "（片段还要选尾帧）" : "") + "，再点「用这些帧重出」", "ok", 10000);
    }
  } catch (e) { toast("换物体请求失败：" + e.message, "err"); }
  boardBusy[key] = false;
  renderBoard();
}

function boardPickEditFrame(sceneId, idx, slotName, file) {
  var t = boardClipSlot(sceneId, idx);
  if (!t || !t.slot.edit) return;
  t.slot.edit.pick = t.slot.edit.pick || {};
  t.slot.edit.pick[slotName] = file;
  renderBoard();
}

async function boardApplyEdit(sceneId, idx) {
  var t = boardClipSlot(sceneId, idx);
  if (!t || !t.slot.edit) { toast("先点「换物体」", "err"); return; }
  var pick = t.slot.edit.pick || {};
  if (!pick.first) { toast("先选一张改好的首帧", "err"); return; }
  if (t.slot.edit.frames && t.slot.edit.frames.last && !pick.last) {
    toast("片段要把尾帧也选上——物体在动，只改首帧重出会弹回原样", "err", 10000);
    return;
  }
  var key = boardKey(sceneId, t.index) + "@apply";
  if (boardBusy[key]) { toast("正在重出", "err"); return; }
  boardBusy[key] = true;
  renderBoard();
  toast("正在用改好的帧重出（约 3~5 分钟）…", "ok", 9000);
  try {
    var r = await api("/api/apply_edit", { scene_id: sceneId, index: t.index === null ? "" : t.index,
                                           first: pick.first, last: pick.last || "" });
    if (!r.ok) { toast("重出失败：" + (r.error || ""), "err", 12000); }
    else {
      if (t.index === null) { t.scene.output_prev = t.scene.output; t.scene.output = r.video; t.scene.edit = null; }
      else { t.slot.clip_prev = t.slot.clip; t.slot.clip = r.video; t.slot.edit = null; }
      toast("换好了。原片留在 clip_prev / output_prev，想回退可以说一声", "ok", 10000);
      scheduleSaveState();
    }
  } catch (e) { toast("重出请求失败：" + e.message, "err"); }
  boardBusy[key] = false;
  renderBoard();
}

// 视频修补：真·掩膜重绘。SAM3 按文字圈出目标 → VACE 只在掩膜里重画，掩膜外原样保留。
async function boardVideoInpaint(sceneId, idx) {
  var t = boardClipSlot(sceneId, idx);
  if (!t) return;
  if (!t.video) { toast("这一镜还没有成片，先出视频再来修补", "err"); return; }
  var oldT = (t.slot.inpaint && t.slot.inpaint.target) || "";
  var target = prompt("要修掉或换掉画面里的什么？\n用画面里看得见的东西描述，例如：画面右边穿蓝衣服的人 / 桌上那个红色杯子", oldT);
  if (!target || !target.trim()) return;
  var repl = prompt("那块地方要变成什么？\n留空 = 按背景自然补全（等于抹掉）", (t.slot.inpaint && t.slot.inpaint.prompt) || "");
  if (repl === null) return;
  var key = boardKey(sceneId, t.index) + "@inpaint";
  if (boardBusy[key]) { toast("修补正在跑", "err"); return; }
  boardBusy[key] = true;
  renderBoard();
  toast("正在修补（VACE 14B，5 秒片段约 5~15 分钟）…", "ok", 10000);
  try {
    var r = await api("/api/video_inpaint", { scene_id: sceneId, index: t.index === null ? "" : t.index,
                                              target: target.trim(), prompt: (repl || "").trim(), steps: 20 });
    if (!r.ok) { toast("修补失败：" + (r.error || ""), "err", 14000); }
    else {
      if (t.index === null) { t.scene.output_prev = t.scene.output; t.scene.output = r.video; }
      else { t.slot.clip_prev = t.slot.clip; t.slot.clip = r.video; }
      t.slot.inpaint = { target: target.trim(), prompt: (repl || "").trim(), frames: r.frames, seed: r.seed };
      toast("修补完成（" + r.frames + " 帧）。原片留在 clip_prev / output_prev", "ok", 10000);
      scheduleSaveState();
    }
  } catch (e) { toast("修补请求失败：" + e.message, "err"); }
  boardBusy[key] = false;
  renderBoard();
}

// 切版本：把某个衍生节点里的某一格设为「当前」。原片不丢，随时切回来。
async function boardSetVersion(sceneId, idx, field, rel) {
  await applyVersion(sceneId, idx, field, rel);
}

async function boardStepVersion(sceneId, idx, field, step) {
  var t = boardClipSlot(sceneId, idx);
  if (!t) return;
  var slot = t.index === null ? t.scene : t.slot;
  var vers = slot[field + "_versions"] || [];
  if (vers.length < 2) return;
  var i = vers.indexOf(slot[field]);
  await applyVersion(sceneId, idx, field, vers[((i < 0 ? 0 : i) + step + vers.length) % vers.length]);
}

async function applyVersion(sceneId, idx, field, rel) {
  if (!rel) return;
  try {
    var r = await api("/api/set_version", { scene_id: sceneId, index: idx === "" || idx === null ? "" : parseInt(idx, 10),
                                            field: field, rel: rel });
    if (!r.ok) { toast("切换版本失败：" + (r.error || ""), "err", 9000); return; }
    var t = boardClipSlot(sceneId, idx);
    if (t) {
      var slot = t.index === null ? t.scene : t.slot;
      if (field === "clip") slot.clip_prev = slot.clip;
      slot[field] = rel;
      if (t.index === null && field === "clip") { t.scene.output_prev = t.scene.output; t.scene.output = rel; }
      else if (t.index === null) { t.scene.ding_frame = rel; t.scene.first_frame = rel; }
    }
    renderBoard();
    toast("已切到该版本", "ok");
  } catch (e) { toast("切换版本失败：" + e.message, "err"); }
}

async function boardSaveEdit(sceneId, idx) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s || !s.states) return;
  var i = parseInt(idx, 10);
  if (!s.states[i]) return;
  var card = document.querySelector('.board-card[data-key="' + boardKey(sceneId, i) + '"]');
  if (!card) return;
  var val = function (f) { var el = card.querySelector('[data-field="' + f + '"]'); return el ? el.value : ""; };
  var next = s.states.slice();
  var sec = parseInt(val("seconds"), 10);
  next[i] = Object.assign({}, next[i], {
    prompt: val("prompt").trim(),
    shot: val("shot"),
    camera: val("camera"),
    seconds: (sec >= 3 && sec <= 15) ? sec : next[i].seconds,
    subtitle: val("subtitle").trim()
  });
  try {
    var r = await api("/api/save_states", { scene_id: sceneId, states: next });
    if (!r.ok) { toast("保存失败：" + (r.error || ""), "err", 9000); return; }
    s.states = r.states;
    boardEditKey = null;
    renderBoard();
    toast("变化帧已保存", "ok");
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

async function boardGenSegment(sceneId, idx) {
  var s = scenes.find(function (x) { return x.id === sceneId; });
  if (!s || !s.states) return;
  var i = parseInt(idx, 10);
  var a = s.states[i], b = s.states[i + 1];
  if (!a || !b) { toast("这一段不存在", "err"); return; }
  if (!a.keyframe || !b.keyframe) { toast("首尾两个变化帧都要先选定关键帧", "err"); return; }
  var key = boardKey(sceneId, i) + "@seg";
  if (boardBusy[key]) { toast("这一段正在生成中", "err"); return; }
  boardBusy[key] = true;
  renderBoard();
  toast("这一段生成中（" + (a.camera || "") + " · " + (a.seconds || 0) + " 秒，约 1~3 分钟）…", "ok", 8000);
  try {
    var r = await api("/api/segment", { scene_id: sceneId, index: i });
    if (!r.ok) { toast("这一段生成失败：" + (r.error || ""), "err", 9000); }
    else {
      var fresh = scenes.find(function (x) { return x.id === sceneId; });
      if (fresh && fresh.states[i]) fresh.states[i].clip = r.clip;
      renderScenes();
      toast("这一段已生成", "ok", 6000);
      scheduleSaveState();
    }
  } catch (e) { toast("这一段请求失败：" + e.message, "err"); }
  boardBusy[key] = false;
  renderBoard();
  renderScenes();
}

async function boardPickKeyframe(sceneId, idx, file) {
  try {
    var r;
    if (idx === "" || idx === null) {
      r = await api("/api/pick_ding", { scene_id: sceneId, file: file });
      if (r.ok) { var s = scenes.find(function (x) { return x.id === sceneId; }); if (s) { s.first_frame = r.first_frame; s.ding_frame = r.first_frame; } }
    } else {
      r = await api("/api/pick_state_keyframe", { scene_id: sceneId, state_index: parseInt(idx, 10), file: file });
      if (r.ok) {
        var s2 = scenes.find(function (x) { return x.id === sceneId; });
        if (s2 && s2.states) s2.states[parseInt(idx, 10)].keyframe = r.keyframe;
        // 第一帧选中的图就是这一镜的定帧/首帧：卡片与监视器立刻跟着换
        if (s2 && r.first_frame) { s2.first_frame = r.first_frame; s2.ding_frame = r.first_frame; }
        renderScenes();
        if (scenes[curIdx] && scenes[curIdx].id === sceneId) renderMonitor();
        scheduleSaveState();
      }
    }
    if (!r.ok) { toast("选用失败：" + (r.error || ""), "err"); return; }
    renderBoard();
    toast("已设为该变化帧的关键帧", "ok");
  } catch (e) { toast("选用失败：" + e.message, "err"); }
}

$("btnCharImage").addEventListener("click", uploadCharImage);
$("btnCharImageDel").addEventListener("click", delCharImage);
$("btnSceneImage").addEventListener("click", uploadSceneImage);
$("btnSceneImageDel").addEventListener("click", delSceneImage);
$("btnCharGen").addEventListener("click", function () { genAssetImage("character"); });
$("btnSceneGen").addEventListener("click", function () { genAssetImage("scene"); });
$("charCandRow").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action='asset-pick']");
  if (el) pickAssetImage(el.getAttribute("data-kind"), el.getAttribute("data-file"));
});
$("viewList").addEventListener("click", function () { showBoardView("list"); });
$("viewBoard").addEventListener("click", function () { showBoardView("board"); });
$("boardZoomIn").addEventListener("click", function () { boardZoom = Math.min(2.5, boardZoom * 1.2); boardApplyTransform(); });
$("boardZoomOut").addEventListener("click", function () { boardZoom = Math.max(0.2, boardZoom / 1.2); boardApplyTransform(); });
$("boardFit").addEventListener("click", boardFit);
$("board100").addEventListener("click", function () { boardFocus(boardCurrentKey(), 1); });
$("boardLocate").addEventListener("click", function () {
  var k = boardCurrentKey();
  if (!k) { boardFit(); return; }
  boardFocus(k, boardZoom < READABLE_ZOOM ? 1 : boardZoom);
});
$("boardMini").addEventListener("click", function (e) {
  if (!boardMiniView) return;
  var r = $("boardMini").getBoundingClientRect();
  var wx = (e.clientX - r.left - boardMiniView.ox) / boardMiniView.sx;
  var wy = (e.clientY - r.top - boardMiniView.oy) / boardMiniView.sy;
  var box = $("boardSurface").getBoundingClientRect();
  boardPan = { x: box.width / 2 - wx * boardZoom, y: box.height / 2 - wy * boardZoom };
  boardApplyTransform();
});
$("boardSurface").addEventListener("dblclick", function (e) {
  if (e.target.closest(".board-card")) return;
  var k = boardCurrentKey();
  if (k) boardFocus(k, boardZoom < READABLE_ZOOM ? 1 : boardZoom);
});
var boardFilmTimer = null;
async function boardFilmPoll() {
  try {
    var resp = await api("/api/board_film_status");
    var r = (resp && resp.status) || {};
    var info = $("boardFilmInfo");
    if (r.running) {
      info.classList.remove("hidden");
      info.textContent = (r.current || r.stage || "进行中") + " · 已完成 " + (r.done || 0);
      return true;
    }
    info.classList.add("hidden");
    if (r.stage === "done") {
      toast("整板成片完成", "ok", 8000);
      var f = scenes.length ? scenes[0] : null;
      if (r.result) { $("monitorVideo").src = "/output/" + encodeURI(r.result); $("monitorVideo").classList.remove("hidden"); $("monitorEmpty").classList.add("hidden"); }
    } else if (r.stage === "error") {
      toast("整板出片失败：" + (r.error || ""), "err", 12000);
    }
    return false;
  } catch (e) { return false; }
}
$("boardFilm").addEventListener("click", async function () {
  var pending = 0;
  scenes.forEach(function (s) {
    var st = s.states || [];
    for (var i = 0; i < st.length - 1; i++) if (!st[i].clip) pending++;
  });
  if (!confirm("整板出片：缺 " + pending + " 段会现场生成（每段约 3~5 分钟），然后自动合成成片。继续？")) return;
  var r = await api("/api/board_film", { need_audio: needAudio });
  if (!r.ok) { toast("启动失败：" + (r.error || ""), "err", 9000); return; }
  toast("整板出片已启动", "ok", 5000);
  if (boardFilmTimer) clearInterval(boardFilmTimer);
  boardFilmTimer = setInterval(async function () { if (!(await boardFilmPoll())) clearInterval(boardFilmTimer); }, 4000);
});
$("btnAddAsset").addEventListener("click", function () {
  var sel = $("addAssetKind");
  addAsset(sel && sel.value ? sel.value : "character");
});
$("btnRelinkAssets").addEventListener("click", function () { linkAssets(""); });
// 一键拆变化帧：给所有还没拆过、而且写了画面描述的镜头依次拆。（画布和分镜卡片共用这一份）
async function splitAllShots() {
  var todo = scenes.filter(function (s) { return (s.prompt || "").trim() && !(s.states && s.states.length); });
  if (!todo.length) { toast("没有需要拆的镜头（都拆过了）", "ok"); return; }
  if (!confirm("给 " + todo.length + " 个还没拆的镜头依次拆变化帧？")) return;
  for (var i = 0; i < todo.length; i++) await boardSplit(todo[i].id);
}
$("boardSplitAll").addEventListener("click", splitAllShots);
$("btnSplitAll").addEventListener("click", splitAllShots);
$("btnGenKfAll").addEventListener("click", genAllKeyframes);
$("btnGenKfAllBoard").addEventListener("click", genAllKeyframes);
var boardDrag = null, cardDrag = null;
$("boardSurface").addEventListener("mousedown", function (e) {
  if (e.target.closest(".board-card")) return;
  boardDrag = { x: e.clientX, y: e.clientY, px: boardPan.x, py: boardPan.y };
  e.preventDefault();
});
$("boardNodes").addEventListener("mousedown", function (e) {
  var card = e.target.closest(".board-card");
  if (!card || e.target.closest("button") || e.target.closest("img")) return;
  var key = card.getAttribute("data-key");
  var p = boardPosOf(key);
  cardDrag = { key: key, el: card, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y };
  e.preventDefault();
});
document.addEventListener("mousemove", function (e) {
  if (boardDrag) {
    boardPan = { x: boardDrag.px + (e.clientX - boardDrag.x), y: boardDrag.py + (e.clientY - boardDrag.y) };
    boardApplyTransform();
  } else if (cardDrag) {
    var nx = cardDrag.ox + (e.clientX - cardDrag.sx) / boardZoom;
    var ny = cardDrag.oy + (e.clientY - cardDrag.sy) / boardZoom;
    boardPos[cardDrag.key] = { x: nx, y: ny };
    cardDrag.el.style.left = nx + "px";
    cardDrag.el.style.top = ny + "px";
    boardRenderEdges();
  }
});
document.addEventListener("mouseup", function () {
  if (cardDrag) boardSavePositions();
  boardDrag = null;
  cardDrag = null;
});
$("boardSurface").addEventListener("wheel", function (e) {
  e.preventDefault();
  var box = $("boardSurface").getBoundingClientRect();
  var mx = e.clientX - box.left, my = e.clientY - box.top;
  var nz = Math.max(0.2, Math.min(2.5, boardZoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
  boardPan = { x: mx - (mx - boardPan.x) * (nz / boardZoom), y: my - (my - boardPan.y) * (nz / boardZoom) };
  boardZoom = nz;
  boardApplyTransform();
}, { passive: false });
// 画布上双击一张图 → 放大看（左右可翻页，「用这一张」按节点类型走）
$("boardNodes").addEventListener("dblclick", function (e) {
  var img = e.target.closest("img");
  var card = e.target.closest("[data-key]");
  if (!img || !card) return;
  var rel = img.getAttribute("data-file") || img.getAttribute("data-rel") || "";
  var node = boardNodeByKey(card.getAttribute("data-key") || "");
  if (!rel || !node) return;
  openBoardPreview(node, rel);
});
$("boardNodes").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action]");
  if (!el) return;
  var a = el.getAttribute("data-action");
  var sid = el.getAttribute("data-scene"), idx = el.getAttribute("data-idx");
  if (a === "board-gen") boardGenKeyframe(sid, idx);
  else if (a === "board-frame-del") removeState(sid, parseInt(idx, 10));
  else if (a === "board-split") boardSplit(sid);
  else if (a === "board-pick") boardPickKeyframe(sid, idx, el.getAttribute("data-file"));
  else if (a === "board-seg") boardGenSegment(sid, idx);
  else if (a === "board-asset-gen") genAssetImage(el.getAttribute("data-asset"));
  else if (a === "board-views") genAssetViews(el.getAttribute("data-asset"));
  else if (a === "board-view-one") genAssetViews(el.getAttribute("data-asset"), [el.getAttribute("data-view")]);
  else if (a === "board-edit") { boardEditKey = boardKey(sid, parseInt(idx, 10)); renderBoard(); }
  else if (a === "board-asset-edit") { boardEditKey = el.getAttribute("data-key"); renderBoard(); }
  else if (a === "board-asset-cancel") { boardEditKey = null; renderBoard(); }
  else if (a === "board-asset-save") boardSaveAsset(el.getAttribute("data-asset"));
  else if (a === "board-asset-del") deleteAsset(el.getAttribute("data-asset"));
  else if (a === "board-edit-obj") boardEditObject(sid, idx);
  else if (a === "board-edit-pick") boardPickEditFrame(sid, idx, el.getAttribute("data-slot"), el.getAttribute("data-file"));
  else if (a === "board-apply-edit") boardApplyEdit(sid, idx);
  else if (a === "board-inpaint") boardVideoInpaint(sid, idx);
  else if (a === "board-setver") boardSetVersion(sid, idx, el.getAttribute("data-field"), el.getAttribute("data-rel"));
  else if (a === "board-ver") boardStepVersion(sid, idx, el.getAttribute("data-field"), parseInt(el.getAttribute("data-step"), 10));
  else if (a === "board-edit-cancel") { boardEditKey = null; renderBoard(); }
  else if (a === "board-edit-save") boardSaveEdit(sid, idx);
  else if (a === "board-asset-pick") pickAssetImage(el.getAttribute("data-asset"), el.getAttribute("data-file"));
  else if (a === "board-play") { $("monitorVideo").src = "/output/" + encodeURI(el.getAttribute("data-file")); $("monitorVideo").classList.remove("hidden"); $("monitorEmpty").classList.add("hidden"); $("monitorVideo").play().catch(function () {}); }
});
$("sceneCandRow").addEventListener("click", function (e) {
  var el = e.target.closest("[data-action='asset-pick']");
  if (el) pickAssetImage(el.getAttribute("data-kind"), el.getAttribute("data-file"));
});
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
  e.target.value = "";
  if (f) readScriptFile(f);
});
var SCRIPT_EXTS = [".md", ".markdown", ".txt", ".doc", ".docx", ".xls", ".xlsx"];
function readScriptFile(f) {
  docxFilename = f.name;
  docxB64 = null;
  $("docxName").textContent = "正在读取：" + f.name + "…";
  docxReading = new Promise(function (resolve) {
    var reader = new FileReader();
    reader.onload = function () {
      docxB64 = reader.result.split(",")[1];
      $("docxName").textContent = "已选择：" + f.name;
      resolve(true);
    };
    reader.onerror = function () {
      docxFilename = "";
      $("docxName").textContent = "";
      toast("文档读取失败，请重新选择文件", "err");
      resolve(false);
    };
    reader.readAsDataURL(f);
  });
}
$("importPanel").addEventListener("dragover", function (e) { e.preventDefault(); });
$("importPanel").addEventListener("drop", function (e) {
  e.preventDefault();
  var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  var ext = "." + (f.name.split(".").pop() || "").toLowerCase();
  if (SCRIPT_EXTS.indexOf(ext) < 0) {
    toast("请上传脚本文档：" + SCRIPT_EXTS.join(" "), "err");
    return;
  }
  readScriptFile(f);
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

// 生成进度：每秒向后端取一次 ComfyUI 当前阶段/采样步数，写到该镜头的状态徽标与预览条上
var liveProg = { stage: "", step: 0, steps: 0 };
var liveProgAt = 0;

async function fetchLiveProgress() {
  if (Date.now() - liveProgAt < 900) return;
  liveProgAt = Date.now();
  try {
    var r = await api("/api/progress");
    liveProg = r && r.ok ? r : { stage: "", step: 0, steps: 0 };
  } catch (e) {
    liveProg = { stage: "", step: 0, steps: 0 };
  }
}

// 刷新/返回上级/切换项目后重新进页面时，后台可能还在生成：按 /api/progress 把「生成中」接回来，
// 否则镜头一律按 state 显示成待生成，进度条消失，但 ComfyUI 还在跑
async function resumeActiveGen() {
  try {
    var r = await api("/api/progress");
    if (!r || !r.ok || !r.scene) return;
    var s = scenes.find(function (x) { return x.id === r.scene; });
    if (!s || s.output || s.status === "busy") return;
    s.resumed = true;
    s.status = "busy";
    s.startedAt = Date.now() - (r.gen_elapsed || 0) * 1000;
    setSceneStatus(s.id);
    toast("镜头 " + s.id + " 仍在后台生成，已接回进度显示", "ok", 8000);
  } catch (e) {}
}

function liveText(sec) {
  var t = Math.floor(sec / 60) + ":" + pad2(sec % 60);
  if (liveProg.steps > 0) return "采样 " + liveProg.step + "/" + liveProg.steps + " · " + t;
  if (liveProg.stage) return liveProg.stage + " · " + t;
  return "生成中 " + t;
}

function paintLiveBar(s, busy) {
  var box = $("vid_" + s.id);
  if (!box) return;
  var el = $("gp_" + s.id);
  if (!busy) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "gp_" + s.id;
    el.className = "pointer-events-none absolute inset-x-0 bottom-0 h-[3px] bg-black/50";
    el.innerHTML = '<div class="h-full w-0 bg-amber-400 transition-all duration-500"></div>';
    box.appendChild(el);
  }
  var bar = el.firstElementChild;
  if (liveProg.steps > 0) {
    bar.className = "h-full bg-amber-400 transition-all duration-500";
    bar.style.width = Math.round(liveProg.step / liveProg.steps * 100) + "%";
  } else {
    bar.className = "h-full w-full animate-pulse bg-amber-400/50";
    bar.style.width = "";
  }
}

setInterval(async function () {
  var now = Date.now();
  if (scenes.some(function (s) { return s.status === "busy" && s.startedAt; })) await fetchLiveProgress();
  // 页面接回来的「生成中」没有本地 promise 收尾：后端不再占生成位就自己收掉，免得取消/结束后一直显示生成中
  if (liveProg.ok && typeof liveProg.scene === "string") {
    scenes.forEach(function (s) {
      if (s.resumed && s.status === "busy" && s.id !== liveProg.scene) {
        s.resumed = false;
        s.status = s.output ? "ok" : "pending";
        s.startedAt = null;
        setSceneStatus(s.id);
      }
    });
  }
  scenes.forEach(function (s) {
    var busy = s.status === "busy" && !!s.startedAt;
    if (busy) {
      var badge = $("st_" + s.id);
      if (badge) badge.innerHTML = '<span class="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse"></span>' + liveText(Math.floor((now - s.startedAt) / 1000));
    }
    paintLiveBar(s, busy);
  });
}, 1000);

loadSettings();
loadVideoModel();
loadToggles();
loadBuildTag();
setInterval(loadBuildTag, 30000);
restoreState();
checkComfy();
setInterval(checkComfy, 15000);
