#!/usr/bin/env node
// Простой текстовый отчёт «что доминирует в бандле» поверх dist/stats.html.
// Парсит data из rollup-plugin-visualizer treemap, агрегирует по top-level
// node_modules-пакетам.
//
// Использование:
//   npm run build:analyze && node scripts/bundle-report.mjs

import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const stats = resolve(here, "..", "dist", "stats.html");
if (!existsSync(stats)) {
  console.error("dist/stats.html не найден — запустите 'npm run build:analyze' сначала");
  process.exit(1);
}

const html = readFileSync(stats, "utf-8");
// data — большой JSON; regex с .*? упирается; парсим через подсчёт скобок.
const start = html.indexOf("const data = ");
if (start < 0) {
  console.error("не нашёл `const data = ` — формат visualizer изменился");
  process.exit(1);
}
let i = html.indexOf("{", start);
let depth = 0;
let inStr = false;
let esc = false;
let end = -1;
for (; i < html.length; i++) {
  const ch = html[i];
  if (inStr) {
    if (esc) { esc = false; continue; }
    if (ch === "\\") { esc = true; continue; }
    if (ch === "\"") inStr = false;
    continue;
  }
  if (ch === "\"") inStr = true;
  else if (ch === "{") depth++;
  else if (ch === "}") {
    depth--;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) {
  console.error("не сматчил конец JSON-объекта");
  process.exit(1);
}
const data = JSON.parse(html.slice(html.indexOf("{", start), end));

// Структура (visualizer v7+): { version, tree: {name, children: [...] }, nodeParts: { uid → {renderedLength, gzipLength, brotliLength}}, ... }
// Каждый лист дерева имеет `uid`. Размер берём из nodeParts по uid. Пакет определяем
// по пути в дереве: ищем сегмент «node_modules/<pkg>» (или «node_modules/@scope/pkg»).

const parts = data.nodeParts || {};

function collect(node, path, acc) {
  if (Array.isArray(node.children)) {
    const nextPath = [...path, node.name || ""];
    for (const c of node.children) collect(c, nextPath, acc);
    return;
  }
  if (!node.uid) return;
  const sz = parts[node.uid];
  if (!sz) return;
  // Определяем пакет по path: ищем сегмент после «node_modules».
  let pkg = "(app)";
  const idx = path.lastIndexOf("node_modules");
  if (idx >= 0 && idx + 1 < path.length) {
    const a = path[idx + 1] || "";
    if (a.startsWith("@") && idx + 2 < path.length) {
      pkg = `${a}/${path[idx + 2]}`;
    } else {
      pkg = a;
    }
  }
  if (!acc.has(pkg)) acc.set(pkg, { rendered: 0, gzip: 0, brotli: 0 });
  const cur = acc.get(pkg);
  cur.rendered += sz.renderedLength || 0;
  cur.gzip += sz.gzipLength || 0;
  cur.brotli += sz.brotliLength || 0;
}

const byPkg = new Map();
collect(data.tree, [], byPkg);

const sorted = [...byPkg.entries()].sort((a, b) => b[1].gzip - a[1].gzip);
const totalRaw = sorted.reduce((s, [, v]) => s + v.rendered, 0);
const totalGz = sorted.reduce((s, [, v]) => s + v.gzip, 0);

console.log("\nТоп пакетов по размеру в бандле (gzip):\n");
console.log("Пакет".padEnd(36), "Raw KB".padStart(10), "Gzip KB".padStart(14));
console.log("-".repeat(62));
for (const [name, v] of sorted.slice(0, 20)) {
  const pct = ((v.gzip / totalGz) * 100).toFixed(1);
  console.log(
    name.padEnd(36),
    (v.rendered / 1024).toFixed(1).padStart(10),
    `${(v.gzip / 1024).toFixed(1).padStart(7)} (${pct.padStart(4)}%)`,
  );
}
console.log("-".repeat(62));
console.log("ИТОГО".padEnd(36),
  (totalRaw / 1024).toFixed(1).padStart(10),
  (totalGz / 1024).toFixed(1).padStart(10));
console.log("\nИнтерактивный treemap: открыть dist/stats.html в браузере.");
