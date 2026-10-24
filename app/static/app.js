const $ = (id) => document.getElementById(id);
let nextAt = null, draft = null, saved = null, catalog = null;
let partChoices = [], selectedParts = new Set();

function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 2800);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function fmtClock(ts) {
  if (!ts) return "—";
  return new Date(ts*1000).toLocaleTimeString("zh-CN", {hour12:false});
}

function countdown() {
  if (!nextAt) { $("countdown").textContent = "—"; return; }
  const left = Math.max(0, Math.round(nextAt - Date.now()/1000));
  const m = Math.floor(left/60), s = left%60;
  $("countdown").textContent = left>0 ? `下轮 ${m}:${String(s).padStart(2,"0")}` : "即将开始";
}

async function poll() {
  try {
    const r = await fetch("/api/status", {cache:"no-store"});
    if (!r.ok) throw new Error("HTTP "+r.status);
    renderStatus(await r.json());
  } catch (e) {
    $("statusBadge").className = "badge bad";
    $("statusBadge").textContent = "连接中断";
  }
  countdown();
}

function renderStatus(d) {
  $("scopeLine").textContent =
    `${d.region}｜${d.scope}｜每 ${d.interval} 秒（±${Math.round(d.jitter*100)}%）`;
  nextAt = d.next_check_at || null;
  countdown();

  const badge = $("statusBadge");
  if (d.consecutive_blocks > 0) {
    badge.className = "badge " + (d.consecutive_blocks >= d.degraded_alert_after ? "bad":"warn");
    badge.textContent = `已拦截 ${d.consecutive_blocks} 轮`;
  } else if (d.running) {
    badge.className = "badge ok"; badge.textContent = "运行中";
  } else {
    badge.className = "badge"; badge.textContent = "已停止";
  }

  const notes = [];
  if (d.consecutive_blocks > 0) {
    notes.push(`<div class="note">已被 Apple 拦截 ${d.consecutive_blocks} 轮，当前结论不可信。
      程序会按退避阶梯自动重试，<b>不要重启</b>——反复重启只会延长封禁。</div>`);
  }
  if (d.error) notes.push(`<div class="note">最近错误：${esc(d.error)}</div>`);
  if (d.quiet_active) notes.push(`<div class="note">当前处于静默时段，提醒不会推送。</div>`);
  $("notes").innerHTML = notes.join("");

  const hits = d.hits || [];
  const v = $("verdict"), big = $("verdictBig"), meta = $("verdictMeta");
  if (!d.last_cycle_at) {
    v.className = "verdict none"; big.textContent = "等待第一轮结果…"; meta.textContent = "";
  } else if (d.consecutive_blocks > 0) {
    v.className = "verdict none"; big.textContent = "本轮未能取得数据";
    meta.textContent = `${fmtClock(d.last_cycle_at)} · 被拦截不等于无货，请勿据此判断`;
  } else if (hits.length) {
    v.className = "verdict hit";
    big.textContent = `${hits.length} 个型号有货`;
    meta.textContent = `${fmtClock(d.last_cycle_at)} · 覆盖 ${d.store_count} 家门店`;
  } else {
    v.className = "verdict none"; big.textContent = "暂无补货";
    meta.textContent =
      `${fmtClock(d.last_cycle_at)} · 覆盖 ${d.store_count} 家门店、${d.part_count} 个型号`;
  }

  $("hits").innerHTML = hits.length ? `<div class="hits">` + hits.map(h => `
      <div class="hitrow">
        <span class="store">${esc(h.store_name)}</span>
        <span class="name">${esc(h.title)}</span>
        <span class="quote">${esc(h.quote||"")}</span>
      </div>`).join("") + `</div>` : "";

  const rows = d.rows || [];
  if (rows.length) {
    $("detailWrap").style.display = "";
    $("detailSummary").textContent =
      `查看本轮明细（${d.store_count} 家门店 × ${d.part_count} 个型号）`;
    const byStore = new Map();
    for (const r of rows) {
      if (!byStore.has(r.store_number)) {
        byStore.set(r.store_number, {name:r.store_name, city:r.city, parts:[]});
      }
      byStore.get(r.store_number).parts.push(r);
    }
    $("detail").innerHTML = [...byStore.values()].map(g => {
      const n = g.parts.filter(p => p.available === true).length;
      const chips = g.parts.map(p => {
        const mark = p.available === true ? "✔" : (p.available === null ? "?" : "·");
        const cls = p.available === true ? "badge ok" : "badge";
        return `<span class="${cls}" style="margin:2px 4px 2px 0">${mark} ${esc(p.title)}</span>`;
      }).join("");
      return `<div style="margin-bottom:9px">
        <div style="font-weight:600;color:var(--text)">${esc(g.name)}
          <span class="muted" style="font-weight:400">${esc(g.city||"")} · ${n?n+" 个有货":"无货"}</span></div>
        <div>${chips}</div></div>`;
    }).join("");
  } else {
    $("detailWrap").style.display = "none";
  }

  renderHistory(d.history || []);
}

