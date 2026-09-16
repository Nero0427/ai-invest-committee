/* Dashboard pages: the market heat map and the billboard.
 *
 * Loaded after app.js, which owns the router and the shared helpers
 * ($, esc, fmtPct, pctClass, fmtNum, openSymbol).
 */

/* ------------------------------------------------------------- helpers -- */

function fmtMoney(value) {
  if (value === null || value === undefined || isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 1e12) return (value / 1e12).toFixed(2) + ' 万亿';
  if (abs >= 1e8) return (value / 1e8).toFixed(2) + ' 亿';
  if (abs >= 1e4) return (value / 1e4).toFixed(2) + ' 万';
  return value.toFixed(2);
}

function fmtSigned(value) {
  if (value === null || value === undefined || isNaN(value)) return '—';
  return (value > 0 ? '+' : '') + fmtMoney(value);
}

function fmtPctSigned(value) {
  if (value === null || value === undefined || isNaN(value)) return '—';
  return (value > 0 ? '+' : '') + Number(value).toFixed(2) + '%';
}

/* Red for up, green for down - the domestic convention. A missing change has
   no business being tinted at all, so it stays neutral grey. */
function pctColor(pct, base) {
  const neutral = base || [42, 46, 58];
  if (pct === null || pct === undefined || isNaN(pct)) return rgb(neutral);
  const t = Math.min(1, Math.abs(pct) / 10);
  const target = pct >= 0 ? [214, 62, 62] : [26, 158, 106];
  const eased = Math.pow(t, 0.62);
  return rgb([
    Math.round(neutral[0] + (target[0] - neutral[0]) * eased),
    Math.round(neutral[1] + (target[1] - neutral[1]) * eased),
    Math.round(neutral[2] + (target[2] - neutral[2]) * eased),
  ]);
}

function rgb(triple) {
  return 'rgb(' + triple[0] + ',' + triple[1] + ',' + triple[2] + ')';
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* A page load is a handful of requests fired at once; on a cold server any
   one of them can lose the race. Two quiet retries turn that into a non-event
   instead of a red banner where data should be. */
async function getJSON(url, options, attempt) {
  const tries = attempt || 0;
  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    if (tries < 2) { await sleep(300 * (tries + 1)); return getJSON(url, options, tries + 1); }
    throw new Error('请求失败，请确认服务仍在运行');
  }
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || '请求失败');
  return data;
}

/* ------------------------------------------------------------- treemap -- */

/* Squarified treemap (Bruls, Huizing & van Wijk). Kept here rather than
   pulled from a library because the project ships zero dependencies. */
function worstRatio(areas, sum, side) {
  let max = 0;
  let min = Infinity;
  for (let i = 0; i < areas.length; i++) {
    if (areas[i] > max) max = areas[i];
    if (areas[i] < min) min = areas[i];
  }
  if (min <= 0) return Infinity;
  const s2 = sum * sum;
  const side2 = side * side;
  return Math.max((side2 * max) / s2, s2 / (side2 * min));
}

function squarify(children, x, y, w, h) {
  const out = [];
  const total = children.reduce((sum, c) => sum + c.value, 0);
  if (!(total > 0) || w <= 0 || h <= 0) return out;

  const scale = (w * h) / total;
  const queue = children.map((c) => ({ item: c, area: c.value * scale }));
  let cx = x;
  let cy = y;
  let cw = w;
  let ch = h;

  while (queue.length) {
    const side = Math.min(cw, ch);
    const row = [queue.shift()];
    let rowSum = row[0].area;
    let cur = worstRatio([row[0].area], rowSum, side);

    while (queue.length) {
      const next = row.concat([queue[0]]);
      const nextSum = rowSum + queue[0].area;
      const ratio = worstRatio(next.map((r) => r.area), nextSum, side);
      if (ratio > cur) break;
      row.push(queue.shift());
      rowSum = nextSum;
      cur = ratio;
    }

    if (cw >= ch) {
      const colW = Math.min(cw, rowSum / ch);
      let oy = cy;
      for (let i = 0; i < row.length; i++) {
        const cellH = row[i].area / colW;
        out.push({ item: row[i].item, x: cx, y: oy, w: colW, h: cellH });
        oy += cellH;
      }
      cx += colW;
      cw -= colW;
    } else {
      const rowH = Math.min(ch, rowSum / cw);
      let ox = cx;
      for (let i = 0; i < row.length; i++) {
        const cellW = row[i].area / rowH;
        out.push({ item: row[i].item, x: ox, y: cy, w: cellW, h: rowH });
        ox += cellW;
      }
      cy += rowH;
      ch -= rowH;
    }
  }
  return out;
}

