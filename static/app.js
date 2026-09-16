/* AI 投委会 - 前端逻辑 */

const $ = (id) => document.getElementById(id);

const AVATAR_COLOR = {
  fundamental: '#5B8FF9',
  technical: '#61DDAA',
  news: '#F6BD16',
  sentiment: '#7262FD',
  bull: '#F2609B',
  bear: '#35D08A',
  risk: '#7AA2F7',
  trader: '#C58B2B',
};

const STAGES = ['collect', 'analyze', 'debate', 'risk', 'human'];

const state = {
  symbol: 'LITE',
  target: null,
  running: false,
  source: null,
  members: [],
  seenSpeeches: 0,
  finished: false,
  rating: '',
};

/* ------------------------------------------------------------- helpers -- */

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

function fmtPct(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
}

function fmtCap(v) {
  if (!v) return '—';
  const n = Number(v);
  if (n >= 1e12) return (n / 1e12).toFixed(2) + ' 万亿';
  if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  return n.toFixed(0);
}

function pctClass(v) {
  if (v === null || v === undefined || isNaN(v)) return 'flat';
  return Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : 'flat');
}

function stanceClass(stance) {
  if (!stance) return 'stance-中';
  if (stance.indexOf('多') >= 0) return 'stance-多';
  if (stance.indexOf('空') >= 0) return 'stance-空';
  return 'stance-中';
}

function barClass(stance) {
  if (!stance) return 'bar-中';
  if (stance.indexOf('多') >= 0) return 'bar-多';
  if (stance.indexOf('空') >= 0) return 'bar-空';
  return 'bar-中';
}

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function initial(name) {
  return name ? name.charAt(0) : '?';
}

function avatarStyle(id) {
  const c = AVATAR_COLOR[id] || '#5B8FF9';
  return 'background:' + c;
}

/* -------------------------------------------------------------- quote --- */

function renderQuote(snap) {
  const q = snap.quote || {};
  const ind = snap.indicators || {};
  const target = snap.target || {};
  const breadth = snap.breadth || {};
  const members = snap.members || [];
  const mf = snap.moneyflow || {};
  const isSector = String(target.kind || '').indexOf('sector') === 0;

  state.symbol = q.code || state.symbol;
  state.target = target;

  $('tkName').textContent = q.name || q.code || '—';
  $('tkCode').textContent = isSector
    ? (target.board === 'concept' ? '概念板块'
      : (target.board === 'etf' ? '行业ETF' : '行业板块'))
    : (q.code || '');

  const mk = $('tkMarket');
  mk.hidden = false;
  mk.textContent = target.kind === 'sector_cn' ? 'A股板块'
    : (target.kind === 'sector_us' ? '美股板块' : (q.kind === 'A' ? 'A股' : '美股'));

  $('priceVal').textContent = fmtNum(q.price);
  $('priceCur').textContent = q.currency || '';
  const cls = pctClass(q.change_pct);
  const arrow = cls === 'up' ? '▲' : (cls === 'down' ? '▼' : '');
  $('priceChg').innerHTML =
    '<span class="' + cls + '">' + arrow + ' ' + fmtNum(q.change) +
    '  ' + fmtPct(q.change_pct) + '</span>';

  let metrics;
  if (isSector) {
    const hasBreadth = !!breadth.total;
    const flow = (mf.main === null || mf.main === undefined) ? '—'
      : ((mf.main > 0 ? '净流入 ' : '净流出 ') + fmtCap(Math.abs(mf.main)));
    metrics = [
      ['上涨家数', hasBreadth ? String(breadth.up) : '—'],
      ['下跌家数', hasBreadth ? String(breadth.down) : '—'],
      ['涨停家数', hasBreadth ? String(breadth.limit_up) : '—'],
      ['上涨占比', (hasBreadth && breadth.ratio !== null && breadth.ratio !== undefined)
        ? (breadth.ratio * 100).toFixed(1) + '%' : '—'],
      ['主力资金', flow],
      ['领涨股', members.length ? (members[0].name || '—') : '—'],
    ];
  } else {
    metrics = [
      ['最高', fmtNum(q.high)],
      ['最低', fmtNum(q.low)],
      ['总市值', fmtCap(q.market_cap)],
      ['PE(TTM)', fmtNum(q.pe)],
      ['PB', fmtNum(q.pb)],
      ['RSI14', fmtNum(ind.rsi14, 1)],
    ];
  }
  $('quoteMetrics').innerHTML = metrics.map(([k, v]) =>
    '<div><span class="k">' + k + '</span><span class="v">' + esc(v) + '</span></div>').join('');

  const notice = $('notice');
  let noticeText = '';
  if (snap.kline_source === 'synthetic') {
    noticeText = '技术面数据说明：该板块无法直接取得指数历史，K线由市值最大的成分股加权合成。' +
      '趋势形态和当前点位可靠，但历史绝对点位与官方指数有偏差，均线与MACD仅供参考。';
  } else if (snap.kline_error) {
    noticeText = '技术面数据受限：' + snap.kline_error +
      ' 本次分析将基于资金流向与内部结构完成，不含均线与MACD判断。';
  }
  if (noticeText) {
    notice.hidden = false;
    notice.textContent = noticeText;
  } else {
    notice.hidden = true;
  }

  renderSpark(snap.spark, q.change_pct);
}