// 补货历史：只显示「有补货」的扫描，时间 + 几家店补了几种型号。
function renderHistory(history) {
  const panel = $("historyPanel");
  if (!history.length) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "";
  panel.querySelector("h2").textContent = `补货历史（最近 ${history.length} 次有补货的扫描）`;
  $("history").innerHTML = history.map(h => {
    const time = new Date(h.at * 1000).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    });
    return `<div class="hitrow">
      <span class="store mono">${esc(time)}</span>
      <span class="name"><b>${h.store_count}</b> 家门店补货 · 共 <b>${h.part_count}</b> 种型号</span>
      <span class="quote">${esc(h.summary)}</span>
    </div>`;
  }).join("");
}

// ---------- 监控日志 ----------
// 整个区块可折叠：10 秒一轮时日志涨得很快。折叠时只留最近 5 条，
// 展开时显示最近 20 条。偏好记在 localStorage，刷新后保持。
const LOG_OPEN_KEY = "appleMonitor.logOpen";
// 折叠时显示条数 / 展开时显示条数
const LOG_COLLAPSED_N = 5;
const LOG_EXPANDED_N = 20;
let logOpen = (() => {
  try {
    const v = localStorage.getItem(LOG_OPEN_KEY);
    return v === null ? false : v === "1";   // 默认折叠
  } catch (e) { return false; }
})();

function applyLogOpen() {
  $("logTools").style.display = logOpen ? "" : "none";
  $("logCaret").textContent = logOpen ? "▴" : "▾";
  $("logToggleText").textContent = logOpen ? "收起" : "展开";
  $("logToggle").classList.toggle("on", logOpen);
  // 条数由渲染决定：折叠 5 条、展开 20 条
  renderLogs();
}

function toggleLog() {
  logOpen = !logOpen;
  try { localStorage.setItem(LOG_OPEN_KEY, logOpen ? "1" : "0"); } catch (e) {}
  applyLogOpen();
}

const ERROR_LABELS = {
  blocked: "Apple 拦截（541/403）",
  rate_limited: "被限流（429）",
  node_error: "节点不通",
  other: "其他错误",
};
let logFilter = "all";
let logData = [];

async function loadLogs() {
  try {
    const r = await fetch("/api/logs?limit=100", {cache:"no-store"});
    if (!r.ok) return;
    const d = await r.json();
    logData = d.rounds || [];
    renderLogs();
  } catch (e) { /* 静默失败，下轮再试 */ }
}