/* Laid out in real pixels rather than a fixed viewBox, so labels keep their
   proportions however the panel is sized. */
let TM_W = 1000;
let TM_H = 620;
let tmTimer = null;
let tmAuto = true;
let tmMarket = 'cn';
const tmData = { cn: null, us: null };

function drawTreemap() {
  const data = tmData[tmMarket];
  if (!data) return;
  const svg = $('treemap');
  const box = svg.parentElement.getBoundingClientRect();
  TM_W = Math.max(640, Math.round(box.width - 2));
  TM_H = Math.max(320, Math.round(box.height - 2));
  svg.setAttribute('viewBox', '0 0 ' + TM_W + ' ' + TM_H);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  renderTreemap(data);
}

async function loadTreemap(manual) {
  const svg = $('treemap');
  if (manual) svg.classList.add('loading');
  try {
    const payload = await getJSON('/api/treemap?market=' + tmMarket
      + (tmMarket === 'cn' ? '&limit=1500' : ''));
    tmData[tmMarket] = payload.data;
    drawTreemap();
    const d = payload.data;
    $('tmMeta').textContent = tmMarket === 'us'
      ? (d.shown + ' 个板块 · ' + d.updated + ' 更新')
      : ('共 ' + d.total + ' 只，图中显示市值最大的 ' + d.shown
         + ' 只（覆盖 ' + d.covered + '% 总市值）· ' + d.updated + ' 更新');
  } catch (err) {
    $('tmMeta').textContent = '加载失败：' + err.message;
  } finally {
    svg.classList.remove('loading');
  }
}

function renderTreemap(data) {
  indexTreemap(data);
  const groups = data.groups.map((g) => ({ value: g.v, group: g }))
    .filter((g) => g.value > 0);
  const cells = squarify(groups, 0, 0, TM_W, TM_H);
  const parts = [];

  cells.forEach((cell) => {
    const group = cell.item.group;
    // Group headers scale with the block so a wide sector is not labelled in
    // the same 9px as a sliver.
    const headerH = Math.min(24, Math.max(13, cell.h * 0.11));
    parts.push('<rect class="tm-ghead" x="' + cell.x + '" y="' + cell.y
      + '" width="' + cell.w + '" height="' + headerH
      + '" fill="' + pctColor(group.p, [28, 31, 41]) + '"/>');

    if (cell.w > 46) {
      const gfs = Math.max(9, Math.min(13, Math.round(cell.w / 22)));
      parts.push('<text class="tm-glabel" style="font-size:' + gfs + 'px" x="'
        + (cell.x + 5) + '" y="' + (cell.y + headerH - 5) + '">'
        + esc(group.g) + '</text>');
      parts.push('<text class="tm-gpct" style="font-size:' + gfs + 'px" x="'
        + (cell.x + cell.w - 5) + '" y="' + (cell.y + headerH - 5)
        + '" text-anchor="end">' + fmtPctSigned(group.p) + '</text>');
    }

    const innerY = cell.y + headerH;
    const innerH = cell.h - headerH;
    if (innerH <= 1 || cell.w <= 1) return;

    const stocks = group.s.map((s) => ({ value: s.v, stock: s }))
      .filter((s) => s.value > 0);
    const inner = squarify(stocks, cell.x, innerY, cell.w, innerH);

    inner.forEach((sc) => {
      const st = sc.item.stock;
      parts.push('<rect class="tm-cell" data-code="' + esc(st.c) + '" x="' + sc.x
        + '" y="' + sc.y + '" width="' + Math.max(0, sc.w - 1)
        + '" height="' + Math.max(0, sc.h - 1) + '" fill="' + pctColor(st.p) + '"/>');

      // Font grows with the block, and the label only appears once there is
      // genuinely room for it - a name clipped to three characters helps
      // nobody.
      const fs = Math.max(8, Math.min(15, Math.sqrt(sc.w * sc.h) / 7));
      const room = fs * 0.95;
      if (sc.w > room * 3.4 && sc.h > fs * 2.1) {
        const cx = sc.x + sc.w / 2;
        const showPct = sc.h > fs * 3.4 && sc.w > room * 4.6;
        const baseY = sc.y + sc.h / 2 + (showPct ? -1 : fs * 0.35);
        parts.push('<text class="tm-name" style="font-size:' + fs + 'px" x="' + cx
          + '" y="' + baseY + '" text-anchor="middle">'
          + esc(fitName(st.n, sc.w, fs)) + '</text>');
        if (showPct) {
          parts.push('<text class="tm-pct" style="font-size:' + (fs * 0.92) + 'px" x="'
            + cx + '" y="' + (baseY + fs * 1.5) + '" text-anchor="middle">'
            + fmtPctSigned(st.p) + '</text>');
        }
      }
    });
  });

  $('treemap').innerHTML = parts.join('');
}