function renderSpark(closes, changePct) {
  const svg = $('spark');
  if (!closes || closes.length < 2) { svg.innerHTML = ''; return; }

  const w = 260, h = 60, pad = 5;
  const min = Math.min.apply(null, closes);
  const max = Math.max.apply(null, closes);
  const range = (max - min) || 1;
  const step = (w - pad * 2) / (closes.length - 1);

  const pts = closes.map((c, i) => [
    pad + i * step,
    h - pad - ((c - min) / range) * (h - pad * 2),
  ]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = line + ' L ' + pts[pts.length - 1][0].toFixed(1) + ' ' + h +
               ' L ' + pts[0][0].toFixed(1) + ' ' + h + ' Z';
  const color = Number(changePct) >= 0 ? '#FF5C5C' : '#35D08A';

  svg.innerHTML =
    '<path d="' + area + '" fill="' + color + '" opacity="0.10"></path>' +
    '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.5" ' +
    'stroke-linejoin="round" stroke-linecap="round"></path>';
}

/* ------------------------------------------------------------- members -- */

function renderMembers(members) {
  state.members = members || [];
  const box = $('memberList');
  box.innerHTML = members.map((m) => (
    '<div class="member" id="member-' + m.id + '">' +
      '<div class="member-avatar" style="' + avatarStyle(m.id) + '">' +
        esc(initial(m.name)) + '</div>' +
      '<div class="member-main">' +
        '<div class="member-name">' + esc(m.name) +
          '<span class="member-badge" data-role="stance">待发言</span></div>' +
        '<div class="member-expertise">' + esc(m.expertise || '') + '</div>' +
        '<div class="member-meter">' +
          '<div class="member-bar"><span data-role="bar" style="width:0%"></span></div>' +
          '<span class="member-conf" data-role="conf">—</span>' +
        '</div>' +
      '</div>' +
      '<div class="member-status" data-role="dot"></div>' +
    '</div>'
  )).join('');
  $('memberCount').textContent = '0/' + members.length;
}

function markMember(id, status) {
  const node = $('member-' + id);
  if (!node) return;
  node.classList.toggle('active', status === 'active');
  const dot = node.querySelector('[data-role="dot"]');
  if (!dot) return;
  if (status === 'active') dot.style.background = '#F2609B';
  else if (status === 'done') dot.style.background = '#35D08A';
  else dot.style.background = '';
}

function updateMemberResult(id, data) {
  const node = $('member-' + id);
  if (!node) return;
  const stance = data && (data.stance || data.rating);
  const raw = data && (data.confidence !== undefined ? data.confidence : data.consensus);
  const conf = (raw === undefined || raw === null) ? null : Math.round(Number(raw));

  const badge = node.querySelector('[data-role="stance"]');
  if (badge && stance) {
    badge.textContent = stance;
    badge.className = 'member-badge ' + stanceClass(stance);
  }
  const bar = node.querySelector('[data-role="bar"]');
  if (bar) {
    bar.style.width = (conf === null ? 0 : Math.max(0, Math.min(100, conf))) + '%';
    bar.className = barClass(stance);
  }
  const confEl = node.querySelector('[data-role="conf"]');
  if (confEl) confEl.textContent = conf === null ? '—' : conf + '%';

  const done = state.members.filter((m) => {
    const n = $('member-' + m.id);
    return n && n.querySelector('[data-role="stance"]') &&
           n.querySelector('[data-role="stance"]').textContent !== '待发言';
  }).length;
  $('memberCount').textContent = done + '/' + state.members.length;
}

/* ---------------------------------------------------------- transcript -- */

function addSpeech(evt) {
  const box = $('transcript');
  if (box.querySelector('.empty')) box.innerHTML = '';

  const data = evt.data || {};
  const stance = data.stance || data.rating || '';
  const conf = (data.confidence !== undefined && data.confidence !== null) ? data.confidence : null;

  const points = []
    .concat(data.points || [])
    .concat(data.risks || [])
    .concat(data.warnings || [])
    .concat(data.thesis || []);

  const isWarn = evt.agent === 'risk' || evt.agent === 'bear';

  let html = '<div class="speech">' +
    '<div class="speech-head">' +
      '<div class="speech-avatar" style="' + avatarStyle(evt.agent) + '">' +
        esc(initial(evt.name)) + '</div>' +
      '<span class="speech-name">' + esc(evt.name) + '</span>' +
      (stance ? '<span class="member-badge ' + stanceClass(stance) + '">' + esc(stance) + '</span>' : '') +
      (conf !== null ? '<span class="speech-time">置信度 ' + Math.round(conf) + '%</span>' : '') +
      '<span class="speech-time" style="margin-left:auto">' + esc(evt.ts || '') + '</span>' +
    '</div>' +
    '<div class="speech-body">' + esc(evt.text || '') + '</div>';

  if (points.length) {
    html += '<ul class="speech-points">' + points.slice(0, 4).map((p) =>
      '<li class="' + (isWarn ? 'warn' : '') + '">' + esc(p) + '</li>').join('') + '</ul>';
  }
  if (data.rebuttal) {
    html += '<ul class="speech-points"><li>' + esc(data.rebuttal) + '</li></ul>';
  }
  html += '</div>';

  box.insertAdjacentHTML('beforeend', html);
  box.scrollTop = box.scrollHeight;
  state.seenSpeeches += 1;
}

function addSystem(text, kind) {
  const box = $('transcript');
  if (box.querySelector('.empty')) box.innerHTML = '';
  box.insertAdjacentHTML('beforeend',
    '<div class="speech system ' + (kind || '') + '">' +
    '<div class="speech-body">' + esc(text) + '</div></div>');
  box.scrollTop = box.scrollHeight;
}

/* ----------------------------------------------------------- consensus -- */

function renderConsensus(data) {
  state.rating = data.rating || '';
  const lv = data.levels || {};
  const rating = data.rating || 'HOLD';
  const label = { BUY: '买入', SELL: '卖出', HOLD: '持有' }[rating] || rating;

  const pos = Array.isArray(lv.position_pct) ? lv.position_pct : null;
  const posText = pos ? (Math.round(pos[0]) + '% - ' + Math.round(pos[1]) + '%') : '—';
  const targetText = (lv.target_low || lv.target_high)
    ? (fmtNum(lv.target_low) + ' - ' + fmtNum(lv.target_high)) : '—';

  let html =
    '<div class="rating-card">' +
      '<div class="rating-top">' +
        '<span class="rating-value rating-' + rating + '">' + rating + '</span>' +
        '<span class="rating-sub">' + label + ' · 共识度 ' + Math.round(data.consensus || 0) + '%</span>' +
      '</div>' +
      '<div class="consensus-meter">' +
        '<div class="label"><span>委员会共识度</span><span>' + Math.round(data.consensus || 0) + '%</span></div>' +
        '<div class="track"><div class="fill" style="width:' +
          Math.max(0, Math.min(100, Math.round(data.consensus || 0))) + '%"></div></div>' +
      '</div>' +
    '</div>';

  html += '<div class="cblock"><h4>核心逻辑</h4><ol>' +
    (data.thesis || []).map((t, i) =>
      '<li><span class="n">' + (i + 1) + '</span><span>' + esc(t) + '</span></li>').join('') +
    '</ol></div>';

  html += '<div class="cblock risks"><h4>主要风险</h4><ol>' +
    (data.risks || []).map((t, i) =>
      '<li><span class="n">' + (i + 1) + '</span><span>' + esc(t) + '</span></li>').join('') +
    '</ol></div>';

  html += '<div class="cblock"><h4>关键价位</h4><div class="levels">' +
    '<div class="level-item"><div class="level-k">支撑位</div><div class="level-v down">' +
      fmtNum(lv.support) + '</div></div>' +
    '<div class="level-item"><div class="level-k">压力位</div><div class="level-v up">' +
      fmtNum(lv.resistance) + '</div></div>' +
    '<div class="level-item"><div class="level-k">目标区间</div><div class="level-v">' +
      targetText + '</div></div>' +
    '<div class="level-item"><div class="level-k">建议仓位</div><div class="level-v">' +
      posText + '</div></div>' +
    '<div class="level-item"><div class="level-k">止损位</div><div class="level-v down">' +
      fmtNum(lv.stop_loss) + '</div></div>' +
    '<div class="level-item"><div class="level-k">辩论轮次</div><div class="level-v">' +
      (data.debate_rounds || '—') + ' 轮</div></div>' +
  '</div></div>';

  html += '<div class="cblock conditions"><h4>策略有效性条件</h4><ul>' +
    (data.conditions || []).map((t) => '<li>' + esc(t) + '</li>').join('') +
    '</ul></div>';

  if (data.action) {
    html += '<div class="action-box"><div class="k">执行建议</div>' +
      '<div class="v">' + esc(data.action) + '</div></div>';
  }

  $('consensus').innerHTML = html;
}

/* ------------------------------------------------------------ pipeline -- */

function setStage(stage, status, note) {
  const idx = STAGES.indexOf(stage);
  const items = $('pipeline').querySelectorAll('li');
  items.forEach((li, i) => {
    const s = li.getAttribute('data-stage');
    if (s === stage) {
      li.classList.remove('running', 'done', 'waiting');
      if (status === 'running') li.classList.add('running');
      else if (status === 'done') li.classList.add('done');
      else if (status === 'waiting') li.classList.add('waiting');
    } else if (idx >= 0 && i < idx && !li.classList.contains('done')) {
      li.classList.remove('running', 'waiting');
      li.classList.add('done');
    }
  });
  if (note) addSystem(note, 'stage-note');
}

function resetPipeline() {
  $('pipeline').querySelectorAll('li').forEach((li) =>
    li.classList.remove('running', 'done', 'waiting'));
}

/* -------------------------------------------------------------- stream -- */

function setLive(text, active) {
  const tag = $('liveTag');
  tag.textContent = text;
  tag.className = 'live' + (active ? ' active' : ' idle');
}

function handleEvent(evt) {
  switch (evt.type) {
    case 'start':
      state.members = evt.members || [];
      renderMembers(evt.members || []);
      if (evt.engine) {
        const badge = $('engineBadge');
        badge.textContent = evt.engine.ready
          ? (evt.engine.label + ' · ' + evt.engine.model)
          : '规则引擎（未配置模型）';
        badge.className = 'engine-badge' + (evt.engine.ready ? ' llm' : '');
      }
      break;

    case 'snapshot':
      renderQuote(evt.data);
      break;

    case 'stage':
      setStage(evt.stage, evt.status, evt.note);
      break;

    case 'agent_start':
      markMember(evt.agent, 'active');
      break;

    case 'speech':
      markMember(evt.agent, 'done');
      addSpeech(evt);
      updateMemberResult(evt.agent, evt.data);
      break;

    case 'warn':
      addSystem(evt.message, 'stage-note');
      break;

    case 'news':
      if (evt.count) {
        addSystem('金十快讯 ' + evt.count + ' 条，与标的直接相关 ' +
                  (evt.matched || 0) + ' 条', 'stage-note');
      }
      break;

    case 'consensus':
      renderConsensus(evt.data);
      break;

    case 'error':
      addSystem(evt.message, 'stage-note');
      setLive('出错', false);
      break;

    case 'done':
      setLive('已完成', false);
      state.running = false;
      $('runBtn').disabled = false;
      break;

    default:
      break;
  }
}

function startAnalysis() {
  if (state.running) return;
  showPage('committee');
  const symbol = ($('symbolInput').value || '').trim();
  if (!symbol) { alert('请先输入标的代码'); return; }

  state.symbol = symbol;
  state.running = true;
  state.finished = false;
  state.seenSpeeches = 0;

  $('runBtn').disabled = true;
  $('transcript').innerHTML = '<p class="empty">正在召集委员…</p>';
  $('consensus').innerHTML = '<p class="empty">辩论结束后生成。</p>';
  $('decisionStatus').hidden = true;
  resetPipeline();
  setLive('实时讨论中', true);

  if (state.source) state.source.close();

  const url = '/api/analyze?symbol=' + encodeURIComponent(symbol);
  const es = new EventSource(url);
  state.source = es;

  es.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (err) { return; }
    try { handleEvent(evt); } catch (err) { console.error(err); }
    if (evt.type === 'done') { es.close(); state.source = null; }
  };

  es.onerror = () => {
    es.close();
    state.source = null;
    if (state.running) {
      state.running = false;
      $('runBtn').disabled = false;
      setLive('连接中断', false);
      addSystem('与服务器的连接中断。若服务已停止，请重新启动后重试。', 'stage-note');
    }
  };
}