function renderLogs() {
  const box = $("logs");
  if (!logData.length) {
    box.innerHTML = '<div class="empty">等待第一轮结果…</div>';
    return;
  }
  const filtered = logData.filter(r => {
    if (logFilter === "hit") return r.hit_count > 0;
    if (logFilter === "fail") return !r.ok;
    return true;
  });
  if (!filtered.length) {
    box.innerHTML = `<div class="empty">没有符合条件的记录</div>`;
    return;
  }
  // 折叠 5 条、展开 20 条。放在筛选之后截断，这样「失败」筛选在折叠态
  // 也能看到最近 5 条失败，而不是被一堆无货行挤掉。
  const limit = logOpen ? LOG_EXPANDED_N : LOG_COLLAPSED_N;
  const rows = filtered.slice(0, limit);
  // 折叠时标题旁显示摘要：总轮次、补货次数、失败次数
  const hitRounds = logData.filter(r => r.hit_count > 0).length;
  const failRounds = logData.filter(r => !r.ok).length;
  const bits = [`显示最近 ${rows.length} 条 / 共 ${logData.length} 轮`];
  if (hitRounds) bits.push(`${hitRounds} 轮有补货`);
  if (failRounds) bits.push(`${failRounds} 轮失败`);
  $("logCount").textContent = `· ${bits.join("，")}`;

  // 默认折叠成单行摘要：10 秒一轮时，展开版几分钟就刷满整页。
  // 只有「有补货」和「失败」的行默认展开——这两种才需要立刻看到。
  box.innerHTML = rows.map((r, i) => {
    const time = new Date(r.at * 1000).toLocaleTimeString("zh-CN", {hour12: false});
    let status, cls;
    if (!r.ok) {
      status = ERROR_LABELS[r.error_kind] || "失败";
      cls = r.error_kind === "blocked" || r.error_kind === "rate_limited" ? "bad" : "warn";
    } else if (r.hit_count > 0) {
      status = `${r.hit_count} 个补货`;
      cls = "ok";
    } else {
      status = "无补货";
      cls = "";
    }
    const node = r.node && r.node !== "（未轮转）" ? r.node : "—";
    const detail = r.error_detail
      ? `<div class="small muted" style="margin-top:4px">${esc(r.error_detail)}</div>` : "";
    const hits = (r.hits || []).map(h =>
      `<div class="hitrow" style="padding:2px 0">
         <span class="store">${esc(h.store)}</span>
         <span class="name">${esc(h.title)}</span>
         <span class="quote">${esc(h.quote || "")}</span>
       </div>`).join("");
    const hasBody = hits || detail;
    return `<div class="logrow" data-i="${i}">
      <div class="row loghead" style="gap:9px">
        <span class="caret">${hasBody ? "▸" : ""}</span>
        <span class="mono small muted" style="min-width:56px">${time}</span>
        <span class="badge ${cls}">${esc(status)}</span>
        <span class="grow small" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(node)}</span>
        <span class="small muted mono" style="white-space:nowrap">${r.store_count} 店 / ${r.part_count} 型号</span>
      </div>
      ${hasBody ? `<div class="logbody">${hits}${detail}</div>` : ""}
    </div>`;
  }).join("");

  // 折叠状态：有补货/失败的默认展开，其余折叠。用户点击可切换。
  box.querySelectorAll(".logrow").forEach(el => {
    const r = rows[Number(el.dataset.i)];
    const body = el.querySelector(".logbody");
    if (!body) return;
    const caret = el.querySelector(".caret");
    let open = r.hit_count > 0 || !r.ok;
    const apply = () => {
      body.style.display = open ? "" : "none";
      caret.textContent = open ? "▾" : "▸";
    };
    apply();
    el.querySelector(".loghead").onclick = () => { open = !open; apply(); };
  });
}

$("logFilter").querySelectorAll("button").forEach(b => {
  b.onclick = () => {
    logFilter = b.dataset.f;
    $("logFilter").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    renderLogs();
  };
});
$("logRefresh").onclick = loadLogs;
$("logToggle").onclick = toggleLog;
// 标题也可以点：折叠态下整块只有标题和 5 行，点哪儿都该能展开
$("logCount").parentElement.style.cursor = "pointer";
$("logCount").parentElement.onclick = toggleLog;
applyLogOpen();

// ---------- 设置 ----------
async function loadSettings() {
  const r = await fetch("/api/settings", {cache:"no-store"});
  if (!r.ok) { toast("读取设置失败", true); return; }
  const d = await r.json();
  catalog = d.stores;
  // families 在响应顶层，不在 stores 里；这里挂上去，供下拉与筛选共用。
  catalog.families = d.families || [];
  saved = d.settings;
  draft = JSON.parse(JSON.stringify(saved));
  partChoices = d.current_parts || [];
  selectedParts = new Set(draft.parts || []);
  renderSettings();
}

