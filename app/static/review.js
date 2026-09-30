"use strict";

/*
rstate = {
  text, annotations, max_chars, context_chars,  // 原始输入（导出 / 导入时原样回传，后端重新切片）
  fragments,                                     // 切片预览结果
  imported,                                      // 导入结果 {annotations, dropped, count}
}
*/
let rstate = null;

const ACTION_META = {
  keep:    { label: "保留", cls: "act-keep" },
  drop:    { label: "舍弃", cls: "act-drop" },
  correct: { label: "修正", cls: "act-correct" },
};

const ERROR_META = {
  unknown_fragment:   { label: "未知切片" },
  duplicate_fragment: { label: "切片重复" },
  digest_mismatch:    { label: "摘要不符" },
  foreign_id:         { label: "归属错误" },
  duplicate_id:       { label: "id 重复" },
  missing_id:         { label: "id 缺失" },
  unknown_action:     { label: "决策非法" },
  bad_range:          { label: "范围非法" },
};

const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);          // 按码点切分，避免 UTF-16 代理对错位
const escapeHtml = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

document.addEventListener("DOMContentLoaded", () => {
  $("demoBtn").addEventListener("click", loadDemo);
  $("previewBtn").addEventListener("click", runPreview);
  $("exportBtn").addEventListener("click", runExport);
  $("templateBtn").addEventListener("click", fillTemplate);
  $("importBtn").addEventListener("click", runImport);
  $("downloadImportBtn").addEventListener("click", downloadImport);
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
  const max_chars = Number($("maxChars").value);
  const context_chars = Number($("ctxChars").value);
  if (!Number.isInteger(max_chars) || max_chars < 1) throw new Error("max_chars 必须为正整数");
  if (!Number.isInteger(context_chars) || context_chars < 0) throw new Error("context_chars 必须为非负整数");
  return { text: $("rvText").value, annotations, max_chars, context_chars };
}

/* ---------- 生成切片 ---------- */

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
    const data = await api("/api/review/preview", payload);
    rstate = { ...payload, fragments: data.fragments, imported: null };
    $("resultPanel").hidden = false;
    $("importPanel").hidden = false;
    resetImport();
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "生成切片失败：" + e.message;
  }
}

/* ---------- 导出 JSONL ---------- */

