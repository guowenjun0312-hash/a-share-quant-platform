"use strict";

/* ---------- 工具 ---------- */
const $ = (id) => document.getElementById(id);
const API_BASE = (location.protocol === "file:") ? "http://127.0.0.1:8765" : "";
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

// 自选股（来自东方财富软件 self_Stocks_v1.xml，共184只，含市场前缀）
const WATCH = [];
const WATCH_SECIDS = WATCH.map((w) => w[1]).join(",");
const WATCH_SECID_MAP = Object.fromEntries(WATCH);
const WATCH_CODES = "";
// 信号监控默认标的（原 9 只：指数+持仓；可在信号监控页修改）
const SIGNAL_CODES = [];

function fmtAmt(v) {
  if (v == null || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(2) + " 亿";
  if (a >= 1e4) return (v / 1e4).toFixed(1) + " 万";
  return v.toFixed(0);
}
function fmtPct(v, digits = 2) {
  if (v == null || isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
}
function fmtPctRaw(v, digits = 2) {
  if (v == null || isNaN(v)) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
}
function quotePct(v, code) {
  // 涨跌幅合理性校验：主板±21%、创业板(30x/301)±31%、北交所±35%，超出视为数据异常
  if (v == null || isNaN(v)) return "—";
  const limit = code && code.startsWith("30") ? 31 : (code && code.startsWith("92") ? 35 : 21);
  if (Math.abs(v) > limit) return "异常(" + (v > 0 ? "+" : "") + v.toFixed(1) + "%)";
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
}
function cls(v) {
  if (v == null || isNaN(v) || Math.abs(v) < 1e-12) return "flat";
  return v > 0 ? "up" : "down";
}
function fmtPrice(v) {
  if (v == null || isNaN(v)) return "—";
  return v.toFixed(2);
}

function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function isTradingTime() {
  const d = new Date();
  const day = d.getDay();
  if (day === 0 || day === 6) return false;
  const hm = d.getHours() * 100 + d.getMinutes();
  return (hm >= 930 && hm <= 1130) || (hm >= 1300 && hm <= 1500);
}

async function api(path, options) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const resp = await fetch(API_BASE + path, { ...options, signal: ctrl.signal });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
    return data;
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("请求超时（服务可能未启动或网络慢）");
    }
    if (e instanceof TypeError) {
      throw new Error("无法连接服务，请双击桌面“大A量化交易平台”启动");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/* ---------- 画布 ---------- */
function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0) return null;
  const w = Math.round(rect.width * dpr);
  const h = Math.round(rect.height * dpr);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}

function drawEmpty(canvas, text) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#7f8db0";
  ctx.font = "13px sans-serif";
  ctx.fillText(text || "暂无数据", 12, 24);
}

function gridLines(ctx, w, h, padL, padR, padT, padB, n) {
  ctx.strokeStyle = "#273450";
  ctx.font = "11px sans-serif";
  ctx.fillStyle = "#7f8db0";
  for (let g = 0; g <= n; g++) {
    const y = padT + (g / n) * (h - padT - padB);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
  }
}

