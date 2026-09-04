"use strict";

/* ============ 基础工具 ============ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const t = await r.text();
  return t ? JSON.parse(t) : {};
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 2600);
}
function errToast(e) { toast("⚠️ " + (e.message || e), "err"); }

const state = { status: null, settings: null, tab: "account", dirStack: [{ file_id: "", name: "根目录" }] };
let timer = null;
function clearTimer() { if (timer) { clearInterval(timer); timer = null; } }

/* ============ 状态条 ============ */
async function refreshStatus() {
  try {
    const s = await api("GET", "/status");
    state.status = s;
    const lb = $("#login-badge");
    lb.className = "badge " + (s.logged_in ? "ok" : "err");
    lb.innerHTML = `<span class="dot"></span> ${s.logged_in ? "光鸭已登录" : "未登录"}`;
    const wb = $("#worker-badge");
    wb.className = "badge " + (s.worker_running ? "ok" : "dim");
    wb.innerHTML = `<span class="dot"></span> ${s.worker_running ? "监听中" : "未运行"}`;
    return s;
  } catch (e) { return null; }
}

/* ============ 路由 ============ */
const titles = { dashboard: "概览", tasks: "转存任务", channels: "频道管理", history: "监控历史", settings: "系统设置" };
function route() {
  clearTimer();
  const key = (location.hash.replace("#/", "") || "dashboard").split("/")[0];
  $("#page-title").textContent = titles[key] || "概览";
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === key));
  (views[key] || views.dashboard)();
}

const views = {};

/* ============ 概览 ============ */
views.dashboard = async function () {
  const s = await api("GET", "/status");
  state.status = s;
  const stats = s.stats || {};
  const card = (num, label) => `<div class="stat"><div class="num">${esc(num)}</div><div class="label">${esc(label)}</div></div>`;
  $("#content").innerHTML = `
    <div class="grid grid-3" style="margin-bottom:16px">
      ${card(s.channel_count, "监听频道数")}
      ${card(s.logged_in ? "已登录" : "未登录", "光鸭账号")}
      ${card(s.worker_running ? "运行中" : "未运行", "监听状态")}
      ${card(stats.submitted ?? 0, "已提交转存")}
      ${card(stats.skipped ?? 0, "已跳过")}
      ${card(stats.failed ?? 0, "失败")}
    </div>
    <div class="grid grid-2">
      <div class="card">
        <h3>监听控制</h3>
        <p class="muted">后台线程会按设定间隔轮询频道，抓到磁力/迅雷/电驴链接就推给光鸭离线下载（光鸭服务器跑 BT，不占你带宽）。</p>
        <div class="row">
          ${s.worker_running
            ? `<button class="btn danger" id="btn-stop">⏹ 停止监听</button>`
            : `<button class="btn primary" id="btn-start">▶ 启动监听</button>`}
          ${s.logged_in ? "" : `<span class="badge warn">请先到「系统设置 → 光鸭账号」登录</span>`}
        </div>
        <p class="hint">提示：未登录无法启动。首次登录请前往 系统设置 → 光鸭账号，用光鸭 App 扫码授权。</p>
        <p class="hint">自动分类：<b>${s.organize_enabled ? "已开启" : "已关闭"}</b>${s.organize_enabled ? `（${s.organize_structure === "two_level" ? "两级目录" : "扁平目录"}，资源会按华语电影/国产剧等自动建子目录）` : "（全部平铺到转存目录）"}，可在 系统设置 → 自动分类 里调整。</p>
      </div>
      <div class="card">
        <h3>最近日志</h3>
        <div class="logs" id="dash-logs">加载中…</div>
      </div>
    </div>`;
  $("#btn-start")?.addEventListener("click", async () => {
    try { await api("POST", "/worker/start"); toast("已启动监听", "ok"); route(); }
    catch (e) { errToast(e); }
  });
  $("#btn-stop")?.addEventListener("click", async () => {
    try { await api("POST", "/worker/stop"); toast("已停止"); route(); }
    catch (e) { errToast(e); }
  });
  renderLogs("#dash-logs", 60);
};

async function renderLogs(sel, lines = 60) {
  try {
    const r = await api("GET", "/logs?lines=" + lines);
    $(sel).textContent = (r.logs && r.logs.length) ? r.logs.join("\n") : "（暂无日志）";
    $(sel).scrollTop = $(sel).scrollHeight;
  } catch (e) { $(sel).textContent = "日志读取失败：" + e.message; }
}

/* ============ 转存任务 ============ */
views.tasks = async function () {
  $("#content").innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">光鸭离线下载任务</h3>
        <div class="row">
          <label class="row" style="margin:0"><input type="checkbox" id="auto" checked style="width:auto"> 自动刷新(10s)</label>
          <button class="btn" id="refresh">🔄 刷新</button>
          <button class="btn danger" id="clean">🧹 清理已完成</button>
        </div>
      </div>
      <p class="hint">数据实时来自光鸭云盘。任务完成即表示文件已离线下载到你的光鸭网盘。</p>
      <div id="task-body"><div class="empty">加载中…</div></div>
    </div>`;
  const draw = async () => {
    let data;
    try { data = await api("GET", "/tasks"); }
    catch (e) { $("#task-body").innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`; return; }
    const ts = data.tasks || [];
    if (!ts.length) { $("#task-body").innerHTML = `<div class="empty">暂无离线下载任务</div>`; return; }
    $("#task-body").innerHTML = `<table><thead><tr><th>名称</th><th>状态</th><th>进度</th><th>大小</th><th>信息</th></tr></thead><tbody>
      ${ts.map((t) => {
        const color = t.status === 2 ? "ok" : t.status === 3 || t.status === 5 ? "err" : t.status === 4 ? "warn" : "dim";
        return `<tr>
          <td>${esc(t.name || "(未知)")}</td>
          <td><span class="badge ${color}">${esc(t.status_text)}</span></td>
          <td style="min-width:140px"><div class="bar"><i style="width:${t.progress}%"></i></div><span class="pill">${t.progress}%</span></td>
          <td class="muted">${fmtSize(t.size)}</td>
          <td class="muted">${esc(t.message || "")}</td>
        </tr>`;
      }).join("")}
    </tbody></table>`;
  };
  await draw();
  $("#refresh").addEventListener("click", draw);
  $("#clean").addEventListener("click", async () => {
    try { await api("POST", "/tasks/cleanup"); toast("已清理已完成任务", "ok"); draw(); }
    catch (e) { errToast(e); }
  });
  $("#auto").addEventListener("change", (e) => {
    if (e.target.checked) timer = setInterval(draw, 10000); else clearTimer();
  });
  timer = setInterval(draw, 10000);
};