/* ------------------------------------------------------------ decision -- */

async function submitDecision(action) {
  const note = ($('decisionNote').value || '').trim();
  const box = $('decisionStatus');
  const labels = { approve: '通过策略', modify: '修改策略', reject: '驳回' };

  if (action !== 'approve' && !note) {
    box.hidden = false;
    box.className = 'decision-status warn';
    box.textContent = '选择「' + labels[action] + '」时，建议写下你的理由或调整方向。';
    return;
  }

  box.hidden = false;
  box.className = 'decision-status';
  box.textContent = '正在记录…';

  try {
    const resp = await fetch('/api/decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // The decision is persisted server-side, so it carries everything the
      // review page needs to show what was decided at what price.
      body: JSON.stringify({
        action: action,
        note: note,
        symbol: state.symbol || ($('symbolInput').value || '').trim(),
        name: ($('tkName').textContent || '').trim(),
        price: parseFloat(($('priceVal').textContent || '').replace(/[^\d.\-]/g, '')) || null,
        rating: state.rating || '',
      }),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'unknown error');
    box.className = 'decision-status ok';
    box.textContent = '已记录：' + labels[action] +
      '（' + data.recorded_at + '）。本系统不接入任何交易通道，不会真实下单。';
  } catch (err) {
    box.className = 'decision-status err';
    box.textContent = '记录失败：' + err.message;
  }
}