function drawLines(canvas, opts) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  const { series, preClose } = opts;
  if (!series || series.length < 2) {
    drawEmpty(canvas, "暂无分时数据");
    return;
  }
  const padL = 56, padR = 16, padT = 16, padB = 26;
  let lo = Infinity, hi = -Infinity;
  for (const p of series) {
    lo = Math.min(lo, p.price);
    hi = Math.max(hi, p.price);
  }
  if (preClose) { lo = Math.min(lo, preClose); hi = Math.max(hi, preClose); }
  const span = Math.max(hi - lo, 1e-9);
  lo -= span * 0.08; hi += span * 0.08;
  const X = (i) => padL + (i / (series.length - 1)) * (w - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

  ctx.clearRect(0, 0, w, h);
  ctx.font = "11px sans-serif";
  gridLines(ctx, w, h, padL, padR, padT, padB, 4);
  for (let g = 0; g <= 4; g++) {
    const y = padT + (g / 4) * (h - padT - padB);
    const val = hi - (g / 4) * (hi - lo);
    ctx.fillStyle = "#7f8db0";
    ctx.fillText(val.toFixed(2), 6, y + 4);
  }
  for (const t of [0, Math.floor(series.length / 2), series.length - 1]) {
    ctx.fillText(series[t].time.slice(11), X(t) - 18, h - 8);
  }
  if (preClose) {
    const y = Y(preClose);
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = "#7f8db0";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.setLineDash([]);
  }
  const line = (key, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.6;
    ctx.beginPath();
    series.forEach((p, i) => {
      const x = X(i), y = Y(p[key]);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  line("avg", "#f5c451");
  line("price", "#4f8cff");
}

function drawBacktest(canvas, data) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  const topH = h * 0.72, gap = 26;
  const padL = 62, padR = 16, padT = 18, padB = 24;
  ctx.clearRect(0, 0, w, h);
  ctx.font = "11px sans-serif";
  if (!data || !data.dates || data.dates.length < 2) {
    drawEmpty(canvas, "暂无回测数据");
    return;
  }
  const n = data.dates.length;
  const eq = data.equity, bh = data.buyhold, dd = data.drawdown;
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    lo = Math.min(lo, eq[i], bh[i]);
    hi = Math.max(hi, eq[i], bh[i]);
  }
  const span = Math.max(hi - lo, 1e-9);
  lo -= span * 0.06; hi += span * 0.06;
  const X = (i) => padL + (i / (n - 1)) * (w - padL - padR);
  const Y = (v, top, hh) => top + (1 - (v - lo) / (hi - lo)) * (hh - padT - padB);
  const grid = (top, hh) => {
    for (let g = 0; g <= 3; g++) {
      const y = top + padT + (g / 3) * (hh - padT - padB);
      ctx.strokeStyle = "#273450";
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      const v = hi - (g / 3) * (hi - lo);
      ctx.fillText(v.toFixed(3), 4, y + 4);
    }
  };
  grid(padT, topH - padT);
  const plot = (arr, top, hh, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.7;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const x = X(i), y = Y(arr[i], top, hh);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  };
  plot(bh, padT, topH - padT, "#8a93a8");
  plot(eq, padT, topH - padT, "#4f8cff");
  ctx.fillStyle = "#4f8cff"; ctx.fillText("策略净值", padL, padT + 12);
  ctx.fillStyle = "#8a93a8"; ctx.fillText("买入持有", padL + 80, padT + 12);
  const botTop = topH + gap;
  ctx.strokeStyle = "#273450";
  ctx.beginPath(); ctx.moveTo(padL, botTop); ctx.lineTo(w - padR, botTop); ctx.stroke();
  let dmin = 0, dmax = 0;
  for (const v of dd) { dmin = Math.min(dmin, v); dmax = Math.max(dmax, v); }
  const dSpan = Math.max(dmax - dmin, 1e-9);
  ctx.fillStyle = "rgba(239,83,80,0.35)";
  ctx.beginPath();
  ctx.moveTo(X(0), botTop + padT + (1 - (0 - dmin) / dSpan) * (h - botTop - padT - padB));
  for (let i = 0; i < n; i++) {
    const x = X(i);
    const y = botTop + padT + (1 - (dd[i] - dmin) / dSpan) * (h - botTop - padT - padB);
    ctx.lineTo(x, y);
  }
  for (let i = n - 1; i >= 0; i--) {
    const x = X(i);
    const y = botTop + padT + (1 - (0 - dmin) / dSpan) * (h - botTop - padT - padB);
    ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#7f8db0";
  ctx.fillText((dmin * 100).toFixed(1) + "%", padL, h - 8);
  ctx.fillText(data.dates[0], padL + 50, h - 8);
  ctx.fillText(data.dates[n - 1], w - padR - 74, h - 8);
}

/* ---------- K线图 ---------- */
function drawKline(canvas, rows, ind) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  ctx.clearRect(0, 0, w, h);
  if (!rows || rows.length < 2) { drawEmpty(canvas, "暂无K线数据"); return; }
  const n = rows.length;
  const padL = 62, padR = 16;
  const mainTop = 20, mainH = h * 0.52;
  const volTop = mainTop + mainH + 14, volH = h * 0.14;
  const macdTop = volTop + volH + 14, macdH = h - macdTop - 26;
  const plotW = w - padL - padR;
  const cw = plotW / n;
  const X = (i) => padL + cw * i + cw / 2;

  let lo = Infinity, hi = -Infinity;
  for (const r of rows) {
    lo = Math.min(lo, r.low);
    hi = Math.max(hi, r.high);
  }
  for (const key of ["ma5", "ma10", "ma20", "ma60"]) {
    for (const v of ind?.[key] || []) {
      if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
  }
  const span = Math.max(hi - lo, 1e-9);
  lo -= span * 0.05; hi += span * 0.05;
  const Y = (v) => mainTop + (1 - (v - lo) / (hi - lo)) * mainH;

  ctx.font = "11px sans-serif";
  gridLines(ctx, w, h, padL, padR, mainTop, h - mainTop, 4);
  for (let g = 0; g <= 4; g++) {
    const y = mainTop + (g / 4) * mainH;
    const v = hi - (g / 4) * (hi - lo);
    ctx.fillStyle = "#7f8db0";
    ctx.fillText(v.toFixed(2), 6, y + 4);
  }
  const xstep = Math.max(1, Math.ceil(n / 12));
  for (let i = 0; i < n; i += xstep) {
    ctx.fillStyle = "#7f8db0";
    ctx.fillText(rows[i].date.slice(2), X(i) - 16, h - 8);
  }

  for (let i = 0; i < n; i++) {
    const r = rows[i];
    const up = r.close >= r.open;
    const color = up ? "#ef5350" : "#26a69a";
    ctx.strokeStyle = color; ctx.fillStyle = color;
    const x = X(i);
    ctx.beginPath();
    ctx.moveTo(x, Y(r.high));
    ctx.lineTo(x, Y(r.low));
    ctx.stroke();
    const bodyTop = Y(Math.max(r.open, r.close));
    const bodyBot = Y(Math.min(r.open, r.close));
    ctx.fillRect(x - cw * 0.34, bodyTop, cw * 0.68, Math.max(bodyBot - bodyTop, 1));
  }
  const maSpec = [
    ["ma5", "#f5c451"], ["ma10", "#4f8cff"],
    ["ma20", "#ab6cff"], ["ma60", "#8a93a8"],
  ];
  for (const [key, color] of maSpec) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.3;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < n; i++) {
      const v = ind?.[key]?.[i];
      if (v == null) continue;
      if (!started) { ctx.moveTo(X(i), Y(v)); started = true; }
      else ctx.lineTo(X(i), Y(v));
    }
    ctx.stroke();
  }
  ctx.fillStyle = "#f5c451"; ctx.fillText("MA5", padL, mainTop + 12);
  ctx.fillStyle = "#4f8cff"; ctx.fillText("MA10", padL + 40, mainTop + 12);
  ctx.fillStyle = "#ab6cff"; ctx.fillText("MA20", padL + 88, mainTop + 12);
  ctx.fillStyle = "#8a93a8"; ctx.fillText("MA60", padL + 136, mainTop + 12);

  // 成交量
  let vmax = 0;
  for (const r of rows) vmax = Math.max(vmax, r.volume || 0);
  ctx.strokeStyle = "#273450";
  ctx.beginPath(); ctx.moveTo(padL, volTop); ctx.lineTo(w - padR, volTop); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(padL, volTop + volH); ctx.lineTo(w - padR, volTop + volH); ctx.stroke();
  ctx.fillStyle = "#7f8db0";
  ctx.fillText("量", 20, volTop + volH / 2);
  for (let i = 0; i < n; i++) {
    const r = rows[i];
    const vh = (r.volume || 0) / vmax * (volH - 6);
    ctx.fillStyle = r.close >= r.open ? "rgba(239,83,80,0.55)" : "rgba(38,166,154,0.55)";
    ctx.fillRect(X(i) - cw * 0.34, volTop + volH - vh, cw * 0.68, vh);
  }
  const vol5 = ind?.volume_ma5 || [], vol10 = ind?.volume_ma10 || [];
  const drawVolLine = (arr, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v == null) continue;
      const y = volTop + volH - (v / vmax) * (volH - 6);
      if (!started) { ctx.moveTo(X(i), y); started = true; }
      else ctx.lineTo(X(i), y);
    }
    ctx.stroke();
  };
  drawVolLine(vol5, "#f5c451");
  drawVolLine(vol10, "#4f8cff");

  // MACD
  const dif = ind?.dif || [], dea = ind?.dea || [], macd = ind?.macd || [];
  let mlo = 0, mhi = 0;
  for (let i = 0; i < n; i++) {
    for (const v of [dif[i], dea[i], macd[i]]) {
      if (v == null) continue;
      mlo = Math.min(mlo, v); mhi = Math.max(mhi, v);
    }
  }
  const mSpan = Math.max(mhi - mlo, 1e-9);
  mlo -= mSpan * 0.1; mhi += mSpan * 0.1;
  const MY = (v) => macdTop + (1 - (v - mlo) / (mhi - mlo)) * macdH;
  ctx.strokeStyle = "#273450";
  ctx.beginPath(); ctx.moveTo(padL, macdTop); ctx.lineTo(w - padR, macdTop); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(padL, macdTop + macdH); ctx.lineTo(w - padR, macdTop + macdH); ctx.stroke();
  const zeroY = MY(0);
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = "#44506b";
  ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(w - padR, zeroY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#7f8db0";
  ctx.fillText("MACD", 16, macdTop + macdH / 2);
  for (let i = 0; i < n; i++) {
    const v = macd[i];
    if (v == null) continue;
    const y0 = MY(0), y1 = MY(v);
    ctx.fillStyle = v >= 0 ? "rgba(239,83,80,0.6)" : "rgba(38,166,154,0.6)";
    ctx.fillRect(X(i) - cw * 0.3, Math.min(y0, y1), cw * 0.6, Math.max(Math.abs(y1 - y0), 1));
  }
  const macdLine = (arr, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v == null) continue;
      const y = MY(v);
      if (!started) { ctx.moveTo(X(i), y); started = true; }
      else ctx.lineTo(X(i), y);
    }
    ctx.stroke();
  };
  macdLine(dif, "#f5c451");
  macdLine(dea, "#4f8cff");
  ctx.fillStyle = "#f5c451"; ctx.fillText("DIF", padL, macdTop + 12);
  ctx.fillStyle = "#4f8cff"; ctx.fillText("DEA", padL + 40, macdTop + 12);
}

