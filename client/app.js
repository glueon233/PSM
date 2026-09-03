"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const encodePath = (id) => id.split(/[\\/]/).map((s) => encodeURIComponent(s)).join("/");

let currentUser = null;
let currentTab = "login";

const PREVIEWABLE = new Set([".mp4", ".m4v", ".webm", ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"]);
const AUDIO_EXTS = new Set([".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"]);

function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtDuration(sec) {
  if (sec == null) return "";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtClock(t) {
  const d = new Date(t * 1000);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove("show"), 2400);
}

function copyText(text, okMsg) {
  const done = () => toast(okMsg || "已复制（粘贴到 VLC 打开网络串流）");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("复制失败，请手动复制"); }
  document.body.removeChild(ta);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ---------- auth ---------- */
function switchTab(tab) {
  currentTab = tab;
  $$(".auth-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#auth-submit").textContent = tab === "login" ? "登录" : "提交注册申请";
  $("#register-hint").style.display = tab === "register" ? "" : "none";
  $("#auth-msg").textContent = "";
  $("#auth-msg").className = "msg";
}

async function submitAuth(e) {
  e.preventDefault();
  const username = $("#auth-username").value.trim();
  const password = $("#auth-password").value;
  const msg = $("#auth-msg");
  msg.className = "msg";
  try {
    if (currentTab === "register") {
      await api("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      msg.className = "msg ok";
      msg.textContent = "注册申请已提交，请等待管理员审批后登录";
      $("#auth-username").value = "";
      $("#auth-password").value = "";
    } else {
      await api("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      location.reload();
    }
  } catch (err) {
    msg.className = "msg error";
    msg.textContent = err.message;
  }
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch {}
  location.reload();
}

/* ---------- resources ---------- */
async function loadVideos() {
  const { items } = await api("/api/videos");
  const grid = $("#video-grid");
  if (!items.length) {
    grid.innerHTML = '<div class="empty">资源库为空，请联系管理员添加</div>';
    return;
  }
  grid.innerHTML = items.map(renderCard).join("");
  grid.querySelectorAll("[data-preview]").forEach((b) =>
    b.addEventListener("click", () => requestAndPreview(b.dataset.preview, b.dataset.title, b.dataset.ext)));
  grid.querySelectorAll("[data-request]").forEach((b) =>
    b.addEventListener("click", () => requestLink(b.dataset.request, b)));
}

function renderCard(v) {
  const previewable = PREVIEWABLE.has(v.ext);
  return `
  <div class="vcard">
    <div class="vcard-top">
      <div class="vname">${escapeHtml(v.name)}</div>
      <span class="badge">${escapeHtml(v.ext.slice(1).toUpperCase())}</span>
    </div>
    <div class="vmeta">
      ${v.duration ? `<span>${fmtDuration(v.duration)}</span>` : ""}
      <span>${fmtSize(v.size)}</span>
      <span>${escapeHtml(v.codec)}</span>
    </div>
    <div class="vactions">
      ${previewable
        ? `<button class="btn small" data-preview="${encodePath(v.id)}" data-title="${escapeHtml(v.name)}" data-ext="${v.ext}">▶ 预览</button>`
        : ""}
      <select class="ttl-select" id="ttl-${encodePath(v.id).replace(/[^a-zA-Z0-9]/g, "")}">
        <option value="600">10 分钟</option>
        <option value="1800" selected>30 分钟</option>
        <option value="3600">1 小时</option>
        <option value="7200">2 小时</option>
      </select>
      <button class="btn primary small" data-request="${encodePath(v.id)}">申请链接</button>
    </div>
  </div>`;
}

async function requestLink(id, btn) {
  const sel = $(`#ttl-${id.replace(/[^a-zA-Z0-9]/g, "")}`);
  const ttl = sel ? parseInt(sel.value, 10) : 1800;
  btn.disabled = true;
  try {
    const r = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_id: id, ttl }),
    });
    localStorage.setItem("linktok_" + r.id, JSON.stringify({ media: r.media, token: r.token, kind: "media" }));
    const fullUrl = location.origin + r.url;
    copyText(fullUrl, "链接已生成并复制，有效期至 " + fmtClock(r.expires));
    loadLinks();
  } catch (e) {
    toast("申请失败: " + e.message);
  } finally {
    setTimeout(() => { btn.disabled = false; }, 1200);
  }
}