/* ------------------------------------------------------------ settings -- */

let providersCache = [];

async function openSettings() {
  const modal = $('settingsModal');
  modal.hidden = false;
  $('cfgStatus').textContent = '加载中…';
  try {
    const resp = await fetch('/api/config');
    const data = await resp.json();
    providersCache = data.providers || [];

    $('cfgProvider').innerHTML = providersCache.map((p) =>
      '<option value="' + p.id + '">' + esc(p.label) +
      (p.model ? '  ·  ' + esc(p.model) : '') + '</option>').join('');

    const cfg = data.config || {};
    $('cfgProvider').value = (cfg.llm && cfg.llm.provider) || 'deepseek';
    $('cfgModel').value = (cfg.llm && cfg.llm.model) || '';
    $('cfgBaseUrl').value = (cfg.llm && cfg.llm.base_url) || '';
    $('cfgRounds').value = String((cfg.committee && cfg.committee.debate_rounds) || 2);
    $('cfgKey').value = '';
    $('cfgKey').placeholder = (cfg.llm && cfg.llm.has_key)
      ? '已保存（留空则不修改）'
      : '留空则使用环境变量或规则引擎';

    $('cfgStatus').textContent = data.engine && data.engine.ready
      ? '当前引擎：' + data.engine.label + ' · ' + data.engine.model
      : '当前使用规则引擎（未配置 API Key）';
    $('cfgStatus').className = 'modal-status';
    updateProviderHint();
  } catch (err) {
    $('cfgStatus').textContent = '加载失败：' + err.message;
    $('cfgStatus').className = 'modal-status err';
  }
}