/* ---------- 板块K线+资金流图 ---------- */
function drawBoardDetail(canvas, kline, fflow) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  ctx.clearRect(0, 0, w, h);
  if (!kline || kline.length < 2) { drawEmpty(canvas, "暂无板块K线"); return; }
  const n = kline.length;
  const padL = 56, padR = 16, padT = 14, padB = 22;
  const topH = h * 0.62, botTop = topH + 12, botH = h - botTop - padB;
  const plotW = w - padL - padR;
  const X = (i) => padL + (i / (n - 1)) * plotW;
  let lo = Infinity, hi = -Infinity;
  for (const r of kline) {
    lo = Math.min(lo, r.low); hi = Math.max(hi, r.high);
  }
  const span = Math.max(hi - lo, 1e-9);
  lo -= span * 0.06; hi += span * 0.06;
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (topH - padT - 8);
  ctx.font = "11px sans-serif";
  ctx.strokeStyle = "#273450";
  ctx.beginPath(); ctx.moveTo(padL, topH); ctx.lineTo(w - padR, topH); ctx.stroke();
  ctx.strokeStyle = "#4f8cff"; ctx.lineWidth = 1.6;
  ctx.beginPath();
  kline.forEach((r, i) => { const x = X(i), y = Y(r.close); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.stroke();
  ctx.fillStyle = "#7f8db0";
  ctx.fillText("板块指数", padL, padT + 12);
  const xstep = Math.max(1, Math.ceil(n / 10));
  for (let i = 0; i < n; i += xstep) ctx.fillText(kline[i].date.slice(2), X(i) - 16, h - 8);

  // 资金流（按日期对齐到K线 x 轴）
  const byDate = new Map((fflow || []).map((r) => [r.date, r]));
  let mabs = 0;
  for (const r of fflow || []) mabs = Math.max(mabs, Math.abs(r.main_net || 0));
  if (mabs > 0) {
    const MY = (v) => botTop + botH / 2 - (v / mabs) * (botH / 2 - 4);
    ctx.strokeStyle = "#273450";
    ctx.beginPath(); ctx.moveTo(padL, botTop + botH / 2); ctx.lineTo(w - padR, botTop + botH / 2); ctx.stroke();
    ctx.fillStyle = "#7f8db0";
    ctx.fillText("主力净流入(亿)", padL, botTop + 12);
    kline.forEach((r, i) => {
      const f = byDate.get(r.date);
      if (!f) return;
      const x = X(i);
      const v = f.main_net / 1e8;
      const y0 = MY(0), y1 = MY(f.main_net);
      ctx.fillStyle = v >= 0 ? "rgba(239,83,80,0.7)" : "rgba(38,166,154,0.7)";
      ctx.fillRect(x - 2, Math.min(y0, y1), 4, Math.max(Math.abs(y1 - y0), 1));
    });
  }
}

/* ---------- 模拟盘净资产曲线 ---------- */
function drawPaperEquity(canvas, equity) {
  const s = fitCanvas(canvas);
  if (!s) return;
  const { ctx, w, h } = s;
  ctx.clearRect(0, 0, w, h);
  if (!equity || equity.length < 1) { drawEmpty(canvas, "暂无模拟盘数据（启动后开始记录）"); return; }
  if (equity.length === 1) {
    ctx.fillStyle = "#4f8cff";
    ctx.beginPath();
    ctx.arc(w / 2, h / 2, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#7f8db0";
    ctx.font = "13px sans-serif";
    ctx.fillText("模拟盘已启动，等待首次撮合记录", w / 2 - 95, h / 2 + 22);
    return;
  }
  const n = equity.length;
  const padL = 62, padR = 16, padT = 18, padB = 24;
  const vals = equity.map((e) => e.equity);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, 1e-9);
  lo -= span * 0.08; hi += span * 0.08;
  const X = (i) => padL + (i / (n - 1)) * (w - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
  ctx.font = "11px sans-serif";
  ctx.strokeStyle = "#273450";
  ctx.fillStyle = "#7f8db0";
  for (let g = 0; g <= 4; g++) {
    const y = padT + (g / 4) * (h - padT - padB);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText((hi - (g / 4) * (hi - lo)).toFixed(0), 6, y + 4);
  }
  const first = equity[0].time.slice(5, 16), last = equity[n - 1].time.slice(5, 16);
  ctx.fillText(first, padL, h - 8);
  ctx.fillText(last, w - padR - 90, h - 8);
  ctx.strokeStyle = "#4f8cff"; ctx.lineWidth = 1.8;
  ctx.beginPath();
  vals.forEach((v, i) => { const x = X(i), y = Y(v); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.stroke();
}

/* ---------- 时钟 / 页签 ---------- */
function tickClock() {
  const d = new Date();
  $("clock").textContent = d.toLocaleString("zh-CN", { hour12: false });
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("panel-" + btn.dataset.tab).classList.add("active");
    const tab = btn.dataset.tab;
    if (tab === "realtime") refreshQuotes();
    if (tab === "backtest") requestAnimationFrame(() => drawBacktest($("btCanvas"), lastBt));
    if (tab === "kline" && !klineLoaded) loadKline();
    if (tab === "boards" && !boardsLoaded) loadBoards();
    if (tab === "signals" && !signalsLoaded) refreshSignals();
    if (tab === "market") refreshMarketSignals();
    if (tab === "paper") loadPaper();
    if (tab === "data" && !dataLoaded) loadData();
  });
});

/* ---------- 实时行情 ---------- */
let trendCache = {};
async function refreshQuotes() {
  $("quoteStatus").textContent = "刷新中…";
  try {
    const data = await api("/api/quotes?secids=" + WATCH_SECIDS);
    renderQuotes(data.quotes);
    $("quoteStatus").textContent = "已更新 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    $("quoteStatus").textContent =
      "行情获取失败：" + e.message + "（若服务未启动，请用浏览器打开 http://127.0.0.1:8765）";
  }
}

function renderQuotes(quotes) {
  const body = $("quoteBody");
  body.innerHTML = quotes.map((q) => {
    const clsP = cls(q.pct);
    const name = q.name || q.code;
    const secid = WATCH_SECID_MAP[q.code] || q.code;
    return `<tr data-code="${esc(q.code)}" data-secid="${esc(secid)}">
      <td><b>${esc(name)}</b> <span class="muted">${esc(q.code)}</span></td>
      <td class="num ${clsP}">${fmtPrice(q.price)}</td>
      <td class="num ${clsP}">${quotePct(q.pct, q.code)}</td>
      <td class="num ${clsP}">${q.change == null ? "—" : (q.change >= 0 ? "+" : "") + q.change.toFixed(2)}</td>
      <td class="num">${fmtPrice(q.open)}</td>
      <td class="num ${cls(q.price - q.high)}">${fmtPrice(q.high)}</td>
      <td class="num ${cls(q.price - q.low)}">${fmtPrice(q.low)}</td>
      <td class="num">${fmtPrice(q.prev_close)}</td>
      <td class="num">${fmtAmt(q.amount)}</td>
      <td class="num">${q.vol_ratio == null ? "—" : q.vol_ratio.toFixed(2)}</td>
      <td class="num muted">${q.time ? q.time.slice(11) : "—"}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => loadTrend(tr.dataset.code, tr.dataset.secid));
  });
}

async function loadTrend(code, secid) {
  try {
    const data = secid
      ? await api("/api/trend?secid=" + secid)
      : await api("/api/trend?code=" + code);
    trendCache[code] = data;
    $("trendTitle").textContent = `${data.name} (${data.code}) 当日分时 · 昨收 ${data.pre_close}`;
    drawLines($("trendCanvas"), { series: data.series, preClose: data.pre_close });
  } catch (e) {
    $("trendTitle").textContent = `分时获取失败：${e.message}`;
  }
}

$("refreshQuotes").addEventListener("click", refreshQuotes);
$("autoRefresh").addEventListener("change", () => {
  if ($("autoRefresh").checked) refreshQuotes();
});
setInterval(() => {
  if ($("autoRefresh").checked && $("panel-realtime").classList.contains("active")) {
    refreshQuotes();
  }
}, 5000);

/* ---------- K线分析 ---------- */
let symbols = [], strategies = {}, lastBt = null;
let klineLoaded = false, boardsLoaded = false, signalsLoaded = false, dataLoaded = false;
let lastKline = null, lastBoardDetail = null;
let monitorMap = {};

async function initKline() {
  $("klSymbol").innerHTML = symbols
    .map((s) => `<option value="${esc(s.code)}">${esc(s.name)} (${esc(s.code)})</option>`)
    .join("");
}

async function loadKline() {
  const code = $("klCustom").value.trim() || $("klSymbol").value;
  if (!code) { $("klStatus").textContent = "请选择或输入代码"; return; }
  const days = Number($("klDays").value);
  $("klStatus").textContent = "加载中…";
  try {
    const [data, chips] = await Promise.all([
      api(`/api/kline?code=${code}&days=${days}`),
      api(`/api/chips?code=${code}&lookback=120`).catch(() => null),
    ]);
    lastKline = { canvas: $("klCanvas"), data };
    drawKline($("klCanvas"), data.rows, data.indicators);
    const last = data.rows[data.rows.length - 1];
    const c = chips || {};
    $("klInfo").innerHTML = [
      ["最新价", fmtPrice(last.close) + "  " + fmtPctRaw(last.pct)],
      ["90%集中度", c.concentration90 == null ? "—" : c.concentration90.toFixed(1) + "%"],
      ["70%集中度", c.concentration70 == null ? "—" : c.concentration70.toFixed(1) + "%"],
      ["获利盘", c.profit_ratio == null ? "—" : (c.profit_ratio * 100).toFixed(1) + "%"],
      ["平均成本", c.avg_cost == null ? "—" : fmtPrice(c.avg_cost)],
    ].map(([k, v]) => `<div class="chip"><span>${k}</span><b>${v}</b></div>`).join("");
    $("klStatus").textContent = `${data.name} (${code}) · ${data.rows.length} 根日K · ${data.rows[0].date} ~ ${last.date}`;
    klineLoaded = true;
  } catch (e) {
    $("klStatus").textContent = "K线加载失败：" + e.message;
  }
}

$("klRefresh").addEventListener("click", loadKline);
$("klSymbol").addEventListener("change", () => { $("klCustom").value = ""; });
$("klDays").addEventListener("change", loadKline);

/* ---------- 板块资金 ---------- */
async function loadBoards() {
  $("boardStatus").textContent = "加载中…";
  try {
    const data = await api("/api/boards");
    renderBoards(data.boards);
    $("boardStatus").textContent = "已更新 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    boardsLoaded = true;
  } catch (e) {
    $("boardStatus").textContent = "板块加载失败：" + e.message;
  }
}

function renderBoards(boards) {
  $("boardBody").innerHTML = boards.map((b) => `
    <tr data-bk="${esc(b.bk)}">
      <td><b>${esc(b.name)}</b> <span class="muted">${esc(b.bk)}</span></td>
      <td class="num ${cls(b.pct)}">${fmtPctRaw(b.pct)}</td>
      <td class="num ${cls(b.main_net)}">${b.main_net == null ? "—" : (b.main_net / 1e8).toFixed(2) + " 亿"}</td>
      <td class="num">${b.turnover == null ? "—" : b.turnover.toFixed(2) + "%"}</td>
    </tr>`).join("");
  $("boardBody").querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => loadBoardDetail(tr.dataset.bk));
  });
}

async function loadBoardDetail(bk) {
  $("boardTitle").textContent = "加载板块详情…";
  try {
    const [detail, stocks] = await Promise.all([
      api(`/api/board/detail?bk=${bk}`),
      api(`/api/board_stocks?bk=${bk}`),
    ]);
    lastBoardDetail = { canvas: $("boardCanvas"), detail };
    drawBoardDetail($("boardCanvas"), detail.kline, detail.fflow);
    const name = detail.kline[detail.kline.length - 1];
    $("boardTitle").textContent = `${bk} · 板块指数 ${name.close.toFixed(2)}（${fmtPctRaw(name.pct)}） · 资金流 ${detail.fflow.length} 天`;
    renderBoardStocks(stocks.stocks);
  } catch (e) {
    $("boardTitle").textContent = "板块详情加载失败：" + e.message;
  }
}

function renderBoardStocks(stocks) {
  $("boardStockCount").textContent = `共 ${stocks.length} 只`;
  $("boardStockBody").innerHTML = stocks.map((x) => `
    <tr>
      <td><b>${esc(x.name)}</b> <span class="muted">${esc(x.code)}</span></td>
      <td class="num ${cls(x.pct)}">${fmtPrice(x.price)}</td>
      <td class="num ${cls(x.pct)}">${fmtPctRaw(x.pct)}</td>
      <td class="num">${x.turnover == null ? "—" : x.turnover.toFixed(2) + "%"}</td>
      <td class="num">${x.vol_ratio == null ? "—" : x.vol_ratio.toFixed(2)}</td>
      <td class="num ${cls(x.main_net)}">${x.main_net == null ? "—" : (x.main_net / 1e8).toFixed(2) + " 亿"}</td>
    </tr>`).join("");
}

$("boardRefresh").addEventListener("click", loadBoards);

/* ---------- 选股器 ---------- */
async function runScreener() {
  $("scrStatus").textContent = "选股中…";
  const q = new URLSearchParams({
    min_pct: $("scrMinPct").value || "0",
    max_pct: $("scrMaxPct").value || "99",
    min_turnover: $("scrTurnover").value || "0",
    min_volratio: $("scrVolratio").value || "0",
    min_amount: $("scrAmount").value || "0",
    min_net: $("scrNet").value || "0",
    beat_board: $("scrBeat").checked ? "1" : "0",
    sort: $("scrSort").value,
    limit: $("scrLimit").value,
  });
  try {
    const data = await api("/api/screener?" + q.toString());
    $("scrBody").innerHTML = data.stocks.map((x) => `
      <tr data-code="${esc(x.code)}">
        <td><b>${esc(x.name)}</b> <span class="muted">${esc(x.code)}</span></td>
        <td class="num ${cls(x.pct)}">${fmtPrice(x.price)}</td>
        <td class="num ${cls(x.pct)}">${fmtPctRaw(x.pct)}</td>
        <td class="num">${x.turnover == null ? "—" : x.turnover.toFixed(2) + "%"}</td>
        <td class="num">${x.vol_ratio == null ? "—" : x.vol_ratio.toFixed(2)}</td>
        <td class="num">${fmtAmt(x.amount)}</td>
        <td class="num ${cls(x.main_net)}">${x.main_net == null ? "—" : (x.main_net / 1e8).toFixed(2) + " 亿"}</td>
        <td class="num muted">${esc(x.industry || "—")}</td>
        <td class="num ${cls(x.board_pct)}">${x.board_pct == null ? "—" : fmtPctRaw(x.board_pct)}</td>
        <td class="num">${x.beat_board ? '<span class="badge up">跑赢</span>' : '<span class="muted">否</span>'}</td>
      </tr>`).join("");
    $("scrStatus").textContent = `筛选到 ${data.total} 只，展示前 ${data.stocks.length} 只`;
    $("scrBody").querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("dblclick", () => {
        $("klCustom").value = tr.dataset.code;
        document.querySelector('.tab[data-tab="kline"]').click();
        loadKline();
      });
    });
  } catch (e) {
    $("scrStatus").textContent = "选股失败：" + e.message;
  }
}

$("scrRun").addEventListener("click", runScreener);

/* ---------- 回测 ---------- */
async function initBacktest() {
  try {
    const [symData, stratData, monitorData] = await Promise.all([
      api("/api/symbols"),
      api("/api/strategies"),
      api("/api/monitor_map").catch(() => ({ map: {} })),
    ]);
    symbols = symData.symbols;
    strategies = stratData;
    monitorMap = monitorData.map || {};
    const localOpts = symbols
      .map((s) => `<option value="${esc(s.code)}">${esc(s.name)} (${esc(s.code)})</option>`)
      .join("");
    const watchOpts = WATCH.map((w) => `<option value="${esc(w[0])}">${esc(w[0])}（自选）</option>`).join("");
    const opts = `<optgroup label="自选股（${WATCH.length}）">${watchOpts}</optgroup><optgroup label="本地数据">${localOpts}</optgroup>`;
    $("btSymbol").innerHTML = localOpts;
    $("klSymbol").innerHTML = opts;
    $("sigCodes").value = WATCH_CODES;
    $("paperCodes").value = WATCH_CODES;
    $("btStrategy").innerHTML = Object.entries(strategies)
      .map(([k, v]) => `<option value="${esc(k)}" ${k === "oversold_bounce" ? "selected" : ""}>${esc(v.label)}</option>`)
      .join("");
    initPaper();
    const sigSel = $("sigStrategySel");
    if (sigSel) {
      sigSel.innerHTML =
        '<option value="best_per_stock">个股最优（短期收益）</option>' +
        Object.entries(strategies)
          .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`)
          .join("");
      sigSel.value = "best_per_stock";
      updateSigStrategyBadge();
    }
    filterBtSymbols();
    renderParams();
    try {
      const n = await api("/api/notes");
      $("noteText").value = n.notes.trim();
    } catch (e) { /* 忽略 */ }
  } catch (e) {
    $("btStatus").textContent =
      "回测面板初始化失败：" + e.message + "（请确认服务已启动：http://127.0.0.1:8765）";
  }
}

