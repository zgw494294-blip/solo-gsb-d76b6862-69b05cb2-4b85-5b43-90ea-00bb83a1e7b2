"use strict";

/*
sstate = {
  text, annotations,  // 原始输入（导出时原样回传，后端重新计算）
  layers,             // 层数
  tokens,             // [{text, start, end, tags:[各层标签]}]
}
*/
let sstate = null;

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);          // 按码点切分，避免 UTF-16 代理对错位
const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

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
  return { text: $("seqText").value, annotations };
}

/* ---------- 预览 ---------- */

async function runPreview() {
  $("inputError").textContent = "";
  let payload;
  try {
    payload = readInput();
  } catch (e) {
    $("inputError").textContent = "批注 JSON 解析失败：" + e.message;
    return;
  }
  try {
    const data = await api("/api/sequence/preview", payload);
    sstate = { ...payload, layers: data.layers, tokens: data.tokens };
    $("resultPanel").hidden = false;
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "生成失败：" + e.message;
  }
}

/* ---------- 导出 ---------- */

async function runExport() {
  if (!sstate) return;
  try {
    const data = await api("/api/sequence/export", {
      text: sstate.text,
      annotations: sstate.annotations,
    });
    download("sequence_corpus.conll", data.conll);
    toast(`已导出 ${data.token_count} 个 token、${data.layers} 层标签（UTF-8 CoNLL）`);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}

function download(name, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
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
  renderTable();
  $("exportHint").textContent =
    `将下载 UTF-8 编码的 CoNLL 文件（每行：token start end 各层标签，空格分隔）`;
}

function renderSummary() {
  const chips = [
    `批注 ${sstate.annotations.length} 条`,
    `token ${sstate.tokens.length} 个`,
    `层 ${sstate.layers} 层`,
  ];
  $("summary").innerHTML = chips
    .map((c) => `<span class="chip" style="background:#334152">${c}</span>`)
    .join("");
}

function tagCell(tag) {
  const cls = tag === "O" ? "o" : tag[0].toLowerCase();
  return `<span class="seq-tag ${cls}">${escapeHtml(tag)}</span>`;
}

function renderTable() {
  const head = `<tr><th>#</th><th>token</th><th>start</th><th>end</th>` +
    Array.from({ length: sstate.layers }, (_, i) => `<th>层 ${i + 1}</th>`).join("") +
    `</tr>`;
  const rows = sstate.tokens.map((t, i) =>
    `<tr><td class="range">${i + 1}</td>` +
    `<td class="tok">${escapeHtml(t.text)}</td>` +
    `<td class="range">${t.start}</td><td class="range">${t.end}</td>` +
    t.tags.map((tag) => `<td>${tagCell(tag)}</td>`).join("") +
    `</tr>`).join("");
  $("seqTable").innerHTML = head + rows;
}

/* ---------- 内置示例（覆盖切分 / 边界切开 / 分层 / BILOU 各分支） ---------- */

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
  const text = "第一章 总则\n本模块用DataX数据采集与清洗，目标是统一口径。🎉";

  const at = (sub, n = 1) => {
    const start = cpIndex(text, sub, n);
    return { start, end: start + cps(sub).length };
  };
  const ann = (id, range, label) => ({ id, start: range.start, end: range.end, label });

  const annotations = [
    ann("s-chapter", at("总则"), "章节"),            // 单 token → U-章节
    ann("s-flow",    at("DataX数据采集"), "流程"),    // 跨 ASCII 串与汉字串，且边界切开汉字串
    ann("s-core",    at("采集与清洗"), "核心"),       // 与 s-flow 重叠 → 分到另一层
    ann("s-goal",    at("目标是统一口径"), "目标"),   // 单 token → U-目标
    ann("s-emoji",   at("🎉"), "标记"),              // 单个其他字符（辅助平面字符，占 1 码点）→ U-标记
  ];

  $("seqText").value = text;
  $("annJson").value = JSON.stringify(annotations, null, 2);
  $("inputError").textContent = "";
  toast("示例已载入，点击「生成预览」");
}