function updateProviderHint() {
  const id = $('cfgProvider').value;
  const p = providersCache.find((x) => x.id === id);
  const parts = [];
  if (p && p.note) parts.push(p.note);
  if (p && p.env && p.env.length) parts.push('环境变量：' + p.env.join(' / '));
  $('cfgStatus').textContent = parts.join('　');
  $('cfgStatus').className = 'modal-status';
}

async function saveSettings() {
  const status = $('cfgStatus');
  status.textContent = '保存中…';
  status.className = 'modal-status';

  const payload = {
    provider: $('cfgProvider').value,
    model: $('cfgModel').value.trim(),
    base_url: $('cfgBaseUrl').value.trim(),
    debate_rounds: Number($('cfgRounds').value),
  };
  const key = $('cfgKey').value.trim();
  if (key) payload.api_key = key;

  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'unknown error');
    status.className = 'modal-status ok';
    status.textContent = data.engine.ready
      ? '已保存。下次分析将使用 ' + data.engine.label + ' · ' + data.engine.model
      : '已保存，但该服务商仍缺少 API Key，将继续使用规则引擎。';
    refreshEngineBadge();
  } catch (err) {
    status.className = 'modal-status err';
    status.textContent = '保存失败：' + err.message;
  }
}

async function refreshEngineBadge() {
  try {
    const resp = await fetch('/api/config');
    const data = await resp.json();
    const e = data.engine || {};
    const badge = $('engineBadge');
    badge.textContent = e.ready ? (e.label + ' · ' + e.model) : '规则引擎（未配置模型）';
    badge.className = 'engine-badge' + (e.ready ? ' llm' : '');
  } catch (err) { /* non-fatal */ }
}