function filterBtSymbols() {
  const q = ($("btSearch").value || "").trim().toLowerCase();
  const keep = symbols.filter(
    (s) => !q || s.code.includes(q) || (s.name || "").toLowerCase().includes(q)
  );
  const cur = $("btSymbol").value;
  $("btSymbol").innerHTML = keep.length
    ? keep.map((s) => `<option value="${esc(s.code)}">${esc(s.name)} (${esc(s.code)})</option>`).join("")
    : '<option value="">无匹配标的</option>';
  if (keep.some((s) => s.code === cur)) $("btSymbol").value = cur;
  $("btSearchCount").textContent = q ? `匹配 ${keep.length} 只` : `共 ${symbols.length} 只`;
}

$("btSearch").addEventListener("input", filterBtSymbols);
$("btSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const opts = [...$("btSymbol").options];
    if (opts.length === 1 && opts[0].value) {
      $("btSymbol").value = opts[0].value;
      runBacktest();
    }
  }
});

function renderParams() {
  const key = $("btStrategy").value;
  const meta = strategies[key];
  const box = $("btParams");
  box.innerHTML = "";
  if (!meta) return;
  const p = meta.params || {};
  const entries = Object.entries(p);
  if (!entries.length) {
    box.innerHTML = '<div class="hint">该策略无参数</div>';
    return;
  }
  const grid = document.createElement("div");
  grid.className = "param-grid";
  for (const [pk, spec] of entries) {
    const lab = document.createElement("label");
    lab.innerHTML = `<span>${esc(spec.label)}</span>`;
    const input = document.createElement("input");
    input.type = "number";
    input.id = "param-" + pk;
    input.value = spec.default;
    input.min = spec.min; input.max = spec.max; input.step = spec.step;
    lab.appendChild(input);
    grid.appendChild(lab);
  }
  box.appendChild(grid);
}