/* Roughly how many CJK characters fit in `width` at `fs` pixels. */
function fitName(name, width, fs) {
  if (!name) return '';
  const limit = Math.floor(width / (fs * 1.05));
  if (limit < 2) return '';
  return name.length > limit ? name.slice(0, limit) : name;
}

function indexTreemap(data) {
  const index = {};
  data.groups.forEach((g) => {
    g.s.forEach((s) => {
      index[s.c] = { n: s.n, p: s.p, v: s.v, group: g.g };
    });
  });
  window.__tmIndex = index;
}

/* -------------------------------------------------------- hover tooltip -- */

const tipCache = new Map();
let tipSeq = 0;

async function showTip(cell, event) {
  const code = cell.getAttribute('data-code');
  const meta = (window.__tmIndex || {})[code];
  if (!meta) return;

  const tip = $('tmTip');
  tip.hidden = false;
  $('tipName').textContent = meta.n;
  $('tipCode').textContent = code + (meta.group ? ' · ' + meta.group : '');
  $('tipRow').innerHTML = '<span class="' + pctClass(meta.p) + '">'
    + fmtPctSigned(meta.p) + '</span>'
    + (tmMarket === 'us' ? '' : '　市值 ' + fmtMoney(meta.v));

  positionTip(tip, event);

  // The sparkline is the whole point of hovering, and it arrives a beat later
  // than the label - stale responses are dropped by sequence number.
  const seq = ++tipSeq;
  let snap = tipCache.get(code);
  if (!snap) {
    try {
      const payload = await getJSON('/api/quote?symbol=' + encodeURIComponent(code));
      snap = payload.data;
      tipCache.set(code, snap);
      if (tipCache.size > 60) tipCache.delete(tipCache.keys().next().value);
    } catch (err) {
      snap = null;
    }
  }
  if (seq !== tipSeq || tip.hidden) return;
  drawTipSpark(snap ? snap.spark : null, snap ? snap.quote : null);
}

function positionTip(tip, event) {
  const wrap = $('treemap').parentElement.getBoundingClientRect();
  let left = event.clientX - wrap.left + 16;
  let top = event.clientY - wrap.top + 16;
  if (left + 250 > wrap.width) left = wrap.width - 255;
  if (top + 170 > wrap.height) top = wrap.height - 175;
  tip.style.left = Math.max(4, left) + 'px';
  tip.style.top = Math.max(4, top) + 'px';
}