/* --------------------------------------------------------------- search -- */

let suggestTimer = null;
let suggestItems = [];

function hideSuggest() {
  $('suggest').hidden = true;
  suggestItems = [];
}

function renderSuggest(items) {
  const box = $('suggest');
  suggestItems = items;
  if (!items.length) {
    box.innerHTML = '<div class="suggest-empty">没有匹配项，可直接输入代码后回车</div>';
    box.hidden = false;
    return;
  }
  box.innerHTML = items.slice(0, 12).map((it, i) => {
    const tag = it.tag || (it.kind === 'sector_us' ? '美股板块' : '板块');
    const cls = it.kind === 'index' ? 'idx'
      : (it.kind === 'stock' ? 'stk' : (it.kind === 'sector_us' ? 'us' : 'cn'));
    const chg = (it.change_pct === null || it.change_pct === undefined) ? ''
      : '<span class="suggest-code ' + pctClass(it.change_pct) + '">' +
        fmtPct(it.change_pct) + '</span>';
    return '<div class="suggest-item" data-index="' + i + '">' +
      '<div class="suggest-main">' +
        '<span class="suggest-name">' + esc(it.name) + '</span>' +
        '<span class="suggest-code">' + esc(it.code) + '</span>' + chg +
      '</div>' +
      '<span class="suggest-tag ' + cls + '">' + esc(tag) + '</span>' +
    '</div>';
  }).join('');
  box.hidden = false;
}

let suggestSeq = 0;

async function fetchSuggest(keyword) {
  // Responses can land out of order: typing 「上证」 and then 「贵州」 used to
  // let the slower first answer overwrite the second one, so the dropdown
  // showed results for a query the box no longer contained.
  const seq = ++suggestSeq;
  try {
    // One box that knows about indices, boards and individual stocks - the
    // board-only search used to come back empty for a stock code.
    const resp = await fetch('/api/search?q=' + encodeURIComponent(keyword));
    const data = await resp.json();
    if (seq !== suggestSeq) return;
    if (!data.ok) { hideSuggest(); return; }
    renderSuggest(data.items || []);
  } catch (err) {
    if (seq === suggestSeq) hideSuggest();
  }
}

function onSymbolInput() {
  const keyword = ($('symbolInput').value || '').trim();
  if (suggestTimer) clearTimeout(suggestTimer);
  if (!keyword) { hideSuggest(); return; }
  suggestTimer = setTimeout(() => fetchSuggest(keyword), 300);
}

/* ------------------------------------------------------- sector picker -- */

let sectorType = 'industry';

async function openSectorPanel() {
  $('sectorModal').hidden = false;
  $('sectorSearch').value = '';
  $('sectorList').innerHTML = '<div class="sector-empty">加载中…</div>';
  $('sectorStatus').textContent = '';
  loadSectorTab(sectorType);
}

