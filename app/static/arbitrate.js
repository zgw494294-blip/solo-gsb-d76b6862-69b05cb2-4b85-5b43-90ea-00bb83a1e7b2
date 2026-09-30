"use strict";

/* 组状态 / 决策的展示文案 */
const GROUP_META = {
  auto:     { label: "自动通过", desc: "一左一右且范围、label 完全相同" },
  pending:  { label: "待仲裁",   desc: "需整组裁决" },
  left:     { label: "采用左侧", desc: "整组采用左侧批注" },
  right:    { label: "采用右侧", desc: "整组采用右侧批注" },
  discard:  { label: "已舍弃",   desc: "整组全部舍弃" },
};
const GROUP_ORDER = ["auto", "pending", "left", "right", "discard"];

/*
astate = {
  text, left, right,  // 原始输入（导出时原样回传，后端重新分组）
  groups,             // /api/arbitrate 返回的仲裁组
  decisions,          // { group_id: "left" | "right" | "discard" }
  selectedGid,        // 当前选中组
}
*/
let astate = null;

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);          // 按码点切分，避免 UTF-16 代理对错位
const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

document.addEventListener("DOMContentLoaded", () => {
  $("demoBtn").addEventListener("click", loadDemo);
  $("arbBtn").addEventListener("click", runArbitrate);
  $("exportBtn").addEventListener("click", runExport);
  $("groupList").addEventListener("click", onListClick);
  $("arbView").addEventListener("click", onViewClick);
});

/* ---------- 与后端通信 ---------- */

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === "string" ? data.detail
      : (data.detail && data.detail.message) || `请求失败（${res.status}）`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function parseSide(el, name) {
  const anns = JSON.parse(el.value.trim() || "[]");
  if (!Array.isArray(anns)) throw new Error(`${name}批注 JSON 必须是数组`);
  return anns;
}

/* ---------- 比对 ---------- */

async function runArbitrate() {
  $("inputError").textContent = "";
  let left, right;
  try {
    left = parseSide($("leftJson"), "左侧");
    right = parseSide($("rightJson"), "右侧");
  } catch (e) {
    $("inputError").textContent = "批注 JSON 解析失败：" + e.message;
    return;
  }
  const payload = { text: $("arbText").value, left, right };
  try {
    const data = await api("/api/arbitrate", payload);
    astate = {
      text: payload.text,
      left,
      right,
      groups: data.groups,
      decisions: {},
      selectedGid: data.groups.length ? data.groups[0].group_id : null,
    };
    $("resultPanel").hidden = false;
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "比对失败：" + e.message;
  }
}

/* ---------- 导出 ---------- */

async function runExport() {
  if (!astate) return;
  try {
    const data = await api("/api/arbitrate/export", {
      text: astate.text,
      left: astate.left,
      right: astate.right,
      decisions: astate.decisions,
    });
    download("arbitration_result.json",
      JSON.stringify({ annotations: data.annotations, decisions: data.decisions }, null, 2));
    toast(`已导出 ${data.annotations.length} 条批注、${data.decisions.length} 组决策`);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.detail) {
      const gids = e.data.detail.pending_groups || [];
      toast(`存在 ${gids.length} 个未处理的仲裁组，禁止导出：${gids.join("、")}`);
      astate.selectedGid = gids[0];
      renderAll();
    } else {
      toast("导出失败：" + e.message);
    }
  }
}

