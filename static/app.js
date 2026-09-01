"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const encodePath = (id) =>
  id.split(/[\\/]/).map((s) => encodeURIComponent(s)).join("/");

const PREVIEWABLE = new Set([".mp4", ".m4v", ".webm", ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"]);
const isPreviewable = (ext) => PREVIEWABLE.has(ext);
const isAudio = (ext) => [".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"].includes(ext);

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtDuration(sec) {
  if (sec == null) return "时长未知";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.round(sec % 60);
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
               : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtClock(t) {
  return new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.remove("show"), 2400);
}

function copyText(text, okMsg) {
  const done = () => toast(okMsg || "已复制播放链接（粘贴到 VLC 打开网络串流）");
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

/* ---------- status ---------- */
async function loadStatus() {
  const s = await api("/api/status");
  $("#uptime").textContent = fmtDuration(s.uptime) || "0:00";
  $("#hls-status").textContent = s.hls_available ? "可用" : "未安装 ffmpeg";
  $("#pill-hls").style.borderColor = s.hls_available ? "var(--green)" : "var(--amber)";
  const d = s.dlna || { enabled: false };
  $("#dlna-status").textContent = d.enabled ? "已启用" : "未启用";
  $("#pill-dlna").style.borderColor = d.enabled ? "var(--green)" : "var(--amber)";
  $("#dlna-name").textContent = d.friendly_name || "--";
  $("#dlna-name2").textContent = d.friendly_name || "--";
  $("#dlna-udn").textContent = d.udn || "--";
  $("#dlna-ip").textContent = d.enabled ? `http://${d.ip}:${d.port}` : "--";
  $("#dlna-msearch").textContent = d.msearch || 0;
}

/* ---------- users ---------- */
async function loadUsers() {
  const { items } = await api("/api/admin/users");
  const pending = items.filter((u) => u.status === "pending");
  const active = items.filter((u) => u.status === "active");

  $("#pending-count").textContent = pending.length ? `（${pending.length} 人）` : "";
  $("#pending-list").innerHTML = pending.length
    ? pending.map((u) => `
      <div class="stream-item">
        <span class="sname">${escapeHtml(u.username)}</span>
        <span class="lexp muted">申请于 ${fmtClock(u.created)}</span>
        <button class="btn small primary" data-approve="${escapeHtml(u.username)}">批准加入白名单</button>
        <button class="btn small danger" data-reject="${escapeHtml(u.username)}">拒绝</button>
      </div>`).join("")
    : '<div class="empty">暂无待审批用户</div>';

  $("#user-list").innerHTML = active.length
    ? active.map((u) => `
      <div class="stream-item">
        <span class="sname">${escapeHtml(u.username)}</span>
        <span class="lexp muted">批准于 ${fmtClock(u.approved)}</span>
        <button class="btn small danger" data-remove="${escapeHtml(u.username)}">移出白名单</button>
      </div>`).join("")
    : '<div class="empty">暂无已激活用户</div>';

  $("#pending-list").querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", () => userAction("approve", b.dataset.approve)));
  $("#pending-list").querySelectorAll("[data-reject]").forEach((b) =>
    b.addEventListener("click", () => userAction("reject", b.dataset.reject)));
  $("#user-list").querySelectorAll("[data-remove]").forEach((b) =>
    b.addEventListener("click", () => userAction("remove", b.dataset.remove)));
}

async function userAction(action, username) {
  try {
    if (action === "approve") {
      await api(`/api/admin/users/${encodeURIComponent(username)}/approve`, { method: "POST" });
      toast(`已批准 ${username} 加入白名单`);
    } else if (action === "reject") {
      await api(`/api/admin/users/${encodeURIComponent(username)}/reject`, { method: "POST" });
      toast(`已拒绝 ${username} 的注册申请`);
    } else {
      await api(`/api/admin/users/${encodeURIComponent(username)}`, { method: "DELETE" });
      toast(`已将 ${username} 移出白名单`);
    }
    loadUsers();
  } catch (e) {
    toast("操作失败: " + e.message);
  }
}

/* ---------- admin links ---------- */
async function loadAdminLinks() {
  const { items } = await api("/api/admin/links");
  $("#admin-link-list").innerHTML = items.length
    ? items.map((l) => `
      <div class="stream-item">
        <span class="sname">${escapeHtml(l.media)}</span>
        <span class="lexp muted">${escapeHtml(l.user)} · 至 ${fmtClock(l.expires)}</span>
        <button class="btn small danger" data-admin-revoke="${l.id}">撤销</button>
      </div>`).join("")
    : '<div class="empty">暂无有效链接</div>';
  $("#admin-link-list").querySelectorAll("[data-admin-revoke]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/admin/links/${b.dataset.adminRevoke}`, { method: "DELETE" });
      loadAdminLinks();
    }));
}

/* ---------- media ---------- */
async function loadVideos() {
  const { items } = await api("/api/videos");
  $("#video-count").textContent = items.length;
  const grid = $("#video-grid");
  if (!items.length) {
    grid.innerHTML = '<div class="empty">source 目录为空 — 将视频文件放入 source/ 后点击刷新</div>';
    return;
  }
  grid.innerHTML = items.map(renderCard).join("");
  grid.querySelectorAll("[data-link]").forEach((b) =>
    b.addEventListener("click", () => makeVlcLink(b.dataset.link)));
  grid.querySelectorAll("[data-preview]").forEach((b) =>
    b.addEventListener("click", () => openPreview(b.dataset.preview, b.dataset.title, b.dataset.ext)));
  grid.querySelectorAll("[data-hls-start]").forEach((b) =>
    b.addEventListener("click", () => startHls(b.dataset.hlsStart, b)));
}

function renderCard(v) {
  const res = v.width ? `${v.width}×${v.height}` : null;
  const previewBtn = isPreviewable(v.ext)
    ? `<button class="btn small" data-preview="${encodePath(v.id)}" data-title="${escapeHtml(v.name)}" data-ext="${v.ext}">▶ 浏览器预览</button>`
    : "";
  return `
  <div class="vcard" data-id="${encodePath(v.id)}">
    <div class="vcard-top">
      <div class="vname">${escapeHtml(v.name)}</div>
      <span class="badge">${escapeHtml(v.ext.slice(1).toUpperCase())}</span>
    </div>
    <div class="vmeta">
      <span>${fmtDuration(v.duration)}</span>
      <span>${fmtSize(v.size)}</span>
      <span>${escapeHtml(v.codec)}</span>
      ${res ? `<span>${res}</span>` : ""}
    </div>
    <div class="vactions">
      <button class="btn primary small" data-link="${encodePath(v.id)}">生成 VLC 限时链接</button>
      ${previewBtn}
      <button class="btn small" data-hls-start="${encodePath(v.id)}">HLS 直播</button>
    </div>
  </div>`;
}

async function makeVlcLink(id) {
  try {
    const r = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_id: id, ttl: 1800 }),
    });
    copyText(location.origin + r.url, "VLC 限时链接已复制，有效期至 " + fmtClock(r.expires));
    loadAdminLinks();
  } catch (e) {
    toast("生成失败: " + e.message);
  }
}