function renderSettings() {
  const chosen = new Set(draft.provinces || []);
  $("provinceChips").innerHTML = (catalog.provinces || []).map(p => {
    const on = chosen.has(p) ? " on" : "";
    const n = catalog.counts[p] || 0;
    return `<button class="chip${on}" data-province="${esc(p)}">${esc(p)}` +
           `<span class="muted" style="margin-left:5px">${n}</span></button>`;
  }).join("");
  $("provinceChips").querySelectorAll("button").forEach(btn => {
    btn.onclick = () => {
      const p = btn.dataset.province;
      const i = draft.provinces.indexOf(p);
      if (i >= 0) draft.provinces.splice(i,1); else draft.provinces.push(p);
      renderSettings();
    };
  });
  $("provinceHint").textContent = draft.provinces.length
    ? `已选 ${draft.provinces.length} 个省份，每轮 ${draft.provinces.length} 次请求（门店列表来源：${catalog.source}）`
    : "按省份选择；每省一次请求即可覆盖该省及周边全部门店。";

  const famSel = $("familySelect");
  const families = catalog.families || [];
  famSel.innerHTML = families.map(f =>
    `<option value="${esc(f.slug)}">${esc(f.label)}</option>`).join("");
  if (draft.family && families.some(f => f.slug === draft.family)) famSel.value = draft.family;
  else if (families.length) draft.family = families[0].slug;

  renderFilters();
  $("intervalInput").value = draft.interval;
  const floor = intervalFloor();
  $("intervalInput").min = String(floor);
  $("intervalHint").textContent = floor < 120
    ? `轮转模式最低 ${floor} 秒。${draft.provinces?.length || 1} 个省份 × 多节点轮转，`
      + `每个节点平均 ${Math.round((draft.interval + 3) * 33)} 秒才被用一次。`
    : `单出口最低 ${floor} 秒。配置 Clash 轮换后可降到 5 秒。`;
  $("jitterInput").value = Math.round(draft.jitter*100);
  $("backoffInput").value = (draft.backoff||[]).map(s => Math.round(s/60)).join(",");
  $("degradedInput").value = draft.degraded_alert_after;
  $("repeatToggle").checked = !!draft.repeat_when_in_stock;
  $("repeatInput").value = draft.repeat_interval;
  $("quietToggle").checked = !!draft.quiet_hours.enabled;
  $("quietStart").value = draft.quiet_hours.start;
  $("quietEnd").value = draft.quiet_hours.end;
  applyQuietState();
  $("quietMode").querySelectorAll("button").forEach(b => {
    b.classList.toggle("on", b.dataset.mode === draft.quiet_hours.mode);
  });
  updateBackoffHint();
  renderParts();
  renderChannels();
  updateDirty();
}

function applyQuietState() {
  const on = draft.quiet_hours.enabled;
  $("quietRow").style.opacity = on ? "1" : ".45";
  $("quietRow").style.pointerEvents = on ? "auto" : "none";
}

function updateBackoffHint() {
  const list = (draft.backoff||[]).map(s => Math.round(s/60));
  $("backoffHint").textContent = list.length
    ? `第 1 次被拦等 ${list[0]} 分钟，之后依次 ${list.slice(1).join(" → ")||"—"} 分钟`
    : "";
}

function renderFilters() {
  const fam = (catalog.families||[]).find(f => f.slug === draft.family) || {colors:[],capacities:[]};
  const colorSel = $("colorSelect"), capSel = $("capacitySelect");
  colorSel.innerHTML = `<option value="">全部颜色</option>` +
    (fam.colors||[]).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  capSel.innerHTML = `<option value="">全部容量</option>` +
    (fam.capacities||[]).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  colorSel.value = draft.color || "";
  capSel.value = draft.capacity || "";
}

function visibleParts() {
  return partChoices.filter(p =>
    (!draft.color || p.color === draft.color) &&
    (!draft.capacity || p.capacity === draft.capacity));
}

function renderParts() {
  const list = $("partList");
  if (!partChoices.length) {
    list.innerHTML = `<div class="empty">点击「读取该机型型号」从官网获取零件号</div>`;
    return;
  }
  const filtered = visibleParts();
  if (!filtered.length) {
    list.innerHTML = `<div class="empty">当前筛选下没有型号</div>`;
    return;
  }
  list.innerHTML = filtered.map(p => {
    const on = selectedParts.has(p.part) ? "checked" : "";
    return `<label class="partitem">
      <input type="checkbox" data-part="${esc(p.part)}" ${on}>
      <span class="grow">${esc(p.name)}</span>
      <span class="pn">${esc(p.part)}</span></label>`;
  }).join("");
  list.querySelectorAll("input[data-part]").forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) selectedParts.add(cb.dataset.part);
      else selectedParts.delete(cb.dataset.part);
      updateDirty();
    };
  });
}

