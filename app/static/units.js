"use strict";

/*
ustate = {
  text, annotations, source_unit, target_unit,  // 原始输入（导出时原样回传，后端重算）
  items, errors, lengths,                        // 预览结果
}
*/
let ustate = null;

const UNIT_NAMES = {
  codepoint: "码点",
  utf16: "UTF-16",
  utf8_byte: "UTF-8 字节",
};
const UNIT_ORDER = ["codepoint", "utf16", "utf8_byte"];

const $ = (id) => document.getElementById(id);
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
  return {
    text: $("unitText").value,
    annotations,
    source_unit: $("sourceUnit").value,
    target_unit: $("targetUnit").value,
  };
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
    const data = await api("/api/units/preview", payload);
    ustate = { ...payload, items: data.items, errors: data.errors, lengths: data.lengths };
    $("resultPanel").hidden = false;
    renderAll();
    $("resultPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("inputError").textContent = "预览失败：" + e.message;
  }
}

/* ---------- 导出 ---------- */

async function runExport() {
  if (!ustate || ustate.errors.length) return;
  try {
    const data = await api("/api/units/export", {
      text: ustate.text,
      annotations: ustate.annotations,
      source_unit: ustate.source_unit,
      target_unit: ustate.target_unit,
    });
    download("annotations_units.json", JSON.stringify(data.annotations, null, 2));
    toast(`已导出 ${data.count} 条批注（${UNIT_NAMES[data.source_unit]} → ${UNIT_NAMES[data.target_unit]}）`);
  } catch (e) {
    if (e.status === 409 && e.data && e.data.detail) {
      ustate.errors = e.data.detail.errors || ustate.errors;
      renderAll();
      toast(`存在 ${ustate.errors.length} 个偏移错误，禁止导出`);
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
  renderTable();
  renderExportBar();
}

function renderSummary() {
  const chips = [
    { text: `批注 ${ustate.annotations.length} 条`, cls: "st-plain" },
    { text: `源：${UNIT_NAMES[ustate.source_unit]}`, cls: "st-plain" },
    { text: `目标：${UNIT_NAMES[ustate.target_unit]}`, cls: "st-plain" },
  ];
  UNIT_ORDER.forEach((u) => chips.push({
    text: `${UNIT_NAMES[u]}总长 ${ustate.lengths[u]}`, cls: "st-plain",
  }));
  chips.push(ustate.errors.length
    ? { text: `错误 ${ustate.errors.length} 个`, cls: "st-error" }
    : { text: "全部合法", cls: "st-ok" });
  $("summary").innerHTML = chips
    .map((c) => `<span class="chip ${c.cls}">${c.text}</span>`)
    .join("");
}

function renderErrors() {
  $("errorBox").hidden = !ustate.errors.length;
  $("errorList").innerHTML = ustate.errors.map((e) =>
    `<li><span class="badge err-${e.type}">${escapeHtml(e.field)}</span> ${escapeHtml(e.message)}</li>`
  ).join("");
}

function coordCell(item, unit) {
  if (!item.coordinates) return `<td class="range">—</td>`;
  const c = item.coordinates[unit];
  const cls = unit === ustate.target_unit ? "range tgt" : "range";
  return `<td class="${cls}">[${c.start}, ${c.end})</td>`;
}

function renderTable() {
  const head = `<tr><th>#</th><th>id</th><th>批注原文（按码点）</th>` +
    UNIT_ORDER.map((u) =>
      `<th>${UNIT_NAMES[u]}${u === ustate.source_unit ? "（源）" : ""}${u === ustate.target_unit ? "（目标）" : ""}</th>`
    ).join("") +
    `<th>错误</th></tr>`;
  const rows = ustate.items.map((item, i) => {
    const raw = ustate.annotations[i] || {};
    const excerpt = item.coordinates
      ? escapeHtml(item.text)
      : `<span class="bad-raw">[${escapeHtml(raw.start)}, ${escapeHtml(raw.end)})</span>`;
    const errs = item.errors.length
      ? item.errors.map((e) => `<div class="cell-err">${escapeHtml(e.message)}</div>`).join("")
      : "";
    return `<tr class="${item.coordinates ? "" : "row-bad"}"><td class="range">${i + 1}</td>` +
      `<td><span class="tag nest">${escapeHtml(item.id)}</span></td>` +
      `<td class="tok">${excerpt}</td>` +
      UNIT_ORDER.map((u) => coordCell(item, u)).join("") +
      `<td>${errs}</td></tr>`;
  }).join("");
  $("unitTable").innerHTML = head + rows;
}

function renderExportBar() {
  const blocked = ustate.errors.length > 0;
  $("exportBtn").disabled = blocked;
  $("exportHint").textContent = blocked
    ? `存在 ${ustate.errors.length} 个偏移错误，禁止导出`
    : `导出时后端重新建立边界映射重算；仅替换 start、end 为${UNIT_NAMES[ustate.target_unit]}坐标，保持输入顺序与其余字段不变`;
}

/* ---------- 内置示例（汉字 / 组合字符 / 辅助平面字符；含越界与代理对内部错误） ---------- */

function loadDemo() {
  // 🎉（U+1F389，辅助平面：1 码点 / 2 UTF-16 单元 / 4 UTF-8 字节）
  // é = e + U+0301 组合尖音符（2 码点 / 2 UTF-16 单元 / 1+2=3 UTF-8 字节）
  const text = "汉字é🎉A";

  // 偏移均按 UTF-16 代码单元给出：
  //   汉[0,1) 字[1,2) e[2,3) ́[3,4) 🎉[4,6)（代理对） A[6,7)
  const annotations = [
    { id: "u1", start: 0, end: 2, label: "汉字" },    // 汉字
    { id: "u2", start: 2, end: 4, label: "组合" },    // é（e + 组合音符，2 码点）
    { id: "u3", start: 4, end: 6, label: "表情" },    // 🎉 整个代理对
    { id: "u4", start: 5, end: 6, label: "坏边界" },  // 5 落在 🎉 代理对内部
    { id: "u5", start: 6, end: 9, label: "越界" },    // 9 超过 UTF-16 总长 7
  ];

  $("unitText").value = text;
  $("annJson").value = JSON.stringify(annotations, null, 2);
  $("sourceUnit").value = "utf16";
  $("targetUnit").value = "utf8_byte";
  $("inputError").textContent = "";
  toast("示例已载入（含 2 条非法批注），点击「开始预览」");
}