async function loadSectorTab(type, keyword) {
  if (type) sectorType = type;
  const status = $('sectorStatus');
  const list = $('sectorList');

  document.querySelectorAll('#sectorTabs .tab').forEach((tab) => {
    tab.classList.toggle('active', tab.getAttribute('data-type') === sectorType);
  });

  status.textContent = '加载中…';
  const url = keyword
    ? '/api/sectors?q=' + encodeURIComponent(keyword)
    : (sectorType === 'commodity'
       ? '/api/commodities'
       : '/api/sectors?type=' + encodeURIComponent(sectorType));

  try {
    const resp = await fetch(url);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'load failed');
    const items = data.items || [];
    if (sectorType === 'commodity' && !keyword) {
      renderCommodityList(items);
      status.textContent = '共 ' + items.length + ' 个品种 · 行情实时，暂不支持分析';
      return;
    }
    renderSectorList(items);
    const liveCount = items.filter((x) => x.live).length;
    status.textContent = '共 ' + items.length + ' 个板块' +
      (liveCount ? '，其中 ' + liveCount + ' 个行情为刚刚同步' : '，行情来自板块列表快照');
  } catch (err) {
    list.innerHTML = '<div class="sector-empty">加载失败：' + esc(err.message) + '</div>';
    status.textContent = '';
  }
}

