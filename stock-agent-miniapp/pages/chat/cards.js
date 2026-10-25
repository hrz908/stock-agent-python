// 对话消息与八张金融卡的「后端载荷 -> 组件 props」映射。
//
// 为什么单独一层：
// - 页面的职责是会话状态与交互，不该再背 300 行字段映射；
// - 这层全是纯函数（输入 payload、输出消息对象），可以脱离页面单测；
// - 新增一张卡只改这里 + chat.wxml 的分支，页面逻辑不用动。
//
// 约定：message 结构统一为
//   纯文本 { role, status:'complete', content:[{ type, data }], blocks? }
//   卡片   { role, status:'complete', card:'quote'|..., content:[], <cardKey>: {...} }
import { parseMarkdown } from '../../utils/markdown';

// ===== 基础消息 =====
function assistantMsg(data) {
  return {
    role: 'assistant',
    status: 'complete',
    content: [{ type: 'markdown', data }],
    // markdown 渲染结构：Lexer 切 token 后自绘（见 utils/markdown.js 与 chat.wxml 的 md-* 模板）
    blocks: parseMarkdown(data)
  };
}

function userMsg(data) {
  return {
    role: 'user',
    status: 'complete',
    content: [{ type: 'text', data }]
  };
}

// ===== 字段工具 =====
// 后端 decimal 字段可能是数字也可能是字符串，统一转 number；空值给 null
function numOrNull(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

// 交易状态：后端给英文枚举，转成用户看得懂的中文。
// 未知值一律不显示：直接把英文枚举抛给用户比空着更糟
function statusTextOf(s) {
  const map = {
    trading: '交易中',
    closed: '已收盘',
    suspended: '停牌',
    limit_up: '涨停',
    limit_down: '跌停',
    resume: '复牌'
  };
  return map[s] || '';
}

// 万元 -> 元（估值 / 资金流的金额字段单位是万元，卡内亿万换算是元口径）
function wanToYuan(v) {
  const n = numOrNull(v);
  return n == null ? null : n * 10000;
}

// 报告期标签：end_date "20260630" -> "2026 中报"
function periodLabelOf(endDate) {
  const s = String(endDate || '');
  if (!/^\d{8}$/.test(s)) return '';
  const map = { '03': '一季报', '06': '中报', '09': '三季报', '12': '年报' };
  return s.slice(0, 4) + ' ' + (map[s.slice(4, 6)] || '报告期');
}

// YYYYMMDD -> YYYY-MM-DD（不成型原样返回空串）
function fmtTsDate(v) {
  const s = String(v || '');
  return /^\d{8}$/.test(s) ? s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6) : '';
}

// 简单移动平均：与 values 等长，前 n-1 项为 null（数据不足不出点，折线自然断开）
function movingAverage(values, n) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    sum += v == null ? 0 : v;
    if (i >= n) {
      const drop = values[i - n];
      sum -= drop == null ? 0 : drop;
    }
    out.push(i >= n - 1 && v != null ? sum / n : null);
  }
  return out;
}

// ===== 八张卡 =====
// K 线卡传给组件的根数。必须与 components/finance/kline-card 的 MAX_BARS 一致：
// 组件只画 MAX_BARS 根，这里多传只会让副标题「最近 N 个交易日」与实际图不符
const KLINE_CARD_ROWS = 15;

// 行情卡：后端 QuoteDetail 的 change_percent 可能是字符串，成交额单位是万元
function quoteCard(q) {
  const amount = q.amount == null ? null : parseFloat(q.amount) * 10000;
  return {
    role: 'assistant',
    status: 'complete',
    card: 'quote',
    content: [],
    quote: {
      name: q.name,
      code: q.code,
      status: statusTextOf(q.status),
      price: numOrNull(q.price),
      change: numOrNull(q.change),
      changePct: numOrNull(q.change_percent),
      open: numOrNull(q.open),
      high: numOrNull(q.high),
      low: numOrNull(q.low),
      amount: isNaN(amount) ? null : amount,
      turnoverRate: numOrNull(q.turnover),
      totalCap: numOrNull(q.total_mv),
      // 分时走势与悬浮标签：数据源没有，显式给空数组——
      // 组件内按「点数不足 2 个不出迷你走势」降级，不给就会拿到 undefined
      points: [],
      tags: [],
      note: q.time ? '行情时间 ' + q.time : ''
    }
  };
}