async function requestAndPreview(id, title, ext) {
  try {
    const r = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_id: id, ttl: 300 }),
    });
    const url = location.origin + r.url;
    const video = $("#preview-video");
    const audio = $("#preview-audio");
    video.classList.add("hidden");
    audio.classList.add("hidden");
    $("#preview-title").textContent = title + "（预览链接 5 分钟内有效）";
    if (AUDIO_EXTS.has(ext)) {
      audio.src = url;
      audio.classList.remove("hidden");
    } else {
      video.src = url;
      video.classList.remove("hidden");
    }
    $("#preview-modal").classList.remove("hidden");
    loadLinks();
  } catch (e) {
    toast("预览失败: " + e.message);
  }
}

function closePreview() {
  const v = $("#preview-video"), a = $("#preview-audio");
  v.pause(); a.pause();
  v.removeAttribute("src"); a.removeAttribute("src");
  $("#preview-modal").classList.add("hidden");
}

/* ---------- my links ---------- */
async function loadLinks() {
  const { items } = await api("/api/links");
  const list = $("#link-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无链接，申请后显示在这里</div>';
    return;
  }
  list.innerHTML = items.map((l) => `
    <div class="link-item" data-id="${l.id}" data-expires="${l.expires}" data-kind="${l.kind}">
      <span class="lmedia">${l.kind === "live" ? '<span class="live-badge"><span class="dot"></span>LIVE</span> ' : ""}${escapeHtml(l.target || l.media)}</span>
      <span class="lexp" id="exp-${l.id}"></span>
      <button class="btn small" data-copy-link="${l.id}">复制完整链接</button>
      <button class="btn small danger" data-revoke="${l.id}">撤销</button>
    </div>`).join("");
  list.querySelectorAll("[data-copy-link]").forEach((b) => {
    b.addEventListener("click", () => copyFullLink(b.dataset.copyLink));
  });
  list.querySelectorAll("[data-revoke]").forEach((b) =>
    b.addEventListener("click", () => revokeLink(b.dataset.revoke)));
  tickCountdown();
}

let linkTokens = {};
async function copyFullLink(id) {
  const saved = localStorage.getItem("linktok_" + id);
  if (saved) {
    try {
      const s = JSON.parse(saved);
      const path = s.kind === "live" ? "/live/" : "/play/";
      copyText(`${location.origin}${path}${encodePath(s.media)}?token=${s.token}`);
      return;
    } catch {}
  }
  try {
    const { items } = await api("/api/links");
    const l = items.find((x) => x.id === id);
    if (!l) return;
    const body = l.kind === "live" ? { live_id: l.target, ttl: 600 } : { media_id: l.target, ttl: 600 };
    const r = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    localStorage.setItem("linktok_" + r.id, JSON.stringify({ media: r.media, token: r.token, kind: r.kind }));
    copyText(`${location.origin}${r.url}`, "已重新生成链接并复制");
    loadLinks();
  } catch (e) {
    toast("复制失败: " + e.message);
  }
}

async function revokeLink(id) {
  try {
    await api(`/api/links/${id}`, { method: "DELETE" });
    toast("链接已撤销");
    loadLinks();
  } catch (e) {
    toast("撤销失败: " + e.message);
  }
}

function tickCountdown() {
  const now = Date.now() / 1000;
  $$(".link-item").forEach((el) => {
    const exp = parseFloat(el.dataset.expires);
    const left = exp - now;
    const span = el.querySelector(".lexp");
    if (!span) return;
    if (left <= 0) {
      span.textContent = "已过期";
      span.classList.add("warn");
    } else {
      const m = Math.floor(left / 60);
      const s = Math.floor(left % 60);
      span.textContent = `${m}:${String(s).padStart(2, "0")} 后过期`;
      span.classList.toggle("warn", left < 120);
    }
  });
}

/* ---------- live publish (HTTP-TS ingest) ---------- */
let publishUrlCache = "";

async function getStreamKey() {
  const sid = $("#pub-stream-id").value.trim();
  if (!/^[A-Za-z0-9_\-]{3,64}$/.test(sid)) {
    toast("流名称需 3-64 位，仅限字母/数字/_-");
    return;
  }
  const ttl = parseInt($("#pub-ttl").value, 10);
  try {
    const r = await api("/api/stream-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stream_id: sid, ttl }),
    });
    publishUrlCache = location.origin + r.url;
    $("#publish-url").textContent = publishUrlCache;
    $("#ffmpeg-cmd").textContent =
      `ffmpeg -re -i 输入源 -c copy -f mpegts "${publishUrlCache}"`;
    $("#publish-expires").textContent = fmtClock(r.expires);
    $("#publish-result").classList.remove("hidden");
    localStorage.setItem("streamkey_" + r.id, JSON.stringify({ stream_id: sid, key: r.key }));
    toast("推流密钥已生成，有效期至 " + fmtClock(r.expires));
    loadKeys();
  } catch (e) {
    toast("获取失败: " + e.message);
  }
}