function renderSectorList(items) {
  const list = $('sectorList');
  if (!items.length) {
    list.innerHTML = '<div class="sector-empty">没有匹配的板块</div>';
    return;
  }
  list.innerHTML = items.map((it) => {
    const boardText = it.board === 'concept' ? '概念'
      : (it.board === 'etf' ? 'ETF' : '行业');
    let pills = '';
    if (it.live) pills += '<span class="pill live">实时</span>';
    if (it.history_source === 'eastmoney') {
      pills += '<span class="pill warn" title="该板块K线依赖东财接口，高峰时段可能取不到">K线待定</span>';
    }
    const chg = (it.change_pct === null || it.change_pct === undefined)
      ? '—' : fmtPct(it.change_pct);
    return '<div class="sector-row" data-code="' + esc(it.code) + '">' +
      '<div class="sector-row-main">' +
        '<span class="sector-row-name">' + esc(it.name) + '</span>' +
        '<span class="sector-row-code">' + esc(it.code) + ' · ' + boardText + '</span>' +
      '</div>' +
      '<div class="sector-row-right">' + pills +
        '<span class="sector-row-chg ' + pctClass(it.change_pct) + '">' + chg + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

/* Commodities are quoted, not analysed: they have live prices but no
   committee pipeline, so the rows say so rather than pretending otherwise. */
function renderCommodityList(items) {
  const list = $('sectorList');
  if (!items.length) {
    list.innerHTML = '<div class="sector-empty">暂无商品行情</div>';
    return;
  }
  list.innerHTML = items.map((it) => {
    const chg = (it.change_pct === null || it.change_pct === undefined)
      ? '—' : fmtPct(it.change_pct);
    return '<div class="sector-row commodity" data-code="' + esc(it.code) + '">'
      + '<div class="sector-row-main">'
      + '<span class="sector-row-name">' + esc(it.name) + '</span>'
      + '<span class="sector-row-code">' + esc(it.group) + ' · ' + esc(it.unit) + '</span>'
      + '</div>'
      + '<div class="sector-row-right">'
      + '<span class="sector-row-price">' + fmtNum(it.price) + '</span>'
      + '<span class="sector-row-chg ' + pctClass(it.change_pct) + '">' + chg + '</span>'
      + '</div></div>';
  }).join('');
}

function pickSector(code) {
  if (!code) return;
  // Commodities carry a dotted or underscored code (113.aum, hf_XAU) and are
  // quoted only - there is no committee pipeline behind them.
  if (code.indexOf('.') >= 0 || code.indexOf('_') >= 0) {
    $('sectorModal').hidden = true;
    alert('商品行情仅供观察，暂不支持投委会分析。');
    return;
  }
  $('sectorModal').hidden = true;
  $('symbolInput').value = code;
  hideSuggest();
  startAnalysis();
}

/* -------------------------------------------------------------- router -- */

const PAGES = ['treemap', 'billboard', 'committee'];
let currentPage = 'treemap';

function showPage(name, force) {
  if (PAGES.indexOf(name) < 0) name = 'treemap';
  const changed = currentPage !== name;
  currentPage = name;

  PAGES.forEach((page) => {
    const section = document.getElementById('page-' + page);
    if (section) section.classList.toggle('active', page === name);
  });
  document.querySelectorAll('#nav a[data-page]').forEach((link) => {
    link.classList.toggle('active', link.getAttribute('data-page') === name);
  });
  if (location.hash !== '#/' + name) {
    history.replaceState(null, '', '#/' + name);
  }
  // `force` matters on the very first call: the default page has not changed
  // from the initial value, but its loader still has to run.
  if ((changed || force) && window.Pages && window.Pages.onEnter) {
    window.Pages.onEnter(name);
  }
}

function openSymbol(symbol, quiet) {
  if (symbol) $('symbolInput').value = symbol;
  showPage('committee');
  if (!quiet) startAnalysis();
}

/* ---------------------------------------------------------------- init -- */

function init() {
  $('runBtn').addEventListener('click', startAnalysis);
  $('symbolInput').addEventListener('input', onSymbolInput);
  $('symbolInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { hideSuggest(); startAnalysis(); }
    else if (e.key === 'Escape') hideSuggest();
  });
  $('suggest').addEventListener('click', (e) => {
    const item = e.target.closest('.suggest-item');
    if (!item) return;
    const picked = suggestItems[Number(item.getAttribute('data-index'))];
    if (!picked) return;
    if (picked.kind === 'commodity') {
      hideSuggest();
      alert('商品行情仅供观察，暂不支持投委会分析。');
      return;
    }
    $('symbolInput').value = picked.value || picked.code;
    hideSuggest();
    startAnalysis();
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-box')) hideSuggest();
  });

  document.querySelectorAll('.decision-actions button').forEach((btn) => {
    btn.addEventListener('click', () => submitDecision(btn.getAttribute('data-action')));
  });

  $('browseBtn').addEventListener('click', openSectorPanel);
  $('sectorClose').addEventListener('click', () => { $('sectorModal').hidden = true; });
  $('sectorModal').addEventListener('click', (e) => {
    if (e.target === $('sectorModal')) $('sectorModal').hidden = true;
  });
  $('sectorTabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    // The keyword survives the switch on purpose: typing 「黄金」 and then
    // clicking over to another tab should not throw the query away.
    const kw = ($('sectorSearch').value || '').trim();
    loadSectorTab(tab.getAttribute('data-type'), kw || undefined);
  });
  $('sectorList').addEventListener('click', (e) => {
    const row = e.target.closest('.sector-row');
    if (row) pickSector(row.getAttribute('data-code'));
  });

  let sectorTimer = null;
  $('sectorSearch').addEventListener('input', () => {
    if (sectorTimer) clearTimeout(sectorTimer);
    const kw = ($('sectorSearch').value || '').trim();
    sectorTimer = setTimeout(() => loadSectorTab(sectorType, kw), 320);
  });
  $('sectorSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const kw = ($('sectorSearch').value || '').trim();
      if (kw) loadSectorTab(sectorType, kw);
    }
  });

  $('navSettings').addEventListener('click', openSettings);
  $('nav').addEventListener('click', (e) => {
    const link = e.target.closest('a[data-page]');
    if (link) showPage(link.getAttribute('data-page'));
  });
  $('cfgCancel').addEventListener('click', () => { $('settingsModal').hidden = true; });
  $('cfgSave').addEventListener('click', saveSettings);
  $('cfgProvider').addEventListener('change', updateProviderHint);
  $('settingsModal').addEventListener('click', (e) => {
    if (e.target === $('settingsModal')) $('settingsModal').hidden = true;
  });

  refreshEngineBadge();
  fetch('/api/quote?symbol=' + encodeURIComponent($('symbolInput').value))
    .then((r) => r.json())
    .then((d) => { if (d.ok) renderQuote(d.data); })
    .catch(() => {});

  const initial = (location.hash || '').replace(/^#\/?/, '');
  showPage(PAGES.indexOf(initial) >= 0 ? initial : 'treemap', true);
  window.addEventListener('hashchange', () => {
    const name = (location.hash || '').replace(/^#\/?/, '');
    if (name && name !== currentPage) showPage(name);
  });
}

document.addEventListener('DOMContentLoaded', init);