function collectParams() {
  const key = $("btStrategy").value;
  const meta = strategies[key];
  const params = {};
  for (const pk of Object.keys(meta?.params || {})) {
    const el = $("param-" + pk);
    if (el && el.value !== "") {
      params[pk] = Number(el.value);
    } else {
      params[pk] = meta.params[pk].default;
    }
  }
  return params;
}

async function runBacktest() {
  $("btRun").disabled = true;
  $("btStatus").textContent = "回测中…";
  try {
    const body = {
      code: $("btSymbol").value,
      name: symbols.find((s) => s.code === $("btSymbol").value)?.name || $("btSymbol").value,
      strategy: $("btStrategy").value,
      params: collectParams(),
      stop: $("btStop").value ? Number($("btStop").value) : null,
    };
    const data = await api("/api/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    lastBt = data;
    renderMetrics(data.metrics);
    drawBacktest($("btCanvas"), data);
    renderTrades(data.trades);
    const b = data.board;
    const boardTxt = b && b.industry
      ? `所属板块：${b.industry}（${b.bk}）· 板块K线 ${b.kline_days} 天 · 资金流 ${b.fflow_days} 天`
      : "该标的不属于行业板块（指数/ETF 不触发板块条件）";
    const best = data.best;
    const bestTxt = best && best.strategy
      ? `本股最优：${strategies[best.strategy]?.label || best.strategy}` +
        `（近1月胜率 ${best.rec_win == null ? "—" : (best.rec_win * 100).toFixed(0) + "%"} · ` +
        `${best.rec_trades ?? 0}笔 · 笔均 ${best.rec_avg1 == null ? "—" : fmtPct(best.rec_avg1)}）` +
        (best.is_best ? " ✓ 当前即最优" : ` ← 建议切到“${strategies[best.strategy]?.label || best.strategy}”对比`)
      : "";
    $("btStatus").textContent =
      `${data.symbol.name} · ${strategies[data.strategy]?.label || data.strategy}` +
      ` · ${data.dates[0]} ~ ${data.dates[data.dates.length - 1]}（${data.dates.length} 个交易日） · ` +
      boardTxt + (bestTxt ? " · " + bestTxt : "");
  } catch (e) {
    $("btStatus").textContent = "回测失败：" + e.message;
  } finally {
    $("btRun").disabled = false;
  }
}

function renderMetrics(m) {
  const cards = [
    ["总收益率", fmtPct(m.total_return), cls(m.total_return)],
    ["年化收益率 (CAGR)", fmtPct(m.cagr), cls(m.cagr)],
    ["最大回撤", fmtPct(-m.max_drawdown), "down"],
    ["夏普比率", m.sharpe == null || isNaN(m.sharpe) ? "—" : m.sharpe.toFixed(2), ""],
    ["交易次数", String(m.trades), ""],
    ["胜率", m.win_rate == null || isNaN(m.win_rate) ? "—" : (m.win_rate * 100).toFixed(1) + "%", ""],
    ["近1月胜率", m.m1 && m.m1.win == null ? "—" : ((m.m1.win * 100).toFixed(0) + "% · " + m.m1.trades + "笔") + (m.m1 && m.m1.avg != null ? " · 笔均" + fmtPct(m.m1.avg) : ""), "up"],
    ["近3月胜率", m.m3 && m.m3.win == null ? "—" : ((m.m3.win * 100).toFixed(0) + "% · " + m.m3.trades + "笔"), ""],
    ["盈亏比", m.profit_factor == null ? "—" : (m.profit_factor === Infinity ? "∞" : m.profit_factor.toFixed(2)), ""],
    ["仓位占比", (m.exposure * 100).toFixed(1) + "%", ""],
  ];
  $("metrics").innerHTML = cards
    .map(([k, v, c]) => `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`)
    .join("");
}

function renderTrades(trades) {
  $("tradeCount").textContent = `共 ${trades.length} 笔`;
  const days = (a, b) => {
    const da = new Date(a), db = new Date(b);
    return Math.max(Math.round((db - da) / 86400000), 1);
  };
  $("tradeBody").innerHTML = trades.length
    ? trades.map((t) => `<tr>
        <td>${esc(t.entry)}</td><td>${esc(t.exit)}</td>
        <td class="num">${days(t.entry, t.exit)}</td>
        <td class="num ${cls(t.ret)}">${fmtPct(t.ret)}</td></tr>`).join("")
    : '<tr><td colspan="4" class="muted" style="text-align:center">无交易</td></tr>';
}

async function runBacktestAll() {
  $("btAllRun").disabled = true;
  $("btAllStatus").textContent = "组合回测中（需要拉取各标的板块数据，请稍候）…";
  try {
    const data = await api("/api/backtest_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy: $("btStrategy").value,
        params: collectParams(),
        stop: $("btStop").value ? Number($("btStop").value) : null,
      }),
    });
    const s = data.summary || {};
    $("btAllSummary").innerHTML = [
      ["标的数", `${s.ok || 0}/${s.symbols || 0}`, ""],
      ["平均收益", fmtPct(s.avg_return), cls(s.avg_return)],
      ["平均最大回撤", fmtPct(-(s.avg_drawdown || 0)), "down"],
      ["平均夏普", s.avg_sharpe == null ? "—" : s.avg_sharpe.toFixed(2), ""],
      ["总交易次数", String(s.total_trades || 0), ""],
      ["全胜标的", String(s.win_all || 0), ""],
      ["近1月平均胜率", s.m1_avg_win == null ? "—" : (s.m1_avg_win * 100).toFixed(0) + "%", ""],
      ["近1月笔数", String(s.m1_trades || 0), ""],
    ].map(([k, v, c]) => `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
    $("btAllBody").innerHTML = data.table.map((r) => `
      <tr data-code="${esc(r.code)}">
        <td><b>${esc(r.name)}</b> <span class="muted">${esc(r.code)}</span></td>
        <td class="num ${cls(r.total_return)}">${r.total_return == null ? "—" : fmtPct(r.total_return)}</td>
        <td class="num">${r.cagr == null ? "—" : fmtPct(r.cagr)}</td>
        <td class="num down">${r.max_drawdown == null ? "—" : fmtPct(-r.max_drawdown)}</td>
        <td class="num">${r.sharpe == null ? "—" : (isNaN(r.sharpe) ? "—" : r.sharpe.toFixed(2))}</td>
        <td class="num">${r.trades == null ? "—" : r.trades}</td>
        <td class="num">${r.win_rate == null ? "—" : (r.win_rate * 100).toFixed(0) + "%"}</td>
        <td class="num">${r.m1 && r.m1.win != null ? (r.m1.win * 100).toFixed(0) + "% (" + r.m1.trades + "笔)" : "—"}</td>
        <td class="num">${r.exposure == null ? "—" : (r.exposure * 100).toFixed(0) + "%"}</td>
        <td class="num muted">${esc(r.board || "—")}</td>
      </tr>`).join("");
    $("btAllBody").querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        const code = tr.dataset.code;
        const opt = symbols.find((x) => x.code === code);
        if (opt) {
          $("btSymbol").value = code;
          runBacktest();
        }
      });
    });
    $("btAllStatus").textContent = "组合回测完成 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    $("btAllStatus").textContent = "组合回测失败：" + e.message;
  } finally {
    $("btAllRun").disabled = false;
  }
}

$("btStrategy").addEventListener("change", renderParams);
$("btRun").addEventListener("click", runBacktest);
$("btAllRun").addEventListener("click", runBacktestAll);
$("noteSave").addEventListener("click", async () => {
  const text = $("noteText").value.trim();
  if (!text) { $("noteMsg").textContent = "请先填写策略说明"; return; }
  try {
    await api("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    $("noteMsg").textContent = "已保存到 notes.md";
  } catch (e) {
    $("noteMsg").textContent = "保存失败：" + e.message;
  }
});

/* ---------- 信号监控 ---------- */
function sigParams() {
  return {};
}

let sigLog = [];
let sigLastDone = null;
async function refreshSignals() {
  $("sigStatus").textContent = "信号扫描中…";
  try {
    const codes = ($("sigCodes").value || SIGNAL_CODES.join(","))
      .split(",").map((s) => s.trim()).filter(Boolean);
    const sel = $("sigStrategySel");
    const data = await api("/api/signals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes, params: sigParams(), strategy: sel ? sel.value : "best_per_stock" }),
    });
    if (data.busy) {
      const since = sigLastDone
        ? Math.round((Date.now() - sigLastDone) / 1000)
        : 0;
      $("sigStatus").textContent = "上一轮扫描进行中（" + codes.length + " 只），请等几秒后自动重试…";
      if (since > 0) {
        setTimeout(() => { if ($("sigStatus").textContent.includes("进行中")) refreshSignals(); }, 8000);
      }
      return;
    }
    renderSignals(data.signals);
    $("sigStatus").textContent =
      "已更新 " + codes.length + " 只 · " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    sigLastDone = Date.now();
    signalsLoaded = true;
  } catch (e) {
    $("sigStatus").textContent = "信号扫描失败：" + e.message;
  }
}

function updateSigStrategyBadge() {
  const sel = $("sigStrategySel");
  const v = sel ? sel.value : "best_per_stock";
  $("sigStrategy").textContent = v === "best_per_stock"
    ? "监控策略：个股短期收益最优"
    : "监控策略：" + (strategies[v]?.label || v);
}

const sigSelEl = $("sigStrategySel");
if (sigSelEl) {
  sigSelEl.addEventListener("change", () => {
    updateSigStrategyBadge();
    refreshSignals();
  });
}

function renderSignals(signals) {
  const sigCard = (s) => {
    const badge = s.signal === 1 ? "买入" : (s.signal === 0 ? "卖出" : "观望");
    const bCls = s.signal === 1 ? "sig-buy" : (s.signal === 0 ? "sig-sell" : "sig-hold");
    const marketOpen = isTradingTime();
    const timeTag = s.date === todayStr()
      ? (marketOpen
          ? '<span class="badge sig-live">盘中实时信号</span>'
          : '<span class="badge sig-close">收盘信号 · 明日参考</span>')
      : `<span class="badge sig-close">${esc(s.date)} 信号</span>`;
    const stratName = strategies[s.strategy]?.label || (s.strategy ? s.strategy : "默认");
    const recWin = s.rec_win == null ? "—" : (s.rec_win * 100).toFixed(0) + "%";
    const prevBuy = s.signal === 0 && s.prev_buy;
    const prevBuyTag = prevBuy
      ? `<div class="sig-prev-buy">⚠ 此前有买入信号${s.prev_buy_date ? `（${esc(s.prev_buy_date)}）` : ""}，现在出现卖出 —— 重点留意</div>`
      : "";
    return `<div class="sig-card">
      <div class="sig-head">
        <b>${esc(s.name || s.code)}</b> <span class="muted">${esc(s.code)}</span>
        ${timeTag}
        <span class="badge ${bCls}">${badge}</span>
      </div>
      ${prevBuyTag}
      <div class="sig-reason muted">匹配策略：${esc(stratName)}</div>
      <div class="sig-row">
        <span>收盘 <b class="${cls(s.pct)}">${fmtPrice(s.close)}</b>（${fmtPctRaw(s.pct)}）</span>
        <span>筹码90 <b>${s.conc90 == null ? "—" : s.conc90.toFixed(1) + "%"}</b></span>
        <span>板块 ${esc(s.board || "—")} 净流入 <b class="${cls(s.board_net)}">${s.board_net == null ? "—" : (s.board_net / 1e8).toFixed(1) + "亿"}</b></span>
        <span>近1月胜率 <b class="up">${recWin}</b>（${s.rec_trades ?? 0}笔）</span>
        <span>近3月胜率 <b>${s.win3 == null ? "—" : (s.win3 * 100).toFixed(0) + "%"}</b></span>
        <span>综合分 <b class="up">${s.mon_score == null ? "—" : s.mon_score.toFixed(0)}</b></span>
        <span>投研 <b class="${s.rating === "回避" ? "down" : (s.rating === "强烈看好" ? "up" : "")}">${esc(s.rating || "中性")}</b></span>
      </div>
      <div class="sig-reason">${esc((s.reasons || []).join(" · "))}</div>
      ${s.error ? `<div class="sig-reason down">${esc(s.error)}</div>` : ""}
    </div>`;
  };
  const buys = signals.filter((s) => s.signal === 1);
  const sells = signals.filter((s) => s.signal === 0);
  const watches = signals.filter((s) => s.signal !== 1 && s.signal !== 0);
  $("sigBuyCards").innerHTML = buys.length
    ? buys.map(sigCard).join("")
    : '<div class="hint">当前无买入信号</div>';
  $("sigSellCards").innerHTML = sells.length
    ? sells.map(sigCard).join("")
    : '<div class="hint">当前无卖出信号</div>';
  $("sigBuyCount").textContent = `共 ${buys.length} 只`;
  $("sigSellCount").textContent = `共 ${sells.length} 只`;
  $("sigWatchInfo").textContent = `其余 ${watches.length} 只处于观望（未触发条件）`;
  const now = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const changed = signals.filter((s) => s.signal === 1 || s.signal === 0);
  if (changed.length) {
    sigLog.unshift({ time: now, items: changed.map((s) => `${s.name} ${s.signal_label} @${s.close}`) });
    if (sigLog.length > 50) sigLog.pop();
  }
  $("sigLogCount").textContent = `${sigLog.length} 条`;
  $("sigLog").innerHTML = sigLog.length
    ? sigLog.map((e) => `<div class="log-item"><span class="muted">${esc(e.time)}</span> ${esc(e.items.join("；"))}</div>`).join("")
    : '<div class="muted">暂无买卖信号记录</div>';
}

$("sigRun").addEventListener("click", refreshSignals);
setInterval(() => {
  if ($("sigAuto").checked && $("panel-signals").classList.contains("active")) refreshSignals();
}, 180000);

async function loadMarketBoard() {
  $("mkBStatus").textContent = "全市场信号计算中（1200 只本地回测，约 20~40 秒）…";
  try {
    const d = await api("/api/market_board?limit=100");
    const rows = [...(d.buys || []), ...(d.sells || [])];
    $("mkBTotal").textContent = String(d.total || 0);
    $("mkBBody").innerHTML = rows.length
      ? rows.map((s) => {
          const tag = s.signal === 1 ? "买入" : "卖出";
          const c = s.signal === 1 ? "up" : "down";
          return `<tr>
            <td><b>${esc(s.name)}</b> <span class="muted">${esc(s.code)}</span></td>
            <td class="${c}"><b>${tag}</b></td>
            <td>${esc(s.label || s.strategy)}</td>
            <td class="num">${s.rec_win == null ? "—" : (s.rec_win * 100).toFixed(0) + "%"}</td>
            <td class="num">${s.score == null ? "—" : Number(s.score).toFixed(1)}</td>
            <td class="num">${fmt(s.price)}</td>
            <td class="num ${cls(s.pct)}">${pctTxt(s.pct)}</td></tr>`;
        }).join("")
      : '<tr><td colspan="7" class="muted">暂无信号</td></tr>';
    $("mkBStatus").textContent = `榜单更新 ${esc(d.updated || "")}`;
  } catch (e) {
    $("mkBStatus").textContent = "榜单失败：" + e.message;
  }
}
$("mkBRun").addEventListener("click", loadMarketBoard);

/* ---------- 全市场分析 ---------- */
async function refreshMarketSignals() {
  $("mktStatus").textContent = "全市场扫描中…";
  try {
    const data = await api("/api/market_signals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 50 }),
    });
    if (data.error) {
      $("mktStatus").textContent = data.error;
      return;
    }
    const row = (s) => {
      const stratName = strategies[s.strategy]?.label || s.strategy || "—";
      const wr = s.rec_win == null ? "—" : (s.rec_win * 100).toFixed(0) + "%";
      const avg = s.rec_avg1 == null ? "—" : fmtPct(s.rec_avg1);
      return `<tr>
        <td><b>${esc(s.name || s.code)}</b> <span class="muted">${esc(s.code)}</span></td>
        <td class="muted">${esc(stratName)}</td>
        <td class="num">${fmtPrice(s.close)}</td>
        <td class="num ${cls(s.pct)}">${fmtPctRaw(s.pct)}</td>
        <td class="num up">${wr}</td>
        <td class="num ${cls(s.rec_avg1)}">${avg}</td>
      </tr>`;
    };
    $("mktBuyBody").innerHTML = (data.buys || []).length
      ? data.buys.map(row).join("")
      : '<tr><td colspan="6" class="muted" style="text-align:center">当前无全市场买入信号</td></tr>';
    $("mktSellBody").innerHTML = (data.sells || []).length
      ? data.sells.map(row).join("")
      : '<tr><td colspan="6" class="muted" style="text-align:center">当前无全市场卖出信号</td></tr>';
    $("mktBuyCount").textContent = `共 ${data.buys?.length || 0} 只`;
    $("mktSellCount").textContent = `共 ${data.sells?.length || 0} 只`;
    $("mktStatus").textContent = `已更新（分析标的 ${data.total || 0} 只）· ` +
      new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    $("mktStatus").textContent = "全市场扫描失败：" + e.message;
  }
}

$("mktRun").addEventListener("click", refreshMarketSignals);
setInterval(() => {
  if ($("mktAuto").checked && $("panel-market").classList.contains("active")) refreshMarketSignals();
}, 300000);

/* ---------- 模拟交易 ---------- */
function initPaper() {
  const opts = ['<option value="best_per_stock">个股最优（短期收益）</option>']
    .concat(Object.entries(strategies).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`))
    .join("");
  $("paperStrategy").innerHTML = opts;
  $("paperStrategy").value = "rsi_short";
}