async function loadKeys() {
  const { items } = await api("/api/stream-keys");
  const list = $("#key-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无密钥，获取后显示在这里</div>';
    return;
  }
  list.innerHTML = items.map((k) => `
    <div class="stream-item" data-id="${k.id}">
      <span class="sname">${escapeHtml(k.stream_id)}</span>
      <span class="lexp muted">至 ${fmtClock(k.expires)}</span>
      <button class="btn small" data-copy-key="${k.id}">复制推流地址</button>
      <button class="btn small danger" data-revoke-key="${k.id}">撤销</button>
    </div>`).join("");
  list.querySelectorAll("[data-copy-key]").forEach((b) =>
    b.addEventListener("click", () => copyStreamKey(b.dataset.copyKey)));
  list.querySelectorAll("[data-revoke-key]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/stream-keys/${b.dataset.revokeKey}`, { method: "DELETE" });
      toast("密钥已撤销");
      loadKeys();
    }));
}

async function copyStreamKey(id) {
  const saved = localStorage.getItem("streamkey_" + id);
  let url = "";
  if (saved) {
    try {
      const s = JSON.parse(saved);
      url = `${location.origin}/ingest/${encodePath(s.stream_id)}?key=${s.key}`;
    } catch {}
  }
  if (!url) {
    try {
      const { items } = await api("/api/stream-keys");
      const k = items.find((x) => x.id === id);
      if (!k) return;
      const r = await api("/api/stream-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stream_id: k.stream_id, ttl: 600 }),
      });
      localStorage.setItem("streamkey_" + r.id, JSON.stringify({ stream_id: r.stream_id, key: r.key }));
      url = location.origin + r.url;
    } catch (e) {
      toast("复制失败: " + e.message);
      return;
    }
  }
  copyText(url, "推流地址已复制");
}

/* ---------- live hall ---------- */
async function loadLive() {
  const { items } = await api("/api/live");
  $("#live-count").textContent = items.length ? `（${items.length} 场）` : "";
  const list = $("#live-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无在线直播</div>';
    return;
  }
  list.innerHTML = items.map((l) => `
    <div class="live-item">
      <span class="lname">${escapeHtml(l.id)}</span>
      <span class="live-badge"><span class="dot"></span>LIVE</span>
      <span class="lexp muted">推流者 ${escapeHtml(l.publisher)} · ${l.viewers} 人观看 · ${fmtDuration(Date.now() / 1000 - l.started)}</span>
      <button class="btn primary small" data-watch="${encodePath(l.id)}">申请观看链接</button>
    </div>`).join("");
  list.querySelectorAll("[data-watch]").forEach((b) =>
    b.addEventListener("click", () => requestWatchLink(b.dataset.watch)));
}

async function requestWatchLink(liveId) {
  try {
    const r = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ live_id: liveId, ttl: 1800 }),
    });
    localStorage.setItem("linktok_" + r.id, JSON.stringify({ media: r.media, token: r.token, kind: "live" }));
    copyText(location.origin + r.url, "观看链接已复制（VLC 打开网络串流），有效期至 " + fmtClock(r.expires));
    loadLinks();
  } catch (e) {
    toast("申请失败: " + e.message);
  }
}

/* ---------- boot ---------- */
async function boot() {
  try {
    const r = await api("/api/auth/me");
    if (r.user) {
      currentUser = r.user;
      $("#user-name").textContent = r.user.username;
      $("#user-pills").style.display = "";
      $("#auth-view").classList.add("hidden");
      $("#main-view").classList.remove("hidden");
      await Promise.all([loadVideos(), loadLinks(), loadKeys(), loadLive()]);
      setInterval(tickCountdown, 1000);
      setInterval(loadLinks, 30000);
      setInterval(loadLive, 5000);
      return;
    }
  } catch {}
  $("#auth-view").classList.remove("hidden");
  $("#main-view").classList.add("hidden");
  switchTab("login");
}

$$(".auth-tab").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
$("#auth-form").addEventListener("submit", submitAuth);
$("#btn-logout").addEventListener("click", logout);
$("#btn-refresh").addEventListener("click", loadVideos);
$("#btn-get-key").addEventListener("click", getStreamKey);
$("#btn-copy-publish").addEventListener("click", () => publishUrlCache && copyText(publishUrlCache, "推流地址已复制"));
$("#preview-close").addEventListener("click", closePreview);
$("#preview-modal").addEventListener("click", (e) => {
  if (e.target === $("#preview-modal")) closePreview();
});

boot();