// K 线卡：meta 是 table 事件的整个 payload（可能带 code / name：本轮查过行情时后端会补上）
function klineCard(rows, meta) {
  // 后端 KlineRow：date / open / high / low / close / volume / change_percent
  const all = Array.isArray(rows) ? rows : [];
  const shown = all.slice(-KLINE_CARD_ROWS);
  const klines = shown.map((r) => ({
    d: r.date,
    o: numOrNull(r.open),
    h: numOrNull(r.high),
    l: numOrNull(r.low),
    c: numOrNull(r.close),
    v: numOrNull(r.volume)
  }));

  const closes = klines.map((k) => k.c);
  const ma = {
    ma5: movingAverage(closes, 5),
    ma10: movingAverage(closes, 10),
    ma20: movingAverage(closes, 20)
  };

  // 区间涨跌幅 / 振幅 / 支撑压力：全部按渲染区间内的真实高低点算，不涉及任何预测
  const first = klines[0];
  const last = klines[klines.length - 1];
  const highs = klines.map((k) => k.h).filter((v) => v != null);
  const lows = klines.map((k) => k.l).filter((v) => v != null);
  let periodPct = null;
  let amplitude = null;
  if (first && last && first.c) {
    periodPct = ((last.c - first.c) / first.c) * 100;
    if (highs.length && lows.length) {
      amplitude = ((Math.max.apply(null, highs) - Math.min.apply(null, lows)) / first.c) * 100;
    }
  }

  return {
    role: 'assistant',
    status: 'complete',
    card: 'kline',
    content: [],
    kline: klines,
    klineMa: ma,
    klinePct: periodPct,
    klineAmplitude: amplitude,
    // 量比需要当日分时成交节奏，日 K 数据源算不出来，不出这个指标（不拿日线近似）
    klineVolumeRatio: null,
    // 支撑 / 压力取区间内的最低 / 最高价（区间极值，不是预测位）
    klineSupport: lows.length ? Math.min.apply(null, lows) : null,
    klineResistance: highs.length ? Math.max.apply(null, highs) : null,
    klineName: (meta && meta.name) || '',
    klineCode: (meta && meta.code) || '',
    // 副标题按实际渲染的根数走：被截断时如实标出
    klineSub: '最近 ' + klines.length + ' 个交易日 · 前复权' + (all.length > klines.length ? '（共 ' + all.length + ' 根）' : ''),
    // 口径类说明按需求全部移除，卡内不再出注释行
    klineNote: ''
  };
}

// 估值卡：daily_basic 行。历史分位 / 行业均值当前数据源没有，传空由卡片降级
function valuationCard(payload) {
  const row = Array.isArray(payload) ? payload[0] : payload;
  if (!row) return null;
  return {
    role: 'assistant',
    status: 'complete',
    card: 'valuation',
    content: [],
    valuation: {
      peStatic: numOrNull(row.pe),
      // peTtm 而非 peTTM：微信把 wxml 的 pe-ttm 映射成 peTtm，写全大写会落空
      peTtm: numOrNull(row.pe_ttm),
      pb: numOrNull(row.pb),
      ps: numOrNull(row.ps_ttm),
      // PEG 需要「PE ÷ 净利增速」，当前数据源不给增速，恒空（卡片显示「--」）
      peg: numOrNull(row.peg),
      dividendYield: numOrNull(row.dv_ttm),
      totalCap: wanToYuan(row.total_mv),
      note: [row.ts_code, row.trade_date ? '数据日期 ' + fmtTsDate(row.trade_date) : '']
        .filter(Boolean).join(' · ')
    }
  };
}

// 资金流向卡：moneyflow 行。四档净额 = 买入 - 卖出；net_mf_amount 即主力净流入。
// 命中龙虎榜时（agent 的 get_top_list）payload 带上 lhb 席位区块，金额统一换算成元
function fundflowCard(payload) {
  const row = Array.isArray(payload) ? payload[0] : payload;
  if (!row) return null;
  const net = (b, s) => {
    const nb = numOrNull(b);
    const ns = numOrNull(s);
    return nb == null || ns == null ? null : (nb - ns) * 10000;
  };
  const lhb = (Array.isArray(row.lhb) ? row.lhb : [])
    .filter((r) => r && r.seat)
    .slice(0, 10)
    .map((r) => ({ seat: r.seat, buy: wanToYuan(r.buy), sell: wanToYuan(r.sell) }));
  const note = [
    row.ts_code,
    row.trade_date ? '数据日期 ' + fmtTsDate(row.trade_date) : '',
    row.lhb_date ? '龙虎榜 ' + fmtTsDate(row.lhb_date) : ''
  ].filter(Boolean).join(' · ');
  return {
    role: 'assistant',
    status: 'complete',
    card: 'fundflow',
    content: [],
    fundflow: {
      mainNet: wanToYuan(row.net_mf_amount),
      // 占成交额比 / 北向持股 / 连续流入天数 / 近 5 日趋势：对应数据源已下线
      // （见 server/README 的降级说明），恒空，卡片不出这几块，不编造数值
      mainNetPct: null,
      northPct: null,
      northChangePct: null,
      streakDays: null,
      days5: [],
      tiers: [
        { label: '超大单', value: net(row.buy_elg_amount, row.sell_elg_amount) },
        { label: '大单', value: net(row.buy_lg_amount, row.sell_lg_amount) },
        { label: '中单', value: net(row.buy_md_amount, row.sell_md_amount) },
        { label: '小单', value: net(row.buy_sm_amount, row.sell_sm_amount) }
      ],
      lhb: lhb,
      note: note
    }
  };
}