// 间隔下限：轮转模式 5 秒，单出口 120 秒。与后端 config.py 保持一致。
// 5 秒是下限而非建议值——单轮查询本身要 2-6 秒，再短就会请求重叠。
function intervalFloor() {
  const rotating = !!saved && !!saved.clash_api && saved.rotate_routes !== false;
  return rotating ? 5 : 120;
}

function currentPayload() {
  return {
    interval: parseInt($("intervalInput").value,10),
    jitter: parseInt($("jitterInput").value,10)/100,
    provinces: [...draft.provinces],
    family: draft.family,
    color: draft.color || "",
    capacity: draft.capacity || "",
    parts: partChoices.filter(p => selectedParts.has(p.part)).map(p => ({part:p.part, name:p.name})),
    backoff: $("backoffInput").value.split(",")
      .map(s => parseInt(s.trim(),10)).filter(n => !isNaN(n) && n > 0).map(m => m*60),
    degraded_alert_after: parseInt($("degradedInput").value,10),
    repeat_when_in_stock: $("repeatToggle").checked,
    repeat_interval: parseInt($("repeatInput").value,10),
    quiet_hours: {
      enabled: $("quietToggle").checked,
      start: $("quietStart").value || "23:00",
      end: $("quietEnd").value || "08:00",
      mode: draft.quiet_hours.mode,
    },
    // notify 必须一起提交：少了这一项，界面上配的渠道永远不会写进配置。
    notify: (draft.notify || []).map(ch => {
      const out = {type: ch.type};
      for (const [k, v] of Object.entries(ch)) {
        if (k === "type" || k.endsWith("_masked")) continue;
        if (v === null || v === undefined || v === "") continue;
        out[k] = v;
      }
      // 打码标记要带给后端，它据此还原真实密钥
      for (const k of ["url","token","password"]) {
        if (ch[`${k}_masked`]) out[`${k}_masked`] = true;
      }
      return out;
    }),
  };
}

function updateDirty() {
  if (!saved) return;
  const payload = currentPayload();
  const same =
    payload.interval === saved.interval &&
    Math.abs(payload.jitter - saved.jitter) < 1e-6 &&
    JSON.stringify([...payload.provinces].sort()) === JSON.stringify([...saved.provinces].sort()) &&
    payload.family === saved.family &&
    (payload.color||"") === (saved.color||"") &&
    (payload.capacity||"") === (saved.capacity||"") &&
    JSON.stringify(payload.parts.map(p=>p.part).sort()) === JSON.stringify([...(saved.parts||[])].sort()) &&
    JSON.stringify(payload.backoff) === JSON.stringify(saved.backoff) &&
    payload.degraded_alert_after === saved.degraded_alert_after &&
    payload.repeat_when_in_stock === saved.repeat_when_in_stock &&
    payload.repeat_interval === saved.repeat_interval &&
    payload.quiet_hours.enabled === saved.quiet_hours.enabled &&
    payload.quiet_hours.start === saved.quiet_hours.start &&
    payload.quiet_hours.end === saved.quiet_hours.end &&
    payload.quiet_hours.mode === saved.quiet_hours.mode &&
    JSON.stringify(payload.notify) === JSON.stringify(normalizeNotify(saved.notify));
  $("saveBtn").disabled = same;
  $("resetBtn").disabled = same;
}

// 后端回填的 notify 带 * masked 标记，比较前要先对齐形状，否则「没改任何东西」
// 也会被判成有改动，保存按钮一直是亮的。
function normalizeNotify(list) {
  return (list || []).map(ch => {
    const out = {type: ch.type};
    for (const [k, v] of Object.entries(ch)) {
      if (k === "type" || k.endsWith("_masked")) continue;
      if (v === null || v === undefined || v === "") continue;
      out[k] = String(v);
    }
    for (const k of ["url","token","password"]) {
      if (ch[`${k}_masked`]) out[`${k}_masked`] = true;
    }
    return out;
  });
}