function fmtSize(b) {
  b = Number(b) || 0;
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
}

/* ============ 频道管理（独立页） ============ */
views.channels = async function () { await renderChannelManager("#content", false); };

async function renderChannelManager(sel, inSettings) {
  const r = await api("GET", "/channels");
  const ch = r.channels || [];
  const disc = (state.settings && state.settings.discovery) || (await api("GET", "/settings")).discovery;
  $(sel).innerHTML = `
    <div class="card">
      <h3>频道列表（${ch.length}）</h3>
      <div class="row" style="margin-bottom:12px">
        <input type="text" id="ch-input" placeholder="输入频道用户名，如 ysh365（去掉 @ 和 t.me/）" />
        <button class="btn primary" id="ch-add">+ 添加</button>
      </div>
      ${ch.length ? `<div id="ch-tags">${ch.map((c) => `<span class="tag">${esc(c)} <a href="#" class="ch-del" data-c="${esc(c)}" style="color:var(--err);text-decoration:none">✕</a></span>`).join(" ")}</div>`
        : `<div class="empty">还没有频道，添加几个公开影视频道试试</div>`}
      <p class="hint">${inSettings ? "" : "修改即时生效（网页抓取模式）。"} 自动发现：<b>${disc.enabled ? "已开启" : "已关闭"}</b>，${disc.interval_hours}h 检查一次新的影视频道并自动加入。</p>
      ${inSettings ? "" : `<div class="right-action"><a class="btn ghost" href="#/settings">前往系统设置 →</a></div>`}
    </div>`;
  $("#ch-add")?.addEventListener("click", () => addChannel($("#ch-input").value, () => renderChannelManager(sel, inSettings)));
  $("#ch-input")?.addEventListener("keydown", (e) => { if (e.key === "Enter") $("#ch-add").click(); });
  $$(".ch-del", $(sel)).forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    delChannel(a.dataset.c, () => renderChannelManager(sel, inSettings));
  }));
}

async function addChannel(name, cb) {
  if (!name.trim()) return;
  try { const r = await api("POST", "/channels", { name }); toast(r.added ? "已添加：" + name.trim() : "已存在", r.added ? "ok" : ""); cb && cb(); }
  catch (e) { errToast(e); }
}
async function delChannel(name, cb) {
  try { await api("DELETE", "/channels/" + encodeURIComponent(name)); toast("已删除：" + name, "ok"); cb && cb(); }
  catch (e) { errToast(e); }
}

/* ============ 监控历史 ============ */
views.history = async function () {
  const el = $("#content");
  el.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">转存监控历史</h3>
        <div class="row">
          <select id="h-status">
            <option value="">全部</option>
            <option value="submitted">已转存</option>
            <option value="upgraded">洗版转存</option>
            <option value="skipped">已跳过</option>
            <option value="failed">失败</option>
          </select>
          <button class="btn" id="h-refresh">🔄 刷新</button>
        </div>
      </div>
      <p class="hint">这里记录了频道里每一条被命中的资源、处理结果（转存 / 跳过 / 洗版 / 失败）与原因，方便你回看自动监控是否按预期工作。</p>
      <div id="h-body"><div class="empty">加载中…</div></div>
    </div>`;
  const draw = async () => {
    const st = $("#h-status").value;
    let data;
    try { data = await api("GET", "/history?limit=200" + (st ? "&status=" + encodeURIComponent(st) : "")); }
    catch (e) { $("#h-body").innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`; return; }
    const items = data.items || [];
    if (!items.length) { $("#h-body").innerHTML = `<div class="empty">暂无记录</div>`; return; }
    const badge = (s) => {
      const m = { submitted: "ok", upgraded: "ok", skipped: "dim", failed: "err" };
      return `<span class="badge ${m[s] || "dim"}">${esc(s)}</span>`;
    };
    $("#h-body").innerHTML = `<table><thead><tr><th>标题</th><th>频道</th><th>分类</th><th>状态</th><th>原因</th><th>时间</th></tr></thead><tbody>
      ${items.map((r) => `<tr>
        <td>${esc(r.title || "(无标题)")}</td>
        <td class="muted">${esc(r.channel || "")}</td>
        <td class="muted">${esc(r.category || "")}</td>
        <td>${badge(r.status)}</td>
        <td class="muted">${esc(r.reason || "")}</td>
        <td class="muted">${esc(r.updated_text || "")}</td>
      </tr>`).join("")}
    </tbody></table>`;
  };
  await draw();
  $("#h-refresh").addEventListener("click", draw);
  $("#h-status").addEventListener("change", draw);
};

/* ============ 系统设置 ============ */
views.settings = async function () {
  const s = await api("GET", "/settings");
  state.settings = s;
  $("#content").innerHTML = `
    <div class="card">
      <div class="tabs" id="tabs">
        <button data-t="account">光鸭账号</button>
        <button data-t="telegram">Telegram 账号</button>
        <button data-t="dir">转存目录</button>
        <button data-t="channels">频道配置</button>
        <button data-t="filter">过滤规则</button>
        <button data-t="organize">自动分类</button>
        <button data-t="runtime">运行参数</button>
        <button data-t="bot">TG 机器人</button>
        <button data-t="tmdb">TMDB</button>
        <button data-t="notify">通知</button>
        <button data-t="backup">备份与还原</button>
        <button data-t="about">关于</button>
      </div>
      <div id="tab-body"></div>
    </div>`;
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.t;
    renderTab();
    $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.t === state.tab));
  }));
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.t === state.tab));
  renderTab();
};