// 同业对比卡：get_peer_compare 载荷 { items, ts_code }，维度全是当日快照
function compareCard(payload) {
  const items = Array.isArray(payload && payload.items) ? payload.items : [];
  const usable = items.filter((it) => it && it.name && !it.error);
  if (usable.length < 2) return null; // 对比至少要有两个标的，组件按空卡降级
  return {
    role: 'assistant',
    status: 'complete',
    card: 'compare',
    content: [],
    compare: {
      items: usable,
      note: [payload.ts_code, '同业对比 · 当日行情快照'].filter(Boolean).join(' · ')
    }
  };
}

// 财报速览卡：get_financial_report 组合载荷 { indicator, income, cashflow }，
// 任一子表可能为 null（积分不足时常见）。金额单位元，比率/同比是百分数值
function reportCard(payload) {
  const data = payload || {};
  const ind = data.indicator || {};
  const inc = data.income || {};
  const cf = data.cashflow || {};
  const endDate = inc.end_date || ind.end_date || cf.end_date || '';
  const date = fmtTsDate(endDate);
  return {
    role: 'assistant',
    status: 'complete',
    card: 'report',
    content: [],
    report: {
      periodLabel: periodLabelOf(endDate),
      revenue: { value: numOrNull(inc.revenue), yoy: numOrNull(ind.or_yoy) },
      netProfit: { value: numOrNull(inc.n_income_attr_p), yoy: numOrNull(ind.netprofit_yoy) },
      cashFlow: { value: numOrNull(cf.n_cashflow_act), yoy: null },
      grossMargin: numOrNull(ind.grossprofit_margin),
      netMargin: numOrNull(ind.netprofit_margin),
      roe: numOrNull(ind.roe),
      eps: numOrNull(ind.eps),
      // 一致预期数据源没有，beat 留空（卡片不出「超预期」标签）
      beat: '',
      note: [inc.ts_code || ind.ts_code || cf.ts_code, date ? '数据日期 ' + date : '']
        .filter(Boolean).join(' · ')
    }
  };
}

// 研报观点卡：东财研报没有目标价与盈利预测调整方向，相关字段传 null，
// 卡片按缺失降级显示「--」（工具层与前端都不编造数值）
function researchCard(payload) {
  const row = Array.isArray(payload) ? payload[0] : payload;
  if (!row) return null;
  return {
    role: 'assistant',
    status: 'complete',
    card: 'research',
    content: [],
    research: {
      org: row.org || '',
      rating: row.rating || '',
      targetPrice: numOrNull(row.target_price),
      publishDate: row.publish_date || '',
      reportType: row.report_type || '',
      summary: row.summary || '',
      forecastDir: row.forecast_dir || '',
      upside: numOrNull(row.upside),
      sourceName: row.source_name || row.org || '',
      sourceUrl: row.source_url || ''
    }
  };
}

// 公司事件卡：get_stock_news 的当日资讯由后端折成时间轴事件，
// 类型也是后端按标题关键词归纳的（前端不做二次推断）。整轮合成一张卡
function eventCard(payload) {
  const data = payload || {};
  const events = (Array.isArray(data.events) ? data.events : [])
    .filter((e) => e && e.title)
    .map((e) => ({
      type: e.type || '',
      date: e.date || '',
      title: e.title,
      desc: e.desc || '',
      important: !!e.important,
      sourceUrl: e.source_url || e.sourceUrl || ''
    }));
  if (!events.length) return null;
  return {
    role: 'assistant',
    status: 'complete',
    card: 'event',
    content: [],
    event: {
      events: events,
      range: data.range || '',
      note: data.note || ''
    }
  };
}

export {
  assistantMsg,
  userMsg,
  numOrNull,
  quoteCard,
  klineCard,
  valuationCard,
  fundflowCard,
  compareCard,
  reportCard,
  researchCard,
  eventCard
};