async function loadPaper() {
  try {
    const st = await api("/api/paper?account=" + encodeURIComponent($("paperAccount").value));
    renderPaper(st);
  } catch (e) {
    $("paperStatus").textContent = "模拟盘加载失败：" + e.message;
  }
}

function renderPaper(st) {
  window.__paperState = st;
  // 同步控件为当前真实状态，避免默认值覆盖已保存配置
  if (st.capital != null) $("paperCapital").value = st.capital;
  if (st.max_positions != null) $("paperMaxPositions").value = st.max_positions;
  if (st.strategy && strategies[st.strategy]) $("paperStrategy").value = st.strategy;
  if (st.mode) $("paperMode").value = st.mode;
  if (st.force_codes && st.force_codes.length) $("paperForceCodes").value = st.force_codes.join(",");
  if (st.codes && st.codes.length) $("paperCodes").value = st.codes.join(",");
  const eq = st.equity_now || 0;
  $("paperCards").innerHTML = [
    ["状态", st.running ? "运行中" : "已停止", st.running ? "up" : "muted"],
    ["总资产", fmtAmt(eq), cls(st.return_total)],
    ["可用现金", fmtAmt(st.cash), ""],
    ["持仓市值", fmtAmt(st.pos_value), cls(st.pos_value - (st.positions ? 0 : 0))],
    ["累计收益", fmtPct(st.return_total), cls(st.return_total)],
    ["交易次数", String(st.trades_count || 0), ""],
    ["胜率", st.win_rate == null ? "—" : (st.win_rate * 100).toFixed(1) + "%", ""],
    ["最近撮合", st.last_tick || "—", ""],
  ].map(([k, v, c]) => `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
  const paperSel = st.selection || {};
  const rot = paperSel.rotation;
  const rotBox = $("paperRotation");
  if (rot) {
    rotBox.style.display = "";
    rotBox.innerHTML =
      `<div class="sig-prev-buy">🔄 动态换仓 ${esc(rot.time)}：卖出 <b>${esc(rot.sell_name)}</b>（综合分 ${rot.sell_score}）` +
      ` → 买入 <b>${esc(rot.buy_name)}</b>（综合分 ${rot.buy_score}）</div>`;
  } else {
    rotBox.style.display = "none";
  }
  const pos = st.positions || {};
  $("paperPosBody").innerHTML = Object.entries(pos).length
    ? Object.entries(pos).map(([code, p]) => {
        const cur = p.last_px || p.entry_px;
        const pnl = (cur / p.entry_px - 1) * 100;
        const stratName = strategies[p.strategy]?.label || p.strategy || "—";
        return `<tr>
          <td><b>${esc(p.name)}</b> <span class="muted">${esc(code)}</span></td>
          <td class="muted">${esc(stratName)}</td>
          <td class="num">${fmtPrice(p.entry_px)}</td>
          <td class="num ${cls(pnl)}">${fmtPrice(cur)}</td>
          <td class="num">${fmtAmt(p.shares * cur)}</td>
          <td class="num ${cls(pnl)}">${fmtPct(pnl / 100)}</td>
          <td class="num muted">${esc(p.entry_date)}</td>
        </tr>`;
      }).join("")
    : '<tr><td colspan="7" class="muted" style="text-align:center">空仓（等待超卖信号，不追高）</td></tr>';
  const trades = (st.trades || []).slice().reverse();
  $("paperTradeCount").textContent = `共 ${trades.length} 笔`;
  $("paperTradeBody").innerHTML = trades.length
    ? trades.map((t) => `<tr>
        <td>${esc(t.date)}</td><td>${esc(t.name)} (${esc(t.code)})</td>
        <td class="muted">${esc(strategies[t.strategy]?.label || t.strategy || "—")}</td>
        <td class="${t.side === "buy" ? "up" : "down"}">${t.side === "buy" ? "买入" : "卖出"}</td>
        <td class="num">${fmtPrice(t.price)}</td>
        <td class="num">${fmtAmt(t.amount)}</td>
        <td class="num ${cls(t.ret)}">${t.ret == null ? "—" : fmtPct(t.ret)}</td>
      </tr>`).join("")
    : '<tr><td colspan="7" class="muted" style="text-align:center">暂无成交</td></tr>';
  const closed = st.closed_trades || [];
  $("paperClosedCount").textContent = `共 ${closed.length} 笔`;
  $("paperClosedBody").innerHTML = closed.length
    ? closed.map((t) => `<tr>
        <td><b>${esc(t.name)}</b> <span class="muted">${esc(t.code)}</span></td>
        <td class="muted">${esc(strategies[t.strategy]?.label || t.strategy || "—")}</td>
        <td class="num muted">${esc(t.buy_date)}</td>
        <td class="num">${fmtPrice(t.buy_px)}</td>
        <td class="num muted">${esc(t.sell_date)}</td>
        <td class="num">${fmtPrice(t.sell_px)}</td>
        <td class="num">${t.hold_days ?? "—"}天</td>
        <td class="num ${cls(t.ret)}">${t.ret == null ? "—" : fmtPct(t.ret)}</td>
        <td class="muted">${esc(t.reason || "卖出信号")}</td>
      </tr>`).join("")
    : '<tr><td colspan="9" class="muted" style="text-align:center">暂无已平仓交易（卖出后自动显示买入→卖出配对）</td></tr>';
  drawPaperEquity($("paperCanvas"), st.equity || []);
  const sel = st.selection;
  const selTxt = sel && sel.codes && sel.codes.length
    ? "本轮入选：" + sel.codes.map((c) => {
        const w = c.win_rate == null ? "—" : (c.win_rate * 100).toFixed(0) + "%";
        return `${c.name}（${strategies[c.strategy]?.label || c.strategy} 胜率${w}）`;
      }).join("、")
    : "";
  $("paperStatus").textContent =
    `范围：${st.mode === "market" ? "全市场（非ST·50元内）" : "自选股"} · 策略：${strategies[st.strategy]?.label || st.strategy} · 初始资金 ${fmtAmt(st.capital)}` +
    (st.min_hold_days > 0 ? ` · 最短持有 ${st.min_hold_days} 天` : "") +
    (st.last_tick ? ` · 上次撮合 ${st.last_tick}` : "") +
    (selTxt ? " · " + selTxt : "");
}

async function startPaper() {
  $("paperStatus").textContent = "启动中…";
  try {
    const selStrategy = $("paperStrategy").value;
    const meta = strategies[selStrategy] || {};
    const params = {};
    for (const pk of Object.keys(meta.params || {})) params[pk] = meta.params[pk].default;
    const mode = $("paperMode").value;
    const maxPos = Number($("paperMaxPositions").value) || 8;
    const forceCodes = $("paperForceCodes").value.split(",").map((s) => s.trim()).filter(Boolean);
    const st = await api("/api/paper/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account: $("paperAccount").value,
        strategy: selStrategy,
        params,
        codes: mode === "market"
          ? []   // 全市场模式：服务端从全市场分析候选池自动取
          : $("paperCodes").value.split(",").map((s) => s.trim()).filter(Boolean),
        mode,
        max_positions: maxPos,
        force_codes: forceCodes,
        reset: false,   // 启动时保留现有持仓，只更新配置
      }),
    });
    renderPaper(st);
    $("paperStatus").textContent = "模拟盘已启动（保留持仓）";
  } catch (e) {
    $("paperStatus").textContent = "启动失败：" + e.message;
  }
}

async function stopPaper() {
  const st = await api("/api/paper/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account: $("paperAccount").value }),
  });
  renderPaper(st);
  $("paperStatus").textContent = "已停止（持仓保留，可随时恢复）";
}

async function resetPaper() {
  if (!confirm("确定重置模拟盘？所有持仓与记录将清空。")) return;
  const st = await api("/api/paper/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account: $("paperAccount").value }),
  });
  renderPaper(st);
  $("paperStatus").textContent = "已重置";
}

async function tickPaper() {
  $("paperStatus").textContent = "撮合中…";
  try {
    const st = await api("/api/paper/tick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: $("paperAccount").value }),
    });
    renderPaper(st);
    $("paperStatus").textContent = "撮合完成 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    $("paperStatus").textContent = "撮合失败：" + e.message;
  }
}

$("paperStart").addEventListener("click", startPaper);
$("paperStop").addEventListener("click", stopPaper);
$("paperReset").addEventListener("click", resetPaper);
$("paperTick").addEventListener("click", tickPaper);
$("paperAccount").addEventListener("change", loadPaper);
setInterval(() => {
  if ($("panel-paper").classList.contains("active")) loadPaper();
}, 5000);

/* ---------- 数据管理 ---------- */
async function loadData() {
  $("dataStatus").textContent = "加载中…";
  try {
    const data = await api("/api/data");
    $("dataBody").innerHTML = data.symbols.map((s) => `
      <tr>
        <td>${esc(s.code)}</td>
        <td><b>${esc(s.name)}</b></td>
        <td class="muted">${esc(s.file)}</td>
        <td class="num">${s.rows}</td>
        <td class="num muted">${esc(s.first || "—")}</td>
        <td class="num muted">${esc(s.last || "—")}</td>
        <td><button class="mini-btn" data-code="${esc(s.code)}" data-name="${esc(s.name)}">更新</button></td>
      </tr>`).join("");
    $("dataBody").querySelectorAll(".mini-btn").forEach((btn) => {
      btn.addEventListener("click", () => refreshSymbol(btn.dataset.code, btn.dataset.name, btn));
    });
    $("dataStatus").textContent = "共 " + data.symbols.length + " 个本地标的";
    dataLoaded = true;
  } catch (e) {
    $("dataStatus").textContent = "数据列表加载失败：" + e.message;
  }
}

async function refreshSymbol(code, name, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "更新中…"; }
  $("dataStatus").textContent = `正在更新 ${code} ${name || ""} …`;
  try {
    const r = await api("/api/data/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name }),
    });
    $("dataStatus").textContent = `${r.name} 已更新：${r.rows} 行，${r.first} ~ ${r.last}`;
    loadData();
  } catch (e) {
    $("dataStatus").textContent = "更新失败：" + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "更新"; }
  }
}

$("addSymbol").addEventListener("click", async () => {
  const code = $("addCode").value.trim();
  if (!code) { $("dataStatus").textContent = "请输入代码"; return; }
  const name = $("addName").value.trim() || undefined;
  await refreshSymbol(code, name);
  $("addCode").value = ""; $("addName").value = "";
});

/* ---------- 窗口自适应 ---------- */
window.addEventListener("resize", () => {
  drawLines($("trendCanvas"), trendCache[document.querySelector("#quoteBody tr")?.dataset.code] || { series: [] });
  if (lastBt) drawBacktest($("btCanvas"), lastBt);
  if (lastKline) drawKline(lastKline.canvas, lastKline.data.rows, lastKline.data.indicators);
  if (lastBoardDetail) drawBoardDetail(lastBoardDetail.canvas, lastBoardDetail.detail.kline, lastBoardDetail.detail.fflow);
  const paperState = window.__paperState;
  if (paperState) drawPaperEquity($("paperCanvas"), paperState.equity || []);
});

/* ---------- 启动 ---------- */
tickClock();
setInterval(tickClock, 1000);
refreshQuotes();
initBacktest();
setTimeout(loadPaper, 300);