function drawTipSpark(values, quote) {
  const svg = $('tipSpark');
  if (!values || values.length < 2) {
    svg.innerHTML = '<text x="4" y="30" class="tip-empty">暂无走势数据</text>';
    return;
  }
  const w = 220;
  const h = 54;
  const pad = 6;
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const span = (max - min) || 1;
  const step = (w - pad * 2) / (values.length - 1);
  const pts = values.map((v, i) => [
    pad + i * step,
    h - pad - ((v - min) / span) * (h - pad * 2),
  ]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = line + ' L' + pts[pts.length - 1][0].toFixed(1) + ' ' + (h - pad)
    + ' L' + pts[0][0].toFixed(1) + ' ' + (h - pad) + ' Z';
  const pct = quote ? quote.change_pct : null;
  const stroke = (pct === null || pct === undefined || pct >= 0) ? '#d63e3e' : '#1a9e6a';
  svg.innerHTML = '<path d="' + area + '" fill="' + stroke + '" opacity="0.16"/>'
    + '<path d="' + line + '" fill="none" stroke="' + stroke + '" stroke-width="1.6"/>';
}

function bindTreemap() {
  const svg = $('treemap');

  svg.addEventListener('mousemove', (e) => {
    const cell = e.target.closest('.tm-cell');
    if (!cell) { $('tmTip').hidden = true; return; }
    showTip(cell, e);
  });
  svg.addEventListener('mouseleave', () => { $('tmTip').hidden = true; });
  svg.addEventListener('click', (e) => {
    const cell = e.target.closest('.tm-cell');
    if (cell) openSymbol(cell.getAttribute('data-code'));
  });

  $('tmRefresh').addEventListener('click', () => loadTreemap(true));
  $('tmAuto').addEventListener('click', () => {
    tmAuto = !tmAuto;
    $('tmAuto').classList.toggle('live', tmAuto);
    $('tmAuto').textContent = tmAuto ? '自动刷新 30s' : '自动刷新已关闭';
    scheduleTreemap();
  });

  $('tmMarket').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    tmMarket = btn.getAttribute('data-mkt');
    $('tmMarket').querySelectorAll('.seg-btn').forEach((b) => {
      b.classList.toggle('active', b === btn);
    });
    $('tmHint').textContent = tmMarket === 'us'
      ? '悬停看走势，点击进入投委会分析（美股按行业 ETF 划分）'
      : '悬停看走势，点击进投委会';
    if (!tmData[tmMarket]) loadTreemap(true);
    else drawTreemap();
  });

  let resizeTimer = null;
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawTreemap, 140);
    }).observe(svg.parentElement);
  }
}

function scheduleTreemap() {
  if (tmTimer) { clearInterval(tmTimer); tmTimer = null; }
  if (!tmAuto) return;
  tmTimer = setInterval(() => {
    if (currentPage === 'treemap' && !document.hidden) loadTreemap(false);
  }, 30000);
}

/* ----------------------------------------------------------- billboard -- */

let bbRows = [];
let bbDate = '';
let bbSort = { key: 'net_amt', dir: -1 };
const bbSeats = new Map();

const BB_COLUMNS = [
  { key: 'code', label: '代码', type: 'text' },
  { key: 'name', label: '名称', type: 'text' },
  { key: 'price', label: '收盘', type: 'num' },
  { key: 'change_pct', label: '涨跌幅', type: 'num', color: true },
  { key: 'net_amt', label: '净买入', type: 'money', color: true },
  { key: 'buy_amt', label: '买入额', type: 'money' },
  { key: 'sell_amt', label: '卖出额', type: 'money' },
  { key: 'amount', label: '成交额', type: 'money' },
  { key: 'turnover', label: '换手率', type: 'pct' },
  { key: 'reason', label: '上榜原因', type: 'text' },
  { key: 'after_1d', label: '次日', type: 'num', color: true },
  { key: 'after_5d', label: '5日', type: 'num', color: true },
];

async function loadBillboard(force) {
  try {
    const payload = await getJSON('/api/billboard' + (force ? '?force=1' : ''));
    bbRows = payload.data.items || [];
    bbDate = payload.data.date;
    $('bbDate').textContent = bbDate;
    renderBillboard();
    renderBillboardInline();
  } catch (err) {
    $('bbTable').innerHTML = '<p class="empty">加载失败：' + esc(err.message) + '</p>';
    $('tmBbBody').innerHTML = '<p class="empty">加载失败：' + esc(err.message) + '</p>';
  }
}

function bbValue(row, key) {
  const v = row[key];
  return v === null || v === undefined ? (typeof v === 'string' ? '' : null) : v;
}