async function loadParts() {
  const slug = $("familySelect").value;
  if (!slug) return;
  $("partsStatus").innerHTML = `<span class="spin"></span> 读取中…`;
  $("loadPartsBtn").disabled = true;
  try {
    const r = await fetch(`/api/parts?family=${encodeURIComponent(slug)}`, {cache:"no-store"});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "读取失败");
    partChoices = d.parts || [];
    const fam = (catalog.families||[]).find(f => f.slug === slug);
    if (fam) {
      fam.colors = d.colors || [];
      fam.capacities = d.capacities || [];
    }
    renderFilters();
    renderParts();
    $("partsStatus").textContent = `已读取 ${partChoices.length} 个型号`;
  } catch (e) {
    $("partsStatus").textContent = "";
    toast("读取型号失败：" + e.message, true);
  } finally {
    $("loadPartsBtn").disabled = false;
  }
}

async function save() {
  const payload = currentPayload();
  const floor = intervalFloor();
  if (!(payload.interval >= floor)) {
    toast(`查询间隔不能低于 ${floor} 秒`, true);
    return;
  }
  if (!(payload.jitter >= 0 && payload.jitter <= 0.9)) { toast("抖动范围应在 0–90 之间", true); return; }
  if (!payload.backoff.length) { toast("退避阶梯不能为空", true); return; }
  for (let i=1;i<payload.backoff.length;i++) {
    if (payload.backoff[i] <= payload.backoff[i-1]) { toast("退避阶梯必须递增", true); return; }
  }
  if (!payload.provinces.length) { toast("请至少选择一个省份", true); return; }
  if (!payload.parts.length) { toast("请至少勾选一个型号", true); return; }

  $("saveBtn").disabled = true;
  try {
    const r = await fetch("/api/settings", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "保存失败");
    toast(d.message || "已保存");
    await loadSettings();
    $("restartNote").style.display = d.restart_required ? "" : "none";
  } catch (e) {
    toast("保存失败：" + e.message, true);
    $("saveBtn").disabled = false;
  }
}

$("familySelect").onchange = () => {
  draft.family = $("familySelect").value;
  partChoices = [];
  renderFilters(); renderParts(); updateDirty();
};
$("colorSelect").onchange = () => { draft.color = $("colorSelect").value; renderParts(); updateDirty(); };
$("capacitySelect").onchange = () => { draft.capacity = $("capacitySelect").value; renderParts(); updateDirty(); };
$("loadPartsBtn").onclick = loadParts;
$("saveBtn").onclick = save;
$("resetBtn").onclick = async () => { await loadSettings(); toast("已放弃修改"); };

["intervalInput","jitterInput","degradedInput","repeatInput","quietStart","quietEnd"]
  .forEach(id => { $(id).oninput = updateDirty; });
$("backoffInput").oninput = () => {
  draft.backoff = $("backoffInput").value.split(",")
    .map(s => parseInt(s.trim(),10)).filter(n => !isNaN(n) && n > 0).map(m => m*60);
  updateBackoffHint(); updateDirty();
};
$("repeatToggle").onchange = updateDirty;
$("quietToggle").onchange = () => {
  draft.quiet_hours.enabled = $("quietToggle").checked;
  applyQuietState(); updateDirty();
};
$("quietMode").querySelectorAll("button").forEach(b => {
  b.onclick = () => {
    draft.quiet_hours.mode = b.dataset.mode;
    $("quietMode").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    updateDirty();
  };
});

// ---------- 通知渠道 ----------
const CHANNEL_LABELS = {
  bark: "Bark", telegram: "Telegram", webhook: "Webhook", smtp: "邮件", console: "仅日志",
};
const CHANNEL_FIELDS = {
  bark: [
    {k:"url", label:"Bark 地址", ph:"https://api.day.app/你的设备Key", wide:true},
    {k:"group", label:"分组", ph:"Apple"},
    {k:"level", label:"通知等级", select:[
      ["timeSensitive","timeSensitive（专注模式也显示）"],
      ["active","active（立即亮屏）"],
      ["passive","passive（只进通知列表）"],
      ["critical","critical（重要警告，忽略静音）"],
      ["default","不指定"],
    ]},
    {k:"sound", label:"铃声", ph:"留空用 Bark 默认"},
    {k:"volume", label:"音量（0–10，仅重要警告）", ph:"留空用默认", type:"number"},
    {k:"icon", label:"图标 URL", ph:"可留空", wide:true},
  ],
  telegram: [
    {k:"token", label:"Bot Token", ph:"123456:ABC-DEF...", wide:true},
    {k:"chat_id", label:"Chat ID", ph:"例如 123456789"},
  ],
  webhook: [
    {k:"url", label:"Webhook 地址", ph:"https://...", wide:true},
    {k:"template", label:"格式", select:[
      ["text","通用 {text}"],
      ["feishu","飞书"],
      ["dingtalk","钉钉"],
      ["wecom","企业微信"],
    ]},
  ],
  smtp: [
    {k:"host", label:"SMTP 服务器", ph:"smtp.example.com"},
    {k:"port", label:"端口", ph:"465", type:"number"},
    {k:"username", label:"用户名", ph:"me@example.com"},
    {k:"password", label:"密码 / 授权码", ph:"可用 env:SMTP_PASSWORD"},
    {k:"to", label:"收件人", ph:"me@example.com"},
    {k:"security", label:"加密", select:[["ssl","SSL"],["starttls","STARTTLS"],["none","不加密"]]},
  ],
  console: [],
};