function download(name, text) {
  const blob = new Blob([text], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

/* ---------- 状态推导 ---------- */

function effStatus(g) {
  if (g.status === "auto") return "auto";
  return astate.decisions[g.group_id] || "pending";
}

function pendingLeft() {
  return astate.groups.filter((g) => effStatus(g) === "pending").length;
}

/* ---------- 渲染 ---------- */

function renderAll() {
  renderSummary();
  renderList();
  renderOverlay();
  renderExportBar();
}

function renderSummary() {
  const counts = {};
  for (const g of astate.groups) {
    const st = effStatus(g);
    counts[st] = (counts[st] || 0) + 1;
  }
  let html = GROUP_ORDER.filter((s) => counts[s])
    .map((s) => `<span class="chip st-${s}">${GROUP_META[s].label} ${counts[s]}</span>`)
    .join("");
  html += `<span class="chip" style="background:#334152">共 ${astate.groups.length} 组</span>`;
  const left = pendingLeft();
  if (left > 0) html += `<span class="chip" style="background:#dc2626">未处理仲裁组 ${left} 个</span>`;
  $("summary").innerHTML = html;
}

function annRow(a, side) {
  return `<div class="arb-ann">
    <span class="tag ${side}">${escapeHtml(String(a.id))}</span>
    <span class="lbl">${escapeHtml(a.label)}</span>
    <span class="cand-range">[${a.start}, ${a.end})</span>
    <span class="ann-excerpt">${escapeHtml(a.text)}</span>
  </div>`;
}

function renderList() {
  const html = astate.groups.map((g) => {
    const st = effStatus(g);
    const meta = GROUP_META[st];
    const sel = g.group_id === astate.selectedGid ? " selected" : "";
    const decided = astate.decisions[g.group_id];

    let body = `<div class="side-cols">
      <div class="side">
        <h4>左侧 · ${g.left.length} 条</h4>
        ${g.left.map((a) => annRow(a, "left")).join("") || '<div class="ann-detail">（无）</div>'}
      </div>
      <div class="side">
        <h4>右侧 · ${g.right.length} 条</h4>
        ${g.right.map((a) => annRow(a, "right")).join("") || '<div class="ann-detail">（无）</div>'}
      </div>
    </div>`;

    if (g.status === "auto") {
      body += `<div class="ann-detail">${meta.desc}，无需处理。</div>`;
    } else if (!decided) {
      body += `<div class="arb-actions">
        <button class="btn small take-left" data-action="left" data-gid="${g.group_id}">整组采用左侧</button>
        <button class="btn small take-right" data-action="right" data-gid="${g.group_id}">整组采用右侧</button>
        <button class="btn small abandon" data-action="discard" data-gid="${g.group_id}">全部舍弃</button>
      </div>`;
    } else {
      body += `<div class="decided${decided === "discard" ? " abandoned" : ""}">${GROUP_META[decided].desc}
        <button class="btn small undo" data-action="undo" data-gid="${g.group_id}">撤销</button></div>`;
    }

    return `<div class="ann-item${sel}" data-gid="${g.group_id}">
      <div class="ann-head">
        <span class="ann-id">${g.group_id}</span>
        <span class="badge st-${st}">${meta.label}</span>
        <span class="ann-meta">${g.left.length} 左 / ${g.right.length} 右</span>
      </div>
      ${body}
    </div>`;
  }).join("");
  $("groupList").innerHTML = html || `<div class="ann-item">无仲裁组</div>`;
}

/* 把文本切成原子段，叠加左右两侧范围 */
function renderOverlay() {
  const chars = cps(astate.text);
  const items = [];
  for (const g of astate.groups) {
    for (const a of g.left) items.push({ ...a, side: "l", gid: g.group_id });
    for (const a of g.right) items.push({ ...a, side: "r", gid: g.group_id });
  }
  const cuts = new Set([0, chars.length]);
  for (const a of items) { cuts.add(a.start); cuts.add(a.end); }
  const bounds = [...cuts].sort((x, y) => x - y);

  let html = "";
  for (let i = 0; i + 1 < bounds.length; i++) {
    const s = bounds[i], e = bounds[i + 1];
    const seg = escapeHtml(chars.slice(s, e).join(""));
    const cover = items.filter((a) => a.start <= s && a.end >= e);
    if (!cover.length) { html += seg; continue; }
    const hasL = cover.some((a) => a.side === "l");
    const hasR = cover.some((a) => a.side === "r");
    const cls = hasL && hasR ? "ov-both" : hasL ? "ov-left" : "ov-right";
    const gids = [...new Set(cover.map((a) => a.gid))];
    const sel = gids.includes(astate.selectedGid) ? " sel" : "";
    const title = cover.map((a) => `${a.side === "l" ? "左" : "右"}:${a.id}(${a.label})`).join("　");
    html += `<span class="ov ${cls}${sel}" data-gid="${gids[0]}" title="${escapeHtml(title)}">${seg}</span>`;
  }
  $("arbView").innerHTML = html;
  scrollToSelected();
}

function renderExportBar() {
  const left = pendingLeft();
  $("exportBtn").disabled = left > 0;
  $("exportHint").textContent = left > 0
    ? `还有 ${left} 个仲裁组未处理（含单侧分量），处理后方可导出`
    : `将导出 ${astate.groups.length} 组决策与采纳的批注（重新编号）`;
}

function scrollToSelected() {
  if (astate.selectedGid == null) return;
  const m = $("arbView").querySelector(".ov.sel");
  if (m) m.scrollIntoView({ block: "center" });
}

/* ---------- 交互 ---------- */

function onListClick(e) {
  const btn = e.target.closest("[data-action]");
  if (btn) {
    const gid = btn.dataset.gid;
    switch (btn.dataset.action) {
      case "left":
      case "right":
      case "discard":
        astate.decisions[gid] = btn.dataset.action;
        astate.selectedGid = gid;
        break;
      case "undo":
        delete astate.decisions[gid];
        astate.selectedGid = gid;
        break;
    }
    renderAll();
    return;
  }
  const item = e.target.closest(".ann-item");
  if (item) {
    astate.selectedGid = item.dataset.gid;
    renderAll();
  }
}

function onViewClick(e) {
  const m = e.target.closest(".ov");
  if (m && m.dataset.gid) {
    astate.selectedGid = m.dataset.gid;
    renderAll();
  }
}

/* ---------- 内置示例（覆盖全部分组分支） ---------- */

function cpIndex(haystack, needle, n = 1) {
  // needle 第 n 次（从 1 计）出现的码点偏移；未找到返回 -1
  let from = 0, idx = -1;
  for (let i = 0; i < n; i++) {
    idx = haystack.indexOf(needle, from);
    if (idx === -1) return -1;
    from = idx + 1;
  }
  return cps(haystack.slice(0, idx)).length;
}

function loadDemo() {
  const text = [
    "第一章 总则",
    "本模块负责数据采集与清洗，目标是统一口径。",
    "第二段 保持不变的内容，双方一致认可。",
    "第三段 条款需要双方确认，意见不一。",
    "第四段 仅左侧保留的旧说明。",
    "第五段 右侧新补充的说明。",
  ].join("\n");

  const at = (sub, n = 1) => {
    const start = cpIndex(text, sub, n);
    return { start, end: start + cps(sub).length };
  };
  const ann = (id, range, label) => ({ id, start: range.start, end: range.end, label });

  const left = [
    ann("L-goal",   at("目标是统一口径"), "目标"),              // 与右侧完全一致 → 自动通过
    ann("L-clean",  at("数据采集与清洗"), "流程"),               // 与右侧同范围但 label 不同 → 仲裁
    ann("L-para",   at("第二段 保持不变的内容，双方一致认可。"), "段落"), // 一对多（右侧拆成两条）
    ann("L-clause", at("条款需要双方确认"), "待定"),             // 多对多（与 L-view 一起）
    ann("L-view",   at("双方确认，意见不一"), "争议"),
    ann("L-old",    at("仅左侧保留的旧说明"), "遗留"),           // 单侧分量（仅左侧）
  ];
  const right = [
    ann("R-goal",   at("目标是统一口径"), "目标"),               // 自动通过
    ann("R-clean",  at("数据采集与清洗"), "核心"),               // 同范围、label 冲突
    ann("R-p1",     at("第二段"), "段首"),                       // 与 R-p2 一起对 L-para 一对多
    ann("R-p2",     at("保持不变的内容"), "共识"),
    ann("R-mid",    at("需要双方确认，意见"), "重点"),           // 多对多
    ann("R-risk",   at("意见不一"), "风险"),
    ann("R-new",    at("右侧新补充的说明"), "新增"),             // 单侧分量（仅右侧）
  ];

  $("arbText").value = text;
  $("leftJson").value = JSON.stringify(left, null, 2);
  $("rightJson").value = JSON.stringify(right, null, 2);
  $("inputError").textContent = "";
  toast("示例已载入，点击「开始比对」");
}