function sortRows(rows) {
  const { key, dir } = bbSort;
  const isText = ['code', 'name', 'reason'].indexOf(key) >= 0;
  return rows.slice().sort((a, b) => {
    if (isText) {
      return String(a[key] || '').localeCompare(String(b[key] || ''), 'zh') * dir;
    }
    const av = bbValue(a, key);
    const bv = bbValue(b, key);
    if (av === null && bv === null) return 0;
    if (av === null) return 1;
    if (bv === null) return -1;
    return (av - bv) * dir;
  });
}

function renderBillboard() {
  const rows = sortRows(bbRows);
  if (!rows.length) {
    $('bbTable').innerHTML = '<p class="empty">当日暂无龙虎榜数据。</p>';
    return;
  }

  let html = '<table class="data-table bb-table"><thead><tr>';
  BB_COLUMNS.forEach((col) => {
    const active = bbSort.key === col.key;
    const arrow = active ? (bbSort.dir > 0 ? ' ▲' : ' ▼') : '';
    html += '<th class="sortable' + (col.type !== 'text' ? ' num' : '')
      + (active ? ' sorted' : '') + '" data-key="' + col.key + '">'
      + esc(col.label) + arrow + '</th>';
  });
  html += '</tr></thead><tbody>';

  rows.forEach((r) => {
    html += '<tr class="bb-row clickable" data-code="' + esc(r.code)
      + '" data-expanded="0">'
      + '<td class="mono">' + esc(r.code) + '<span class="expander">▸</span></td>'
      + '<td>' + esc(r.name) + '</td>'
      + '<td class="num">' + fmtNum(r.price) + '</td>'
      + '<td class="num ' + pctClass(r.change_pct) + '">' + fmtPctSigned(r.change_pct) + '</td>'
      + '<td class="num ' + pctClass(r.net_amt) + '">' + fmtSigned(r.net_amt) + '</td>'
      + '<td class="num">' + fmtMoney(r.buy_amt) + '</td>'
      + '<td class="num">' + fmtMoney(r.sell_amt) + '</td>'
      + '<td class="num">' + fmtMoney(r.amount) + '</td>'
      + '<td class="num">' + fmtNum(r.turnover, 1) + '%</td>'
      + '<td class="reason">' + esc(r.reason || '—') + '</td>'
      + '<td class="num ' + pctClass(r.after_1d) + '">' + fmtPctSigned(r.after_1d) + '</td>'
      + '<td class="num ' + pctClass(r.after_5d) + '">' + fmtPctSigned(r.after_5d) + '</td>'
      + '</tr>';
  });
  $('bbTable').innerHTML = html + '</tbody></table>';
}

async function toggleSeats(row) {
  const code = row.getAttribute('data-code');
  const expanded = row.getAttribute('data-expanded') === '1';
  const next = row.nextElementSibling;
  if (next && next.classList.contains('seat-tr')) {
    next.remove();
    row.setAttribute('data-expanded', '0');
    if (expanded) return;
  }
  if (expanded) { row.setAttribute('data-expanded', '0'); return; }

  const holder = document.createElement('tr');
  holder.className = 'seat-tr';
  const cell = document.createElement('td');
  cell.colSpan = BB_COLUMNS.length;
  cell.innerHTML = '<div class="seat-box">正在拉取席位…</div>';
  holder.appendChild(cell);
  row.after(holder);
  row.setAttribute('data-expanded', '1');

  try {
    let seats = bbSeats.get(code);
    if (!seats) {
      const payload = await getJSON('/api/billboard/seats?code=' + encodeURIComponent(code)
        + '&date=' + encodeURIComponent(bbDate));
      seats = payload.data;
      bbSeats.set(code, seats);
    }
    cell.innerHTML = renderSeats(seats);
  } catch (err) {
    cell.innerHTML = '<div class="seat-box">席位加载失败：' + esc(err.message) + '</div>';
  }
}