function openPreview(id, title, ext) {
  const video = $("#preview-video");
  const audio = $("#preview-audio");
  video.classList.add("hidden");
  audio.classList.add("hidden");
  const url = `/play/${id}`;
  $("#preview-title").textContent = title;
  if (isAudio(ext)) {
    audio.src = url;
    audio.classList.remove("hidden");
  } else {
    video.src = url;
    video.classList.remove("hidden");
  }
  $("#preview-modal").classList.remove("hidden");
}

function closePreview() {
  const video = $("#preview-video");
  const audio = $("#preview-audio");
  video.pause(); audio.pause();
  video.removeAttribute("src"); audio.removeAttribute("src");
  $("#preview-modal").classList.add("hidden");
}

/* ---------- HLS ---------- */
async function startHls(id, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/api/streams/${id}`, { method: "POST" });
    toast(`HLS 直播已启动: ${r.url}`);
    loadStreams();
  } catch (e) {
    toast("启动失败: " + e.message);
  } finally {
    setTimeout(() => { btn.disabled = false; }, 1500);
  }
}

async function stopHls(id) {
  await api(`/api/streams/${id}`, { method: "DELETE" });
  loadStreams();
}

async function loadStreams() {
  const { items } = await api("/api/streams");
  const list = $("#stream-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无运行中的 HLS 流</div>';
    return;
  }
  list.innerHTML = items.map((s) => `
    <div class="stream-item">
      <span class="sname">${escapeHtml(s.id)}</span>
      <span class="surl">${location.origin}${s.url}</span>
      <span class="state ${s.state}">${s.state}</span>
      <button class="btn small" data-stream-copy="${escapeHtml(location.origin + s.url)}">复制</button>
      <button class="btn small danger" data-stream-stop="${encodePath(s.id)}">停止</button>
    </div>`).join("");
  list.querySelectorAll("[data-stream-copy]").forEach((b) =>
    b.addEventListener("click", () => copyText(b.dataset.streamCopy)));
  list.querySelectorAll("[data-stream-stop]").forEach((b) =>
    b.addEventListener("click", () => stopHls(b.dataset.streamStop)));
}

/* ---------- boot ---------- */
async function boot() {
  try {
    const r = await api("/api/auth/me");
    if (!r.admin) {
      location.href = "/admin/login.html";
      return;
    }
  } catch {
    location.href = "/admin/login.html";
    return;
  }
  await Promise.all([loadStatus(), loadVideos(), loadStreams(), loadUsers(), loadAdminLinks()]);
}

$("#btn-refresh").addEventListener("click", () => { loadVideos(); loadStatus(); });
$("#btn-admin-logout").addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST" }).catch(() => {});
  location.href = "/admin/login.html";
});
$("#preview-close").addEventListener("click", closePreview);
$("#preview-modal").addEventListener("click", (e) => {
  if (e.target === $("#preview-modal")) closePreview();
});

boot();
setInterval(loadStreams, 3000);
setInterval(() => { loadUsers(); loadAdminLinks(); }, 15000);
setInterval(loadStatus, 10000);
