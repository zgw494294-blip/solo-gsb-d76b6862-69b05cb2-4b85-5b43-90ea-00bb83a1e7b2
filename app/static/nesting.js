"use strict";

/* 错误类型的展示文案 */
const ERROR_META = {
  duplicate_span: { label: "同范围" },
  crossing:       { label: "部分交叉" },
  root_label:     { label: "根 label 非法" },
  child_label:    { label: "子 label 非法" },
};

/*
nstate = {
  text, annotations, rules,  // 原始输入（导出时原样回传，后端重新计算）
  errors,                    // 审计错误列表
  tree,                      // 嵌套树（存在 duplicate_span / crossing 时为 null）
}
*/
let nstate = null;

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);          // 按码点切分，避免 UTF-16 代理对错位
const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

document.addEventListener("DOMContentLoaded", () => {
  $("demoBtn").addEventListener("click", loadDemo);
  $("auditBtn").addEventListener("click", runAudit);
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
    throw new Error("结构规则 JSON 必须是对象");
  }
  return { text: $("nestText").value, annotations, rules };
}

/* ---------- 审计 ---------- */

async function runAudit() {
  $("inputError").textContent = "";
  let payload;
  try {
    payload = readInput();
  } catch (e) {
    $("inputError").textContent = "输入 JSON 解析失败：" + e.message;
    return;
  }
  try {
    const data = await api("/api/nesting/preview", payload);
    nstate = { ...payload, errors: data.errors, tree: data.tree };
    $("resultPanel").hidden = false;
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "审计失败：" + e.message;
  }
}

/* ---------- 导出 ---------- */

async function runExport() {
  if (!nstate || nstate.errors.length) return;
  try {
    const data = await api("/api/nesting/export", {
      text: nstate.text,
      annotations: nstate.annotations,
      rules: nstate.rules,
    });
    download("nesting_tree.json", JSON.stringify(data.tree, null, 2));
    toast(`已导出 ${data.count} 条批注的稳定嵌套 JSON`);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.detail) {
      nstate.errors = e.data.detail.errors || nstate.errors;
      renderAll();
      toast(`存在 ${nstate.errors.length} 个审计错误，禁止导出`);
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
  renderTree();
  renderExportBar();
}

function renderSummary() {
  const chips = [{ text: `批注 ${nstate.annotations.length} 条`, cls: "st-plain" }];
  chips.push(nstate.errors.length
    ? { text: `错误 ${nstate.errors.length} 个`, cls: "st-error" }
    : { text: "无错误", cls: "st-ok" });
  if (nstate.tree) chips.push({ text: `根节点 ${nstate.tree.length} 个`, cls: "st-plain" });
  $("summary").innerHTML = chips
    .map((c) => `<span class="chip ${c.cls}">${c.text}</span>`)
    .join("");
}

function renderErrors() {
  $("errorBox").hidden = !nstate.errors.length;
  $("errorList").innerHTML = nstate.errors.map((e) => {
    const meta = ERROR_META[e.type] || { label: e.type };
    return `<li><span class="badge err-${e.type}">${meta.label}</span> ${escapeHtml(e.message)}</li>`;
  }).join("");
}

function nodeHtml(node) {
  const kids = node.children.map(nodeHtml).join("");
  return `<li><div class="nest-node">
    <span class="tag nest">${escapeHtml(String(node.id))}</span>
    <span class="lbl">${escapeHtml(node.label)}</span>
    <span class="cand-range">[${node.start}, ${node.end})</span>
    <span class="ann-excerpt">${escapeHtml(node.text)}</span>
  </div>${kids ? `<ul>${kids}</ul>` : ""}</li>`;
}

function renderTree() {
  if (!nstate.tree) {
    $("nestTree").innerHTML =
      `<div class="ann-detail">区间关系存在错误（duplicate_span / crossing），无法构建嵌套树。</div>`;
    return;
  }
  $("nestTree").innerHTML = nstate.tree.length
    ? `<ul class="nest-tree">${nstate.tree.map(nodeHtml).join("")}</ul>`
    : `<div class="ann-detail">（无批注）</div>`;
}

function renderExportBar() {
  const blocked = nstate.errors.length > 0;
  $("exportBtn").disabled = blocked;
  $("exportHint").textContent = blocked
    ? `存在 ${nstate.errors.length} 个审计错误，禁止导出`
    : "将导出保留原 id、label、范围的稳定嵌套 JSON（后端重新计算）";
}

/* ---------- 内置示例（严格包含 / 端点相接 / label 规则错误） ---------- */

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
  const text = "第一章 总则\n本模块负责数据采集与清洗，目标是统一口径。";

  const at = (sub, n = 1) => {
    const start = cpIndex(text, sub, n);
    return { start, end: start + cps(sub).length };
  };
  const ann = (id, range, label) => ({ id, start: range.start, end: range.end, label });

  const head = at("第一章 总则");
  const annotations = [
    { id: "n-doc", start: 0, end: cps(text).length, label: "文档" },  // 根：文档 ✓
    ann("n-ch1",   head, "章节"),                    // 文档 → 章节 ✓
    ann("n-title", at("总则"), "短语"),              // 章节 → 短语 ✗（规则未允许）
    // 与 n-ch1 端点相接（相接不算重叠）；文档 → 段落 ✗（规则未允许）
    { id: "n-body", start: head.end, end: cps(text).length, label: "段落" },
    ann("n-goal",  at("目标是统一口径"), "句子"),     // 段落 → 句子 ✓
    ann("n-core",  at("统一口径"), "短语"),          // 句子 → 短语 ✓
  ];

  const rules = {
    root_labels: ["文档"],
    children: {
      "文档": ["章节"],
      "章节": ["句子"],
      "段落": ["句子", "短语"],
      "句子": ["短语"],
    },
  };

  $("nestText").value = text;
  $("annJson").value = JSON.stringify(annotations, null, 2);
  $("rulesJson").value = JSON.stringify(rules, null, 2);
  $("inputError").textContent = "";
  toast("示例已载入（含 2 处 label 规则错误），点击「开始审计」");
}