function renderSeats(seats) {
  const side = (title, rows, cls) => {
    if (!rows || !rows.length) {
      return '<div class="seat-col"><h4 class="' + cls + '">' + title + '</h4>'
        + '<p class="empty small">无数据</p></div>';
    }
    return '<div class="seat-col"><h4 class="' + cls + '">' + title + '</h4>'
      + '<table class="seat-table"><thead><tr><th>营业部</th>'
      + '<th class="num">买入</th><th class="num">卖出</th><th class="num">净额</th>'
      + '</tr></thead><tbody>'
      + rows.map((s) => '<tr><td class="seat-name">' + esc(s.name) + '</td>'
        + '<td class="num">' + fmtMoney(s.buy) + '</td>'
        + '<td class="num">' + fmtMoney(s.sell) + '</td>'
        + '<td class="num ' + pctClass(s.net) + '">' + fmtSigned(s.net) + '</td></tr>').join('')
      + '</tbody></table></div>';
  };
  return '<div class="seat-box"><div class="seat-grid">'
    + side('买入席位', seats.buy, 'seat-buy')
    + side('卖出席位', seats.sell, 'seat-sell')
    + '</div></div>';
}

function renderBillboardInline() {
  const body = $('tmBbBody');
  const rows = bbRows.slice().sort((a, b) => (b.net_amt || 0) - (a.net_amt || 0)).slice(0, 12);
  if (!rows.length) { body.innerHTML = '<p class="empty">暂无数据。</p>'; return; }
  $('tmBbDate').textContent = bbDate;

  let html = '<div class="mini-list">';
  rows.forEach((r) => {
    html += '<div class="mini-row clickable" data-code="' + esc(r.code) + '">'
      + '<span class="mini-name">' + esc(r.name) + '</span>'
      + '<span class="mini-code">' + esc(r.code) + '</span>'
      + '<span class="mini-pct ' + pctClass(r.change_pct) + '">' + fmtPctSigned(r.change_pct) + '</span>'
      + '<span class="mini-amt ' + pctClass(r.net_amt) + '">净买 ' + fmtSigned(r.net_amt) + '</span>'
      + '</div>';
  });
  html += '</div><div class="mini-foot">点击查看完整榜单与席位明细</div>';
  body.innerHTML = html;
  body.querySelector('.mini-foot').addEventListener('click', () => showPage('billboard'));
}

/* ----------------------------------------------------------------- wire -- */

const BOUND = {};
function once(key, fn) {
  if (BOUND[key]) return;
  BOUND[key] = true;
  fn();
}

window.Pages = {
  onEnter(name) {
    if (name === 'treemap') {
      once('tm', bindTreemap);
      loadTreemap(false);
      loadBillboard(false);
      loadMarketStrip();
      scheduleTreemap();
    } else if (tmTimer) {
      clearInterval(tmTimer);
      tmTimer = null;
    }
    if (name === 'billboard') {
      once('bb', () => {
        $('bbRefresh').addEventListener('click', () => loadBillboard(true));
        $('bbTable').addEventListener('click', (e) => {
          const th = e.target.closest('th.sortable');
          if (th) {
            const key = th.getAttribute('data-key');
            if (bbSort.key === key) bbSort.dir = -bbSort.dir;
            else bbSort = { key: key, dir: key === 'code' || key === 'name' ? 1 : -1 };
            renderBillboard();
            return;
          }
          const row = e.target.closest('tr.bb-row');
          if (row) toggleSeats(row);
        });
      });
      loadBillboard(false);
    }
  },
};

async function loadMarketStrip() {
  try {
    const payload = await getJSON('/api/market');
    const s = payload.data.stats;
    $('mkStrip').innerHTML = ''
      + strip('上涨', s.up, 'up') + strip('下跌', s.down, 'down')
      + strip('平盘', s.flat, '') + strip('涨停', s.limit_up, 'up')
      + strip('跌停', s.limit_down, 'down') + strip('成交额', fmtMoney(s.amount), '')
      + strip('总数', s.total, '');
  } catch (err) {
    $('mkStrip').innerHTML = '<span class="ms-err">市场数据加载失败：' + esc(err.message) + '</span>';
  }
}

function strip(label, value, cls) {
  return '<div class="ms-item"><span class="ms-k">' + esc(label) + '</span>'
    + '<span class="ms-v ' + cls + '">' + esc(value) + '</span></div>';
}

/* Clicking a billboard row jumps into the committee with that symbol. */
document.addEventListener('click', (e) => {
  const row = e.target.closest('.mini-row.clickable');
  if (row) {
    const code = row.getAttribute('data-code');
    if (code) openSymbol(code);
  }
});