async function renderTab() {
  const S = state.settings;
  const body = $("#tab-body");
  if (state.tab === "account") {
    const ok = state.status?.logged_in;
    body.innerHTML = `
      <div class="kv"><span>登录状态</span><span class="badge ${ok ? "ok" : "err"}"><span class="dot"></span> ${ok ? "已登录" : "未登录"}</span></div>
      <p class="hint">光鸭令牌有效期约 2 小时，程序会自动用 refresh_token 续期，无需频繁登录。仅当令牌彻底失效才需重新扫码。</p>
      <div class="row">
        <button class="btn primary" id="login-btn">📷 扫码登录</button>
        ${ok ? `<button class="btn danger" id="logout-btn">退出登录</button>` : ""}
      </div>`;
    $("#login-btn")?.addEventListener("click", openLoginModal);
    $("#logout-btn")?.addEventListener("click", async () => {
      try { await api("POST", "/guangya/logout"); toast("已退出登录", "ok"); await refreshStatus(); views.settings(); }
      catch (e) { errToast(e); }
    });
  } else if (state.tab === "telegram") {
    let ok = false;
    try { const r = await api("GET", "/userbot/status"); ok = !!r.logged_in; } catch (e) {}
    state.userbot_logged_in = ok;
    const tg = (S.telegram || {});
    body.innerHTML = `
      <div class="kv"><span>登录状态</span><span class="badge ${ok ? "ok" : "err"}"><span class="dot"></span> ${ok ? "已登录" : "未登录"}</span></div>
      <p class="hint">用你自己的 TG 账号实时收消息（官方接口，不限流、能收私有频道、秒级到）。建议用<b>小号</b>。需先在 <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a> 申请 api_id / api_hash（免费，2 分钟）。</p>
      <label>api_id</label>
      <input type="text" id="tg-apiid" value="${esc(tg.api_id || "")}" placeholder="如 1234567" />
      <label>api_hash</label>
      <input type="text" id="tg-apihash" value="${esc(tg.api_hash || "")}" placeholder="如 a1b2c3d4e5f6..." />
      <div class="row" style="margin-top:10px">
        <button class="btn primary" id="tg-save">保存凭据</button>
        ${ok ? `<button class="btn danger" id="tg-logout">退出登录</button>` : `<button class="btn" id="tg-login">📱 登录账号</button>`}
      </div>
      <p class="hint">保存后点「登录账号」，按提示填手机号 → 收验证码 → 填入即可。代理已在「运行参数」里配置，会自动走，无需在此填。</p>`;
    $("#tg-save").addEventListener("click", async () => {
      S.telegram = S.telegram || {};
      S.telegram.api_id = $("#tg-apiid").value.trim();
      S.telegram.api_hash = $("#tg-apihash").value.trim();
      try { await api("PUT", "/settings", S); toast("已保存 Telegram 凭据", "ok"); }
      catch (e) { errToast(e); }
    });
    $("#tg-login")?.addEventListener("click", openUserbotLoginModal);
    $("#tg-logout")?.addEventListener("click", async () => {
      try { await api("POST", "/userbot/logout"); state.userbot_logged_in = false; toast("已退出登录", "ok"); renderTab(); }
      catch (e) { errToast(e); }
    });
  } else if (state.tab === "dir") {
    body.innerHTML = `
      <label>当前转存目录</label>
      <div id="dir-crumb" class="row"></div>
      <div id="dir-list" class="grid" style="margin-top:10px"></div>
      <label>已选目录 fileId（由上方浏览自动填充，也可手填）</label>
      <input type="text" id="out-parent" value="${esc(S.output.parent_id)}" placeholder="留空 = 光鸭默认离线下载目录" />
      <label>备用：目录名称（仅当目标目录已存在时生效，优先使用上面的 fileId）</label>
      <input type="text" id="out-path" value="${esc(S.output.save_path)}" placeholder="例如：TG转存" />
      <p class="hint">离线下载会把文件放到这里。建议建一个专门目录（如「TG转存」）方便管理。</p>
      <button class="btn primary" id="dir-save" style="margin-top:12px">保存转存目录</button>`;
    renderDirCrumb();
    renderDirList();
    $("#dir-save").addEventListener("click", async () => {
      S.output.parent_id = $("#out-parent").value.trim();
      S.output.save_path = $("#out-path").value.trim();
      try { await api("PUT", "/settings", S); toast("已保存转存目录", "ok"); }
      catch (e) { errToast(e); }
    });
  } else if (state.tab === "channels") {
    renderChannelManager("#tab-body", true).then(() => {
      // 在频道管理下方补一段自动发现配置
      const disc = state.settings.discovery;
      const extra = document.createElement("div");
      extra.className = "card";
      extra.style.marginTop = "16px";
      extra.innerHTML = `
        <h3>频道自动发现</h3>
        <label><input type="checkbox" id="disc-on" ${disc.enabled ? "checked" : ""} style="width:auto"> 开启自动发现新频道</label>
        <label>检查间隔（小时）</label>
        <input type="number" id="disc-int" step="0.5" min="0.5" value="${esc(disc.interval_hours)}" />
        <label>种子文件（本地，每行一个 @用户名 或 t.me/xxx）</label>
        <input type="text" id="disc-seed" value="${esc(disc.seed_file)}" placeholder="如 seeds/channels_seed.txt" />
        <label>种子链接（一行一个，可选，用于从网络清单补充频道）</label>
        <textarea id="disc-urls" rows="3" placeholder="https://raw.githubusercontent.com/.../channels.md">${esc((disc.seed_urls || []).join("\n"))}</textarea>
        <label>磁力验证阈值（新频道至少要有 N 条磁力才入库；0=跳过验证全入库）</label>
        <input type="number" id="disc-vt" min="0" max="10" value="${esc(disc.verify_threshold ?? 1)}" />
        <p class="hint">开启后，新发现频道会先扫 1 页 t.me/s/xxx，有磁力才追加进监控列表，避免把纯网盘分享/广告频道拉进来。</p>
        <button class="btn primary" id="disc-save" style="margin-top:12px">保存自动发现设置</button>`;
      $("#tab-body").appendChild(extra);
      $("#disc-save").addEventListener("click", async () => {
        state.settings.discovery = {
          enabled: $("#disc-on").checked,
          interval_hours: Number($("#disc-int").value) || 24,
          verify_threshold: Math.max(0, Number($("#disc-vt").value) || 1),
          seed_file: $("#disc-seed").value.trim(),
          seed_urls: $("#disc-urls").value.split("\n").map((x) => x.trim()).filter(Boolean),
        };
        try { await api("PUT", "/settings", state.settings); toast("已保存自动发现设置", "ok"); }
        catch (e) { errToast(e); }
      });
    });
  } else if (state.tab === "filter") {
    body.innerHTML = `
      <label>包含关键词（命中任意一个才转存，留空=全部转）</label>
      <textarea id="f-inc" rows="3" placeholder="每行一个，如 1080P / 4K / 电影">${esc((S.filter.include_keywords || []).join("\n"))}</textarea>
      <label>排除关键词（命中任意一个则跳过）</label>
      <textarea id="f-exc" rows="3" placeholder="每行一个，如 预告 / 样本 / 预告片">${esc((S.filter.exclude_keywords || []).join("\n"))}</textarea>
      <label>最低画质</label>
      <select id="f-res">
        <option value="">不限</option>
        ${["720P", "1080P", "2160P", "4K"].map((r) => `<option value="${r}" ${S.filter.min_resolution === r ? "selected" : ""}>${r}</option>`).join("")}
      </select>
      <button class="btn primary" id="filter-save" style="margin-top:12px">保存过滤规则</button>`;
    $("#filter-save").addEventListener("click", async () => {
      S.filter = {
        include_keywords: $("#f-inc").value.split("\n").map((x) => x.trim()).filter(Boolean),
        exclude_keywords: $("#f-exc").value.split("\n").map((x) => x.trim()).filter(Boolean),
        min_resolution: $("#f-res").value,
      };
      try { await api("PUT", "/settings", S); toast("已保存过滤规则", "ok"); }
      catch (e) { errToast(e); }
    });
  } else if (state.tab === "organize") {
    let O = {};
    try { O = await api("GET", "/organize"); } catch (e) {
      body.innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`; return;
    }
    state.organize = O;
    const kinds = O.kinds || [], regions = O.regions || [];
    const row = (m) => `
      <tr data-k="${esc(m.kind)}" data-r="${esc(m.region)}">
        <td><select class="og-kind">${kinds.map((k) => `<option value="${esc(k.value)}" ${k.value === m.kind ? "selected" : ""}>${esc(k.name)}</option>`).join("")}</select></td>
        <td><select class="og-region">${regions.map((r) => `<option value="${esc(r.value)}" ${r.value === m.region ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></td>
        <td><input type="text" class="og-name" value="${esc(m.name)}" /></td>
        <td><button class="btn ghost og-del" style="padding:4px 8px">✕</button></td>
      </tr>`;
    body.innerHTML = `
      <label><input type="checkbox" id="og-on" ${O.enabled ? "checked" : ""} style="width:auto"> 开启自动分类转存</label>
      <p class="hint">开启后，频道里的资源会按「内容形态 + 地区」自动在转存目录下建子目录（如 <code>华语电影</code>、<code>国产剧</code>），再转存进去——不用手动整理。关闭则全部平铺到转存根目录。</p>
      <label>目录结构</label>
      <select id="og-struct">
        <option value="flat" ${O.structure === "flat" ? "selected" : ""}>扁平：华语电影 / 欧美电影 / 国产剧 / 日韩剧 …（推荐）</option>
        <option value="two_level" ${O.structure === "two_level" ? "selected" : ""}>两级：电影/华语、电视剧/欧美 …</option>
      </select>
      <label><input type="checkbox" id="og-create" ${O.create_missing ? "checked" : ""} style="width:auto"> 分类目录不存在时自动创建</label>
      <label>判定不出类别时放入</label>
      <input type="text" id="og-unknown" value="${esc(O.unknown_dir)}" placeholder="未分类" />

      <h3 style="margin-top:18px">分类目录对照表</h3>
      <p class="hint">想改目录名（比如把「国产剧」改成「华语剧集」）直接编辑即可；也可以加行自定义。</p>
      <table id="og-table">
        <thead><tr><th>内容形态</th><th>地区</th><th>转存到子目录</th><th></th></tr></thead>
        <tbody>${(O.mapping || []).map(row).join("")}</tbody>
      </table>
      <div class="row" style="margin-top:8px">
        <button class="btn ghost" id="og-add">+ 添加一行</button>
        <button class="btn primary" id="og-save">保存自动分类设置</button>
      </div>

      <h3 style="margin-top:20px">分类试算</h3>
      <p class="hint">粘贴一条频道消息标题，看看它会被分到哪个目录。上方改动会实时参与试算（无需先保存）。</p>
      <div class="row">
        <input type="text" id="og-test" placeholder="如：流浪地球2 2023 4K 国语中字" />
        <button class="btn" id="og-test-btn">试算</button>
      </div>
      <div id="og-test-out" class="hint" style="margin-top:8px"></div>`;

    const collect = () => ({
      enabled: $("#og-on").checked,
      structure: $("#og-struct").value,
      create_missing: $("#og-create").checked,
      unknown_dir: $("#og-unknown").value.trim() || "未分类",
      mapping: $$("#og-table tbody tr").map((tr) => ({
        kind: $(".og-kind", tr).value,
        region: $(".og-region", tr).value,
        name: $(".og-name", tr).value.trim(),
      })).filter((m) => m.name),
    });

    $("#og-add")?.addEventListener("click", () => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><select class="og-kind">${kinds.map((k) => `<option value="${esc(k.value)}">${esc(k.name)}</option>`).join("")}</select></td>
        <td><select class="og-region">${regions.map((r) => `<option value="${esc(r.value)}">${esc(r.name)}</option>`).join("")}</select></td>
        <td><input type="text" class="og-name" placeholder="目录名" /></td>
        <td><button class="btn ghost og-del" style="padding:4px 8px">✕</button></td>`;
      $("#og-table tbody").appendChild(tr);
      bindDel();
    });
    const bindDel = () => $$("#og-table .og-del").forEach((b) => b.addEventListener("click", () => {
      b.closest("tr").remove();
    }));
    bindDel();

    $("#og-save")?.addEventListener("click", async () => {
      try {
        await api("PUT", "/organize", { organize: collect() });
        toast("已保存自动分类设置", "ok");
      } catch (e) { errToast(e); }
    });

    const runTest = async () => {
      const t = $("#og-test").value.trim();
      if (!t) return;
      $("#og-test-out").textContent = "试算中…";
      try {
        const r = await api("POST", "/organize/preview", { title: t, structure: collect().structure, mapping: collect().mapping });
        const conf = Math.round((r.confidence || 0) * 100);
        const badge = conf >= 70 ? "ok" : conf >= 40 ? "warn" : "dim";
        $("#og-test-out").innerHTML = `
          判定：<b>${esc(r.category)}</b>
          <span class="badge ${badge}">${esc(r.kind_name)} / ${esc(r.region_name)} · 置信度 ${conf}%</span>
          <br><span class="muted">依据：${esc((r.signals || []).slice(0, 4).join("、") || "无明确信号，走兜底规则")}</span>`;
      } catch (e) { $("#og-test-out").textContent = "试算失败：" + e.message; }
    };
    $("#og-test-btn")?.addEventListener("click", runTest);
    $("#og-test")?.addEventListener("keydown", (e) => { if (e.key === "Enter") runTest(); });
  } else if (state.tab === "runtime") {
    body.innerHTML = `
      <label>监听来源</label>
      <select id="rt-type">
        <option value="web" ${S.sources.type === "web" ? "selected" : ""}>网页抓取（公开频道，无需登录，推荐）</option>
        <option value="userbot" ${S.sources.type === "userbot" ? "selected" : ""}>账号实时监听（需 Telegram api_id/hash）</option>
      </select>
      <label>轮询间隔（秒，最小 30）</label>
      <input type="number" id="rt-poll" min="30" value="${esc(S.sources.poll_interval)}" />
      <label>单条失败重试次数</label>
      <input type="number" id="rt-retry" min="1" value="${esc(S.max_retries)}" />
      <label><input type="checkbox" id="rt-hist" ${S.scan_history ? "checked" : ""} style="width:auto"> 启动时补抓历史消息</label>
      <label>历史扫描页数</label>
      <input type="number" id="rt-pages" min="1" value="${esc(S.history_pages)}" />
      <hr style="border:none;border-top:1px solid var(--bd);margin:14px 0" />
      <label style="font-weight:600">自动发现频道</label>
      <label><input type="checkbox" id="rt-disc" ${(S.discovery && S.discovery.enabled) ? "checked" : ""} style="width:auto"> 启用自动发现（从种子源自动扩充频道库）</label>
      <p class="hint">⚠️ 自动发现会从聚合页里扒频道名，先扫 1 页验证磁力（verify_threshold >= 1），有磁力才入库。若开启，系统的「零产出自动剔除」还会清掉长期不发链接的噪音频道。</p>
      <button class="btn" id="rt-prune" style="margin-top:8px">🧹 清理无效频道（移除长期 0 链接的噪音频道）</button>
      <button class="btn primary" id="rt-reset" style="margin-top:8px; margin-left:8px">⭐ 恢复精选频道(28)</button>
      <div id="rt-prune-out" class="hint" style="margin-top:8px"></div>
      <label style="font-weight:600">转存去重</label>
      <label><input type="checkbox" id="dd-cloud" ${(S.dedup && S.dedup.cloud_check_new) ? "checked" : ""} style="width:auto"> 云端复查（强烈建议开启）</label>
      <p class="hint">开启后，新出现的链接也会去光鸭盘里按片名复查，防止「同一片子不同磁力」被重复转存七八上十次。关闭则只做本地磁力去重。</p>
      <label><input type="checkbox" id="dd-upg" ${(S.dedup && S.dedup.upgrade) ? "checked" : ""} style="width:auto"> 版本升级（洗版）</label>
      <p class="hint">开启后，盘里已有同名同集文件、但新链接质量更优（更高分辨率 / REMUX / Atmos 等）时，自动删除旧版本并转存新版本；质量相同或更差则照常跳过。<b>注意：删除为不可逆操作</b>，默认关闭，确认需要再开。</p>
      <label>云端目录列表缓存（秒，最小 30）</label>
      <input type="number" id="dd-ttl" min="30" value="${esc((S.dedup && S.dedup.cache_ttl) || 300)}" />
      <button class="btn primary" id="rt-save" style="margin-top:12px">保存运行参数</button>
      <p class="hint">改完运行参数后，需重启监听（概览页停止再启动）才生效。</p>`;
    $("#rt-save").addEventListener("click", async () => {
      S.sources.type = $("#rt-type").value;
      S.sources.poll_interval = Math.max(30, Number($("#rt-poll").value) || 120);
      S.max_retries = Math.max(1, Number($("#rt-retry").value) || 3);
      S.scan_history = $("#rt-hist").checked;
      S.history_pages = Math.max(1, Number($("#rt-pages").value) || 3);
      S.discovery = S.discovery || {};
      S.discovery.enabled = $("#rt-disc").checked;
      S.discovery.verify_threshold = Math.max(0, Number(S.discovery.verify_threshold) || 1);
      S.dedup = S.dedup || {};
      S.dedup.cloud_check_new = $("#dd-cloud").checked;
      S.dedup.upgrade = $("#dd-upg").checked;
      S.dedup.cache_ttl = Math.max(30, Number($("#dd-ttl").value) || 300);
      try { await api("PUT", "/settings", S); toast("已保存运行参数", "ok"); }
      catch (e) { errToast(e); }
    });

    $("#rt-prune")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      $("#rt-prune-out").textContent = "清理中…（逐个频道检查链接，请稍候）";
      try {
        const r = await api("POST", "/channels/prune");
        const n = (r.removed || []).length;
        if (n === 0) {
          $("#rt-prune-out").innerHTML = `✅ 无需清理：当前 ${r.total} 个频道都有链接产出。`;
        } else {
          $("#rt-prune-out").innerHTML =
            `✅ 已移除 ${n} 个无效频道，剩余 ${r.kept} 个。<br>被移除：${esc((r.removed || []).join("、"))}<br><span class="muted">改完需「停止监听 → 启动监听」让新列表生效。</span>`;
          // 同步前端状态，避免再次保存时把被删频道写回去
          S.sources.channels = (S.sources.channels || []).filter((c) => !(r.removed || []).includes(c));
        }
      } catch (e) { $("#rt-prune-out").textContent = "清理失败：" + e.message; }
      finally { btn.disabled = false; }
    });

    $("#rt-reset")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      $("#rt-prune-out").textContent = "恢复中…";
      try {
        const r = await api("POST", "/channels/reset");
        $("#rt-prune-out").innerHTML =
          `✅ 已恢复为 ${r.total} 个精选频道：<br>${esc((r.channels || []).join("、"))}` +
          `<br><span class="muted">改完需「停止监听 → 启动监听」让新列表生效。</span>`;
        S.sources.channels = r.channels || [];  // 同步前端，避免再次保存时把噪音频道写回
      } catch (e) { $("#rt-prune-out").textContent = "恢复失败：" + e.message; }
      finally { btn.disabled = false; }
    });
  } else if (state.tab === "bot") {
    const B = S.bot || {};
    body.innerHTML = `
      <p class="hint">开启后，用手机上的 Telegram 就能直接控制程序：发个磁力链接立刻转存，
      <code>/status</code> 查进度、<code>/pause</code> 暂停轮询、<code>/add 频道名</code> 加频道。
      转存结果也会推送给你。</p>
      <div class="card">
        <h3>怎么申请</h3>
        <p>1. 在 Telegram 里找 <code>@BotFather</code>，发 <code>/newbot</code>，按提示拿到 token（形如 <code>123456:ABC-DEF…</code>）。</p>
        <p>2. 找 <code>@userinfobot</code> 发任意消息，拿到你自己的数字 ID，填进下面的管理员列表。</p>
        <p>3. 保存后<b>重启监听</b>（概览页「停止监听 → 启动监听」）才会生效。</p>
      </div>
      <label style="margin-top:16px"><input type="checkbox" id="bot-enabled" ${B.enabled ? "checked" : ""} style="width:auto"> 启用 TG 机器人</label>
      <label>Bot Token（@BotFather 处获取）</label>
      <input type="text" id="bot-token" placeholder="123456:ABC-DEF..." value="${esc(B.token || "")}" />
      <label>管理员数字 ID（多个用英文逗号分隔）</label>
      <input type="text" id="bot-admins" placeholder="123456789, 987654321" value="${esc((B.admin_ids || []).join(", "))}" />
      <p class="hint">留空则谁的命令都不执行（机器人只当摆设）。填了自己，就只有你能用。</p>
      <label><input type="checkbox" id="bot-notify" ${B.notify !== false ? "checked" : ""} style="width:auto"> 转存结果推送给我</label>
      <label><input type="checkbox" id="bot-anyone" ${B.allow_anyone ? "checked" : ""} style="width:auto"> 允许所有人使用（不建议）</label>
      <p class="hint">勾上等于任何人都能往你的光鸭盘里塞东西，一般别开。</p>
      <label style="margin-top:10px"><input type="checkbox" id="bot-search" ${B.search_enabled !== false ? "checked" : ""} style="width:auto"> 全网磁力搜索（/s 关键词，可直接点结果转存）</label>
      <label>代理（留空则沿用「频道配置」里的代理）</label>
      <input type="text" id="bot-proxy" placeholder="http://127.0.0.1:7890" value="${esc(B.proxy || "")}" />
      <button class="btn primary" id="bot-save" style="margin-top:12px">保存</button>
      <div id="bot-out" class="hint" style="margin-top:8px"></div>`;
    $("#bot-save").addEventListener("click", async () => {
      const rawIds = $("#bot-admins").value || "";
      const ids = rawIds.split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean)
        .map((x) => Number(x)).filter((n) => Number.isFinite(n) && n !== 0);
      S.bot = {
        enabled: $("#bot-enabled").checked,
        token: ($("#bot-token").value || "").trim(),
        admin_ids: ids,
        notify: $("#bot-notify").checked,
        allow_anyone: $("#bot-anyone").checked,
        search_enabled: $("#bot-search").checked,
        proxy: ($("#bot-proxy").value || "").trim(),
      };
      try {
        await api("PUT", "/settings", S);
        toast("已保存 TG 机器人设置", "ok");
        $("#bot-out").innerHTML = "✅ 已写入配置文件。<b>需重启监听</b>才会生效（概览页：停止监听 → 启动监听）。";
      } catch (e) { errToast(e); }
    });
  } else if (state.tab === "tmdb") {
    const TM = S.tmdb || {};
    body.innerHTML = `
      <p class="hint">TMDB（themoviedb.org）是全球最大的免费影视资料库。填上 API Key 后，
      系统认片会「更聪明」：<b>中文名 ↔ 英文原名</b>（如《星际穿越》= Interstellar）、
      <b>续集序号</b>（《流浪地球》与《流浪地球2》）这些纯靠文字认不出的关系都能识别，
      云端去重就不再误判。现在填好先存着，不会影响现有功能。</p>
      <div class="card">
        <h3>怎么申请</h3>
        <p>1. 打开 <a href="https://www.themoviedb.org/signup" target="_blank" rel="noopener">themoviedb.org</a> 注册账号（免费）。</p>
        <p>2. 登录后进 <b>设置 → API</b>，点「创建」，按提示填用途申请（个人使用、非商用即可，一般即时通过）。</p>
        <p>3. 拿到的 <b>API Key（v3 auth）</b> 填到下面，点「测试」确认能用。</p>
      </div>
      <label style="margin-top:16px">TMDB API Key（v3 auth）</label>
      <input type="text" id="tmdb-key" placeholder="形如 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d" value="${esc(TM.api_key || "")}" spellcheck="false" />
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="tmdb-save">保存</button>
        <button class="btn" id="tmdb-test">🧪 测试连接</button>
      </div>
      <div id="tmdb-out" class="hint" style="margin-top:8px"></div>`;
    $("#tmdb-save").addEventListener("click", async () => {
      S.tmdb = S.tmdb || {};
      S.tmdb.api_key = ($("#tmdb-key").value || "").trim();
      try { await api("PUT", "/settings", S); toast("已保存 TMDB API Key", "ok"); }
      catch (e) { errToast(e); }
    });
    $("#tmdb-test").addEventListener("click", async () => {
      const key = ($("#tmdb-key").value || "").trim();
      if (!key) { $("#tmdb-out").textContent = "请先填写 API Key 再测试。"; return; }
      $("#tmdb-out").textContent = "测试中…";
      try {
        const r = await api("POST", "/tmdb/test", { api_key: key });
        $("#tmdb-out").innerHTML = r.ok
          ? `✅ ${esc(r.message || "连接成功")}`
          : `❌ ${esc(r.message || "Key 不可用")}`;
      } catch (e) { $("#tmdb-out").textContent = "测试失败：" + e.message; }
    });
  } else if (state.tab === "notify") {
    body.innerHTML = `
      <label><input type="checkbox" id="nt-console" ${S.notify_console ? "checked" : ""} style="width:auto"> 控制台通知</label>
      <p class="hint">开启后，每条转存/跳过/失败都会在日志中打印通知。Web 界面内的「最近日志」始终可见。</p>
      <button class="btn primary" id="nt-save" style="margin-top:12px">保存</button>`;
    $("#nt-save").addEventListener("click", async () => {
      S.notify_console = $("#nt-console").checked;
      try { await api("PUT", "/settings", S); toast("已保存", "ok"); }
      catch (e) { errToast(e); }
    });
  } else if (state.tab === "backup") {
    let info = {};
    try { info = await api("GET", "/backup/info"); } catch (e) {}
    const files = (info.files || []).map((f) => `<code>${esc(f)}</code>`).join("、") || "<i>暂无</i>";
    const sizeKB = info.size ? (info.size / 1024).toFixed(1) + " KB" : "0 KB";
    body.innerHTML = `
      <p class="hint">备份文件保存在数据目录 <code>${esc(info.data_dir || "")}</code>。容器化部署时该目录对应挂载的存储卷，重装/重建都不丢数据。</p>
      <div class="card">
        <h3>导出备份</h3>
        <p>把以下数据打包成 .zip 下载到本地：</p>
        <p class="kv"><span>包含文件</span><span>${files}</span></p>
        <p class="kv"><span>体积</span><span>${sizeKB}</span></p>
        <div class="row">
          <a class="btn primary" href="/api/backup/download" download>⬇️ 导出备份 (.zip)</a>
        </div>
        <p class="hint">含 config.yaml（账号与频道配置）、去重数据库（已转存记录）、Telegram 会话（若启用 userbot）。还原后可完整恢复运行状态。</p>
      </div>
      <div class="card" style="margin-top:16px">
        <h3>导入还原</h3>
        <p>选择一个之前导出的备份 .zip，覆盖当前配置与数据。还原前系统会自动备份现有数据（.bak）。</p>
        <div class="row" style="margin-bottom:10px">
          <input type="file" id="bk-file" accept=".zip,application/zip" />
          <button class="btn" id="bk-restore">⬆️ 还原</button>
        </div>
        <p class="hint" id="bk-msg">⚠️ 还原会覆盖当前配置和去重数据库，且会重启监听（若正在运行）。请确认选对了备份文件。</p>
      </div>`;
    $("#bk-restore")?.addEventListener("click", async () => {
      const f = $("#bk-file").files && $("#bk-file").files[0];
      if (!f) { toast("请先选择备份文件", "err"); return; }
      if (!confirm("确认还原？当前数据会被覆盖（已自动备份为 .bak）。")) return;
      const fd = new FormData();
      fd.append("file", f);
      $("#bk-msg").textContent = "还原中…";
      try {
        const r = await fetch("/api/backup/restore", { method: "POST", body: fd });
        if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText); }
        toast("还原成功，正在刷新…", "ok");
        setTimeout(() => location.reload(), 900);
      } catch (e) { errToast(e); $("#bk-msg").textContent = "⚠️ " + e.message; }
    });
  } else if (state.tab === "about") {
    body.innerHTML = `
      <div class="kv"><span>程序</span><span>TG 频道资源 → 光鸭云盘 自动转存</span></div>
      <div class="kv"><span>版本</span><span>0.1.2</span></div>
      <div class="kv"><span>工作模式</span><span>公开频道网页抓取 → 光鸭离线下载</span></div>
      <div class="kv"><span>光鸭接口</span><span>逆向自 LitePan（PolyForm Noncommercial，仅个人非商用）</span></div>
      <h3 style="margin-top:18px">合规提示</h3>
      <p class="hint">本工具仅做<b>个人自用</b>的转存与整理。磁力/种子本身是中性技术，但频道内影视资源多涉及版权，请勿对外二次分发、分享或传播。请遵守所在地区法律法规与网盘服务条款。</p>
      <h3 style="margin-top:14px">快捷操作</h3>
      <div class="row">
        <button class="btn" id="ab-disc">运行一次频道发现</button>
      </div>
      <p class="hint">「运行一次频道发现」会立刻从种子源扫描新频道并自动加入配置（等价于 discover.py）。</p>`;
    $("#ab-disc").addEventListener("click", async () => {
      try {
        const r = await api("POST", "/discover/run");
        toast(r.added ? `发现并加入 ${r.added} 个新频道` : `未发现新频道（已扫描 ${r.found} 个候选）`, r.added ? "ok" : "");
        if (r.added) await refreshStatus();
      } catch (e) { errToast(e); }
    });
  }
}

/* 目录浏览器 */
function renderDirCrumb() {
  const el = $("#dir-crumb");
  el.innerHTML = state.dirStack.map((d, i) =>
    `<a href="#" class="tag" data-i="${i}" style="cursor:pointer">${esc(i === 0 ? "📁 根目录" : d.name)}</a>`).join(" <span class='muted'>/</span> ");
  $$("#dir-crumb .tag").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    state.dirStack = state.dirStack.slice(0, Number(a.dataset.i) + 1);
    renderDirCrumb(); renderDirList();
  }));
}
async function renderDirList() {
  const cur = state.dirStack[state.dirStack.length - 1];
  const el = $("#dir-list");
  el.innerHTML = `<div class="empty">加载目录…</div>`;
  try {
    const r = await api("GET", "/guangya/folders?parent_id=" + encodeURIComponent(cur.file_id));
    const fs = r.folders || [];
    if (!fs.length) { el.innerHTML = `<div class="empty">该目录下没有子文件夹</div>`; return; }
    el.innerHTML = fs.map((f) => `
      <div class="card" style="padding:12px;display:flex;justify-content:space-between;align-items:center">
        <span>📁 ${esc(f.name)}</span>
        <div class="row tight">
          <button class="btn ghost" data-enter="${esc(f.file_id)}" data-name="${esc(f.name)}">进入</button>
          <button class="btn primary" data-pick="${esc(f.file_id)}" data-name="${esc(f.name)}">选择</button>
        </div>
      </div>`).join("");
    $$("[data-enter]", el).forEach((b) => b.addEventListener("click", () => {
      state.dirStack.push({ file_id: b.dataset.enter, name: b.dataset.name });
      renderDirCrumb(); renderDirList();
    }));
    $$("[data-pick]", el).forEach((b) => b.addEventListener("click", () => {
      $("#out-parent").value = b.dataset.pick;
      toast("已选择目录：" + b.dataset.name, "ok");
    }));
  } catch (e) { el.innerHTML = `<div class="empty">读取目录失败：${esc(e.message)}</div>`; }
}

/* ============ 登录弹窗（按 LitePan：默认填入 Token，扫码折叠） ============ */
function openLoginModal() {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <h3>光鸭登录</h3>
        <p class="hint">推荐：把 LitePan（或光鸭 App）拿到的 <b>访问令牌</b>与<b>刷新令牌</b>粘到下面，点保存即可登录。令牌会自动续期，过期前无需重新填。</p>
        <div class="form-row">
          <label>访问令牌 *</label>
          <textarea id="tk-access" placeholder="access_token" rows="3"></textarea>
        </div>
        <div class="form-row">
          <label>刷新令牌 *</label>
          <textarea id="tk-refresh" placeholder="refresh_token" rows="3"></textarea>
        </div>
        <div class="row" style="justify-content:flex-end;margin-top:8px">
          <button class="btn ghost" id="qr-close">关闭</button>
          <button class="btn primary" id="tk-save">保存并登录</button>
        </div>
        <p id="tk-msg" class="err-line"></p>

        <details class="manual-login">
          <summary>用二维码扫码登录（备用）</summary>
          <div class="qr-box"><img id="qr-img" alt="二维码" /></div>
          <p id="qr-err" class="err-line"></p>
          <p class="hint">用「光鸭云盘」App 扫码并确认授权。二维码 2 分钟内有效。</p>
          <p class="row"><a id="qr-link" class="pill" target="_blank" rel="noopener">🔗 打不开二维码？点此打开链接</a></p>
          <span id="qr-status" class="badge dim"><span class="dot"></span> 等待扫码…</span>
        </details>
      </div>
    </div>`;
  $("#qr-close").addEventListener("click", () => closeModal(root));
  const tkMsg = $("#tk-msg");
  $("#tk-save").addEventListener("click", async () => {
    const access = $("#tk-access").value.trim();
    const refresh = $("#tk-refresh").value.trim();
    if (!access && !refresh) { tkMsg.textContent = "⚠️ 请至少填写刷新令牌（访问令牌只有 2 小时有效期，靠刷新令牌自动续期，过期的访问令牌也能自动换发）"; return; }
    tkMsg.textContent = "校验中…";
    try {
      const r = await api("POST", "/guangya/login/manual", { access_token: access, refresh_token: refresh });
      tkMsg.textContent = "✅ 登录成功";
      toast("光鸭登录成功", "ok");
      await refreshStatus();
      setTimeout(() => closeModal(root), 600);
    } catch (e) { tkMsg.textContent = "⚠️ " + (e.message || e); }
  });
  // 二维码折叠里默认不主动发起，留给需要的人点开
  const details = root.querySelector("details.manual-login");
  if (details) details.addEventListener("toggle", () => {
    if (details.open && !$("#qr-img").src) {
      api("POST", "/guangya/login/start").then((info) => {
        if (info.qr_data_url) {
          $("#qr-img").src = info.qr_data_url;
        } else {
          const im = $("#qr-img"); if (im) im.style.display = "none";
          const el = $("#qr-err"); if (el) el.textContent = "二维码图片生成失败，请点下方链接在浏览器里完成授权";
        }
        $("#qr-link").href = info.qr_url;
        let live = true;
        const iv = setInterval(async () => {
          if (!live) return;
          try {
            const r = await api("GET", "/guangya/login/poll?device_code=" + encodeURIComponent(info.device_code));
            const st = $("#qr-status");
            if (r.status === "success") { st.className="badge ok"; st.innerHTML=`<span class="dot"></span> 登录成功`; clearInterval(iv); live=false; toast("光鸭登录成功","ok"); await refreshStatus(); setTimeout(()=>closeModal(root),800); }
            else if (r.status === "denied") { st.className="badge err"; st.innerHTML=`<span class="dot"></span> 已取消`; clearInterval(iv); }
            else if (r.status === "expired") { st.className="badge err"; st.innerHTML=`<span class="dot"></span> 二维码过期`; clearInterval(iv); }
          } catch(e){}
        }, Math.max(2000, info.interval * 1000));
      }).catch((e) => { const el=$("#qr-err"); if(el) el.textContent = "⚠️ 登录失败：" + (e.message||e); });
    }
  });
}
function closeModal(root) { root.innerHTML = ""; }

/* ============ Telegram Userbot 登录弹窗（手机 + 验证码，可选 2FA） ============ */
function openUserbotLoginModal() {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <h3>Telegram 账号登录</h3>
        <div id="ub-step-phone">
          <p class="hint">输入你的手机号（含国家区号，如 +8613800138000），Telegram 会发验证码。</p>
          <div class="form-row"><label>手机号</label><input type="text" id="ub-phone" placeholder="+86..." /></div>
          <div class="row" style="justify-content:flex-end;margin-top:8px">
            <button class="btn ghost" id="ub-close">关闭</button>
            <button class="btn primary" id="ub-send">发送验证码</button>
          </div>
          <p id="ub-msg" class="err-line"></p>
        </div>
        <div id="ub-step-code" style="display:none">
          <p class="hint">已向 <b id="ub-phone-show"></b> 发送验证码，请查收 TG 短信 / 其他客户端，填入下方。</p>
          <div class="form-row"><label>验证码</label><input type="text" id="ub-code" placeholder="12345" /></div>
          <div class="row" style="justify-content:flex-end;margin-top:8px">
            <button class="btn primary" id="ub-submit-code">登录</button>
          </div>
          <p id="ub-code-msg" class="err-line"></p>
        </div>
        <div id="ub-step-pwd" style="display:none">
          <p class="hint">该账号开启了两步验证，请输入登录密码。</p>
          <div class="form-row"><label>登录密码</label><input type="password" id="ub-pwd" /></div>
          <div class="row" style="justify-content:flex-end;margin-top:8px">
            <button class="btn primary" id="ub-submit-pwd">确认</button>
          </div>
          <p id="ub-pwd-msg" class="err-line"></p>
        </div>
      </div>
    </div>`;
  $("#ub-close").addEventListener("click", () => closeModal(root));
  const msg = $("#ub-msg");
  $("#ub-send").addEventListener("click", async () => {
    const phone = $("#ub-phone").value.trim();
    if (!phone) { msg.textContent = "⚠️ 请填写手机号"; return; }
    msg.textContent = "发送中…";
    try {
      await api("POST", "/userbot/login/start", { phone });
      $("#ub-step-phone").style.display = "none";
      $("#ub-step-code").style.display = "block";
      $("#ub-phone-show").textContent = phone;
      msg.textContent = "";
    } catch (e) { msg.textContent = "⚠️ " + (e.message || e); }
  });
  $("#ub-submit-code").addEventListener("click", async () => {
    const code = $("#ub-code").value.trim();
    const cm = $("#ub-code-msg");
    if (!code) { cm.textContent = "⚠️ 请填写验证码"; return; }
    cm.textContent = "登录中…";
    try {
      const r = await api("POST", "/userbot/login/code", { code });
      if (r.status === "password_needed") {
        $("#ub-step-code").style.display = "none";
        $("#ub-step-pwd").style.display = "block";
        return;
      }
      toast("Telegram 登录成功", "ok");
      state.userbot_logged_in = true;
      setTimeout(() => closeModal(root), 500);
      views.settings();
    } catch (e) { cm.textContent = "⚠️ " + (e.message || e); }
  });
  $("#ub-submit-pwd").addEventListener("click", async () => {
    const pwd = $("#ub-pwd").value;
    const pm = $("#ub-pwd-msg");
    pm.textContent = "验证中…";
    try {
      await api("POST", "/userbot/login/password", { password: pwd });
      toast("Telegram 登录成功", "ok");
      state.userbot_logged_in = true;
      setTimeout(() => closeModal(root), 500);
      views.settings();
    } catch (e) { pm.textContent = "⚠️ " + (e.message || e); }
  });
}

/* ============ 启动 ============ */
window.addEventListener("hashchange", route);
refreshStatus().then(route);
