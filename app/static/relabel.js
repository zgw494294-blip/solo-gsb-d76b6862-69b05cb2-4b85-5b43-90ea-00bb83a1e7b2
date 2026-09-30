"use strict";

/*
lstate = {
  text, annotations, rules,        // 原始输入（导出时原样回传，后端重新求值）
  items, errors, target_labels,    // 预览结果（迁移链与逐项错误）
}
*/
let lstate = null;

const ERROR_META = {
  no_rule: { label: "缺少可用规则" },
  cycle:   { label: "循环" },
};

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);          // 按码点切分，避免 UTF-16 代理对错位
const escapeHtml = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

document.addEventListener("DOMContentLoaded", () => {
  $("demoBtn").addEventListener("click", loadDemo);
  $("previewBtn").addEventListener("click", runPreview);
  $("exportBtn").addEventListener("click", runExport);
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

function readInput() {
  const annotations = JSON.parse($("annJson").value.trim() || "[]");
  if (!Array.isArray(annotations)) throw new Error("批注 JSON 必须是数组");
  const rules = JSON.parse($("rulesJson").value.trim() || "{}");
  if (typeof rules !== "object" || rules === null || Array.isArray(rules)) {
    throw new Error("规则 JSON 必须是对象");
  }
  return { text: $("rlText").value, annotations, rules };
}

/* ---------- 预览 ---------- */

async function runPreview() {
  $("inputError").textContent = "";
  let payload;
  try {
    payload = readInput();
  } catch (e) {
    $("inputError").textContent = "输入 JSON 解析失败：" + e.message;
    return;
  }
  try {
    const data = await api("/api/relabel/preview", payload);
    lstate = { ...payload, items: data.items, errors: data.errors,
               target_labels: data.target_labels };
    $("resultPanel").hidden = false;
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "迁移失败：" + e.message;
  }
}

/* ---------- 导出 ---------- */

async function runExport() {
  if (!lstate || lstate.errors.length) return;
  try {
    const data = await api("/api/relabel/export", {
      text: lstate.text,
      annotations: lstate.annotations,
      rules: lstate.rules,
    });
    download("annotations_relabeled.json", JSON.stringify(data.annotations, null, 2));
    toast(`已导出 ${data.count} 条批注（后端重新求值，只替换 label）`);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.detail) {
      lstate.errors = e.data.detail.errors || lstate.errors;
      renderAll();
      toast(`存在 ${lstate.errors.length} 个迁移错误，禁止导出`);
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

/* ---------- 渲染 ---------- */

function renderAll() {
  renderSummary();
  renderErrors();
  renderTextView();
  renderChainTable();
  renderExportBar();
}

function renderSummary() {
  const done = lstate.items.filter((it) => it.status === "done");
  const kept = done.filter((it) => it.steps.length === 0).length;
  const chips = [
    { text: `批注 ${lstate.annotations.length} 条`, cls: "st-plain" },
    { text: `目标标签 ${lstate.target_labels.length} 个`, cls: "st-plain" },
    { text: `规则 ${(lstate.rules.rules || []).length} 条`, cls: "st-plain" },
    { text: `已迁移 ${done.length - kept} 条`, cls: "st-plain" },
    { text: `无需迁移 ${kept} 条`, cls: "st-plain" },
  ];
  chips.push(lstate.errors.length
    ? { text: `错误 ${lstate.errors.length} 个`, cls: "st-error" }
    : { text: "全部成功", cls: "st-ok" });
  $("summary").innerHTML = chips
    .map((c) => `<span class="chip ${c.cls}">${c.text}</span>`)
    .join("");
}

function renderErrors() {
  $("errorBox").hidden = !lstate.errors.length;
  $("errorList").innerHTML = lstate.errors.map((e) => {
    const meta = ERROR_META[e.type] || { label: e.type };
    return `<li><span class="badge err-${e.type}">${meta.label}</span> ${escapeHtml(e.message)}</li>`;
  }).join("");
}

function renderTextView() {
  // 按码点统计批注覆盖次数，相同覆盖数的连续码点合并为一段
  const chars = cps(lstate.text);
  const cov = new Array(chars.length).fill(0);
  lstate.items.forEach((it) => {
    for (let i = it.start; i < it.end; i++) cov[i] += 1;
  });
  let html = "";
  let i = 0;
  while (i < chars.length) {
    let j = i + 1;
    while (j < chars.length && cov[j] === cov[i]) j++;
    const seg = escapeHtml(chars.slice(i, j).join(""));
    html += cov[i] ? `<span class="rv-cov c${Math.min(cov[i], 3)}">${seg}</span>` : seg;
    i = j;
  }
  $("textView").innerHTML = html || "（空文本）";
}

function chainHtml(item) {
  const parts = item.chain.map((lb, i) => {
    const isBad = item.status === "error" && i === item.chain.length - 1;
    return `<span class="lbl${isBad ? " bad" : ""}">${escapeHtml(lb)}</span>`;
  });
  return parts.join(`<span class="chain-arrow">→</span>`);
}

function stepsHtml(item) {
  if (!item.steps.length) return `<span class="ann-detail">（无需改写）</span>`;
  return item.steps.map((s) => {
    const cond = s.pattern === null ? "回退" : `pattern「${escapeHtml(s.pattern)}」`;
    return `<div class="chain-step">规则#${s.index}：${escapeHtml(s.from)} → ` +
           `${escapeHtml(s.to)}（${cond}）</div>`;
  }).join("");
}

function resultHtml(item) {
  if (item.status === "error") {
    return item.errors.map((e) => `<div class="cell-err">${escapeHtml(e.message)}</div>`).join("");
  }
  const tag = item.steps.length ? "已迁移" : "已是目标标签";
  return `<span class="lbl final">${escapeHtml(item.final_label)}</span> ` +
         `<span class="ann-detail">${tag}</span>`;
}

function renderChainTable() {
  const head = `<tr><th>#</th><th>id</th><th>范围</th><th>批注原文</th>` +
    `<th>迁移链</th><th>命中规则</th><th>结果 / 错误</th></tr>`;
  const rows = lstate.items.map((item, i) => {
    const bad = item.status === "error";
    return `<tr class="${bad ? "row-bad" : ""}"><td class="range">${i + 1}</td>` +
      `<td><span class="tag nest">${escapeHtml(String(item.id))}</span></td>` +
      `<td class="range">[${item.start}, ${item.end})</td>` +
      `<td class="tok">${escapeHtml(item.text)}</td>` +
      `<td class="chain-cell">${chainHtml(item)}</td>` +
      `<td>${stepsHtml(item)}</td>` +
      `<td>${resultHtml(item)}</td></tr>`;
  }).join("");
  $("chainTable").innerHTML = head + rows;
}

function renderExportBar() {
  const blocked = lstate.errors.length > 0;
  $("exportBtn").disabled = blocked;
  $("exportHint").textContent = blocked
    ? `存在 ${lstate.errors.length} 个迁移错误，禁止导出`
    : "导出时后端重新求值迁移链；保持输入顺序与 id、范围和其余字段不变，只替换 label";
}

/* ---------- 内置示例（多步链 / 条件分流 / 回退 / 标签合并 / 循环 / 缺规则） ---------- */

function cpIndex(haystack, needle) {
  const idx = haystack.indexOf(needle);
  return idx === -1 ? -1 : cps(haystack.slice(0, idx)).length;
}

function loadDemo() {
  const text = "采集数据，清洗样本，统一口径；保留杂项。";

  const ann = (id, sub, label) => {
    const start = cpIndex(text, sub);
    return { id, start, end: start + cps(sub).length, label };
  };
  const annotations = [
    ann("r1", "采集", "上古-流程"),  // 上古-流程 → 旧-流程 → 流程（多步链）
    ann("r2", "数据", "旧-对象"),    // pattern「数据」命中 → 实体（条件分流）
    ann("r3", "清洗", "旧-对象"),    // pattern 未中 → 回退 → 流程
    ann("r4", "样本", "旧-甲"),      // 旧-甲 → 实体（标签合并）
    ann("r5", "统一", "旧-乙"),      // 旧-乙 → 实体（标签合并）
    ann("r6", "口径", "坏-甲"),      // 坏-甲 → 坏-乙 → 坏-甲（循环）
    ann("r7", "保留", "孤立"),       // 不在目标体系且无规则（缺少可用规则）
    ann("r8", "杂项", "实体"),       // 已是目标标签（0 步）
  ];

  const rules = {
    target_labels: ["实体", "流程"],
    rules: [
      { from: "上古-流程", to: "旧-流程" },
      { from: "旧-流程", to: "流程" },
      { from: "旧-对象", to: "实体", pattern: "数据" },  // 条件规则：原文恰为「数据」
      { from: "旧-对象", to: "流程" },                   // 回退规则：须排在该 from 最后
      { from: "旧-甲", to: "实体" },
      { from: "旧-乙", to: "实体" },
      { from: "坏-甲", to: "坏-乙" },
      { from: "坏-乙", to: "坏-甲" },
    ],
  };

  $("rlText").value = text;
  $("annJson").value = JSON.stringify(annotations, null, 2);
  $("rulesJson").value = JSON.stringify(rules, null, 2);
  $("inputError").textContent = "";
  toast("示例已载入（含循环与缺规则各 1 条，导出将被禁止），点击「开始迁移」");
}