async function runExport() {
  if (!rstate) return;
  try {
    const data = await api("/api/review/export", {
      text: rstate.text,
      annotations: rstate.annotations,
      max_chars: rstate.max_chars,
      context_chars: rstate.context_chars,
    });
    download("review_slices.jsonl", data.jsonl, "application/x-ndjson");
    toast(`已导出 ${data.count} 个切片的 JSONL（后端重新装箱）`);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}

/* ---------- 审阅模板与导入 ---------- */

function fillTemplate() {
  if (!rstate) return;
  const tpl = rstate.fragments.map((f) => ({
    fragment_id: f.fragment_id,
    sha256: f.sha256,
    decisions: f.annotations.map((a) => ({ id: a.id, action: "keep" })),
  }));
  $("reviewJson").value = JSON.stringify(tpl, null, 2);
  $("importPanel").scrollIntoView({ behavior: "smooth" });
  toast("已按当前切片生成全 keep 模板，可改为 drop / correct 后导入");
}

async function runImport() {
  if (!rstate) return;
  $("importError").textContent = "";
  let reviews;
  try {
    reviews = JSON.parse($("reviewJson").value.trim() || "[]");
    if (!Array.isArray(reviews)) throw new Error("审阅结果 JSON 必须是数组");
  } catch (e) {
    $("importError").textContent = "审阅结果 JSON 解析失败：" + e.message;
    return;
  }
  try {
    const data = await api("/api/review/import", {
      text: rstate.text,
      annotations: rstate.annotations,
      max_chars: rstate.max_chars,
      context_chars: rstate.context_chars,
      reviews,
    });
    rstate.imported = data;
    renderImportResult();
    toast(`已导入 ${data.count} 条批注（舍弃 ${data.dropped.length} 条）`);
  } catch (e) {
    const errors = e.data && e.data.detail && e.data.detail.errors;
    if (e.status === 422 && errors) {
      renderImportErrors(errors);
      toast(`审阅结果含 ${errors.length} 个错误，已拒绝整批`);
    } else {
      $("importError").textContent = "导入失败：" + e.message;
    }
  }
}

function downloadImport() {
  if (!rstate || !rstate.imported) return;
  download("review_imported.json", JSON.stringify(rstate.imported, null, 2), "application/json");
}

function download(name, text, type) {
  const blob = new Blob([text], { type: type || "application/json" });
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

/* ---------- 渲染切片 ---------- */

function renderAll() {
  renderSummary();
  renderFragments();
  $("exportHint").textContent =
    `窗口 ≤ max_chars，每侧上下文 ≤ context_chars；导出时后端重新装箱，每行一个切片`;
}

function renderSummary() {
  const chips = [
    { text: `批注 ${rstate.annotations.length} 条`, cls: "st-plain" },
    { text: `切片 ${rstate.fragments.length} 个`, cls: "st-plain" },
    { text: `max_chars ${rstate.max_chars}`, cls: "st-plain" },
    { text: `context_chars ${rstate.context_chars}`, cls: "st-plain" },
  ];
  $("summary").innerHTML = chips
    .map((c) => `<span class="chip ${c.cls}">${c.text}</span>`)
    .join("");
}

function fragViewHtml(f) {
  // 按码点统计覆盖次数，相同覆盖数的连续码点合并为一段
  const chars = cps(f.text);
  const cov = new Array(chars.length).fill(0);
  f.annotations.forEach((a) => {
    for (let i = a.start; i < a.end; i++) cov[i] += 1;
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
  return html;
}

function fragCard(f) {
  const chars = cps(f.text);
  const rows = f.annotations.map((a, i) => {
    const excerpt = escapeHtml(chars.slice(a.start, a.end).join(""));
    return `<tr><td class="range">${i + 1}</td>` +
      `<td><span class="tag nest">${escapeHtml(a.id)}</span></td>` +
      `<td><span class="lbl">${escapeHtml(a.label)}</span></td>` +
      `<td class="range">[${a.start}, ${a.end})</td>` +
      `<td class="range">[${f.start + a.start}, ${f.start + a.end})</td>` +
      `<td class="tok">${excerpt}</td></tr>`;
  }).join("");
  const table = f.annotations.length
    ? `<div class="seq-table-wrap"><table class="seq-table"><tr>` +
      `<th>#</th><th>id</th><th>label</th><th>局部范围</th><th>全局范围</th><th>原文</th></tr>${rows}</table></div>`
    : "";
  return `<div class="frag-card">
    <div class="frag-head">
      <span class="tag frag">${escapeHtml(f.fragment_id)}</span>
      <span class="cand-range">全局 [${f.start}, ${f.end})</span>
      <span>窗口 ${f.end - f.start} 码点</span>
      <span>批注 ${f.annotations.length} 条</span>
      <span class="sha" title="${f.sha256}">SHA-256 ${f.sha256.slice(0, 12)}…</span>
    </div>
    <pre class="frag-view">${fragViewHtml(f)}</pre>
    ${table}
  </div>`;
}

function renderFragments() {
  $("fragList").innerHTML = rstate.fragments.length
    ? rstate.fragments.map(fragCard).join("")
    : `<div class="ann-detail">（无批注，未生成切片）</div>`;
}

/* ---------- 渲染导入 ---------- */

function resetImport() {
  rstate.imported = null;
  $("importResult").hidden = true;
  $("importErrorBox").hidden = true;
  $("importError").textContent = "";
}

function renderImportErrors(errors) {
  $("importResult").hidden = true;
  $("importErrorBox").hidden = false;
  $("importErrorList").innerHTML = errors.map((e) => {
    const meta = ERROR_META[e.type] || { label: e.type };
    return `<li><span class="badge err-${e.type}">${meta.label}</span> ${escapeHtml(e.message)}</li>`;
  }).join("");
}

function renderImportResult() {
  const data = rstate.imported;
  $("importErrorBox").hidden = true;
  $("importResult").hidden = false;
  const chars = cps(rstate.text);
  const head = `<tr><th>#</th><th>id</th><th>label</th><th>决策</th><th>全局范围</th><th>所属切片</th><th>原文</th></tr>`;
  const rows = data.annotations.map((a, i) => {
    const meta = ACTION_META[a.action];
    const excerpt = escapeHtml(chars.slice(a.start, a.end).join(""));
    return `<tr><td class="range">${i + 1}</td>` +
      `<td><span class="tag nest">${escapeHtml(a.id)}</span></td>` +
      `<td><span class="lbl">${escapeHtml(a.label)}</span></td>` +
      `<td><span class="badge ${meta.cls}">${meta.label}</span></td>` +
      `<td class="range">[${a.start}, ${a.end})</td>` +
      `<td>${escapeHtml(a.fragment_id)}</td>` +
      `<td class="tok">${excerpt}</td></tr>`;
  }).join("");
  const dropped = data.dropped.length
    ? `<tr><td colspan="7" class="range">已舍弃：${data.dropped.map(escapeHtml).join("、")}</td></tr>`
    : "";
  $("importTable").innerHTML = head + rows + dropped;
  $("importHint").textContent =
    `保留 ${data.annotations.filter((a) => a.action === "keep").length} 条 · ` +
    `修正 ${data.annotations.filter((a) => a.action === "correct").length} 条 · ` +
    `舍弃 ${data.dropped.length} 条`;
  $("importResult").scrollIntoView({ behavior: "smooth" });
}

/* ---------- 内置示例（合并装箱 / 封口 / 左边界贴齐 / 右侧余量转左 / 辅助平面字符） ---------- */

function cpIndex(haystack, needle) {
  const idx = haystack.indexOf(needle);
  return idx === -1 ? -1 : cps(haystack.slice(0, idx)).length;
}

function loadDemo() {
  // 🎉（U+1F389，辅助平面字符，占 1 个码点）
  const text = "总则：数据采集要规范🎉，清洗与标注分离。验收时复核样本，最终口径以评审为准。";

  const at = (sub) => {
    const start = cpIndex(text, sub);
    return { start, end: start + cps(sub).length };
  };
  const ann = (id, sub, label) => {
    const r = at(sub);
    return { id, start: r.start, end: r.end, label };
  };
  const annotations = [
    ann("a-采集", "数据采集", "流程"),
    ann("a-规范", "规范", "要求"),
    ann("a-表情", "🎉", "符号"),
    ann("a-清洗", "清洗", "流程"),
    ann("a-标注", "标注", "流程"),
    ann("a-样本", "样本", "对象"),
    ann("a-评审", "评审", "流程"),
  ];

  $("rvText").value = text;
  $("annJson").value = JSON.stringify(annotations, null, 2);
  $("maxChars").value = 20;
  $("ctxChars").value = 6;
  $("inputError").textContent = "";
  toast("示例已载入（前 5 条装为 1 片并贴齐左边界，后 2 条右侧余量转左），点击「生成切片」");
}