function renderChannels() {
  const list = draft.notify || [];
  const box = $("channels");
  if (!list.length) {
    box.innerHTML = `<div class="empty">还没有通知渠道。没有渠道时提醒只会写日志。</div>`;
    return;
  }
  box.innerHTML = list.map((ch, i) => {
    const fields = CHANNEL_FIELDS[ch.type] || [];
    const inputs = fields.map(f => {
      const val = ch[f.k] ?? "";
      let control;
      if (f.select) {
        control = `<select data-ch="${i}" data-k="${f.k}">` + f.select.map(([v,l]) =>
          `<option value="${v}" ${String(val)===v?"selected":""}>${l}</option>`).join("") + `</select>`;
      } else {
        control = `<input data-ch="${i}" data-k="${f.k}" type="${f.type||"text"}"
          value="${esc(val)}" placeholder="${esc(f.ph||"")}">`;
      }
      return `<div class="field" style="margin:0;${f.wide?"grid-column:1/-1":""}">
        <label class="small">${esc(f.label)}</label>${control}</div>`;
    }).join("");
    const masked = ch.url_masked || ch.token_masked || ch.password_masked;
    return `<div style="border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:10px">
      <div class="row" style="justify-content:space-between;margin-bottom:10px">
        <b style="font-size:13.5px">${CHANNEL_LABELS[ch.type]||ch.type}</b>
        <button class="chip" data-del="${i}">移除</button>
      </div>
      ${masked ? `<div class="hint" style="margin-bottom:8px">
        密钥已配置（显示为掩码）。想更换就整个重新填，留空则保持原值。</div>` : ""}
      <div class="grid2">${inputs || '<span class="muted small">这个渠道没有可配置项。</span>'}</div>
    </div>`;
  }).join("");

  box.querySelectorAll("[data-ch]").forEach(el => {
    el.oninput = el.onchange = () => {
      const idx = Number(el.dataset.ch);
      const key = el.dataset.k;
      draft.notify[idx][key] = el.value;
      updateDirty();
    };
  });
  box.querySelectorAll("[data-del]").forEach(btn => {
    btn.onclick = () => {
      draft.notify.splice(Number(btn.dataset.del), 1);
      renderChannels(); updateDirty();
    };
  });
}

document.querySelectorAll("[data-add]").forEach(btn => {
  btn.onclick = () => {
    const type = btn.dataset.add;
    if (!draft.notify) draft.notify = [];
    const preset = {type};
    if (type === "bark") Object.assign(preset, {group:"Apple", level:"timeSensitive"});
    if (type === "webhook") preset.template = "text";
    if (type === "smtp") Object.assign(preset, {port:"465", security:"ssl"});
    draft.notify.push(preset);
    renderChannels(); updateDirty();
  };
});

$("testNotifyBtn").onclick = async () => {
  const btn = $("testNotifyBtn");
  btn.disabled = true;
  btn.textContent = "发送中…";
  try {
    // 带上面板当前值：不用先保存就能验证 key 对不对
    const r = await fetch("/api/notify/test", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({notify: currentPayload().notify}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "发送失败");
    toast(d.message || "测试推送已发送");
  } catch (e) {
    toast("测试失败：" + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "发送测试推送";
  }
};

loadSettings().then(() => {
  poll();
  loadLogs();
  setInterval(poll, 2000);
  // 日志每 5 秒刷新：10 秒一轮，5 秒足够及时，又不至于频繁打断滚动
  setInterval(loadLogs, 5000);
});
