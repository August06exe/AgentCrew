/* panel-lib.js — 管家面板公共小工具 */
async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  return r.json();
}
function el(id) { return document.getElementById(id); }
function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function toast(msg, bad) {
  const t = el('toast');
  t.textContent = msg;
  t.className = 'bh-toast ' + (bad ? 'bh-bad' : 'bh-good');
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 4000);
}
async function loadSkinFallback() {
  // 由 index 页引用皮肤后调用；面板页直接引用本地 skin，无需兜底
}
