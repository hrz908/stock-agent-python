// 对话页：消息收发走 Agent SSE（services/agent.js 单例），token 逐字追加；
// 纯文本走自绘气泡 + markdown（utils/markdown.js），行情/K线等结构化数据走自绘卡片消息；
// 「新对话」压栈本页新实例，每实例一条独立 session + 独立消息状态（见 onNewChat / NEW_CHAT_URL）。

// 注意：这里只能用默认导入。「默认 + 命名」混合导入会让 devtools 的 SWC 热编译引入
// @swc/runtime/_interop_require_wildcard（本项目不存在），chat.js 加载即抛错、页面方法全丢
//（表现为 WXML 正常渲染但点击全部无效）。session_id 走 agentSocket.freshSessionId()。
import agentSocket from '../../services/agent';
import { startRecognize, stopRecognize, abortRecognize } from '../../services/asr';
import {
  listSessions,
  createSession,
  sessionMessages,
  deleteSession
} from '../../services/sessions';
import { parseMarkdown } from '../../utils/markdown';
import { list as listQuickAsks, click as reportQuickAskClick } from '../../services/quickAsk';
import { isDev } from '../../config';

// 侧边抽屉导航项：每行 = 文字 + 右侧箭头（左侧不放图标）
// 不列「主页」：抽屉本来就只在主页存在，这一项点了没地方去
const MENUS = [
  { key: 'kline', label: 'K线卡片' },
  { key: 'watchlist', label: '自选股浮动' },
  { key: 'alerts', label: '提醒推送' },
  { key: 'settings', label: '设置' }
];

// 首屏引导问题：正常走后端 /quick-asks（热度候选池 + 加权抽样 + 状态选模板 +
// 会话去重），这里只保留本地兜底——接口不可用（离线 / 未登录 / 池子为空）时
// 首屏仍然有东西可点，不能是一张白纸
const FALLBACK_STOCKS = [
  { code: '600519', name: '贵州茅台' },
  { code: '300750', name: '宁德时代' },
  { code: '002594', name: '比亚迪' },
  { code: '600036', name: '招商银行' },
  { code: '300059', name: '东方财富' },
  { code: '000858', name: '五粮液' },
  { code: '601899', name: '紫金矿业' },
  { code: '002475', name: '立讯精密' }
];
const FALLBACK_TEMPLATES = [
  { tag: '盘面', build: (s) => `${s.name}现在什么价？` },
  { tag: '资金', build: (s) => `${s.name}最近的资金流向怎么样？` },
  { tag: '基本面', build: (s) => `${s.name}的估值贵不贵？` },
  { tag: '风险', build: (s) => `${s.name}当前最大的风险点是什么？` }
];

// 抽三条不重复的股票，逐个套模板；展示与发送用同一句（兜底态不拼额外限定）
function buildFallbackQuickAsks() {
  const pool = FALLBACK_STOCKS.slice();
  return FALLBACK_TEMPLATES.map((tpl, i) => {
    const idx = Math.floor(Math.random() * pool.length);
    const stock = pool.splice(idx, 1)[0] || FALLBACK_STOCKS[0];
    const display = tpl.build(stock);
    return {
      id: `local_${stock.code}_${Date.now()}_${i}`,
      display,
      query: display,
      stock_code: stock.code,
      stock_name: stock.name,
      tag: tpl.tag
    };
  });
}

// 抽屉导航项 → 子页面路由，key 与 MENUS 一一对应
const ROUTES = {
  watchlist: '/pages/watchlist/watchlist',
  alerts: '/pages/alerts/alerts',
  settings: '/pages/settings/settings',
  kline: '/pages/kline/kline'
};

// 「新对话」：注册一条新 session 后压栈本页的一个新实例 = 一条全新会话
// （独立消息、独立会话），旧对话留在栈里可返回；
// 从新对话页进子页，返回照样回到新对话页。?session= 带上注册好的 id
const NEW_CHAT_URL = '/pages/chat/chat?new=1';

// 触发抽屉开关的最小横向滑动距离（px）
const SWIPE_MIN = 40;

// 发送看门狗：超过这个时长一个字都没回来，就认为这一轮没有响应（秒）
const SEND_TIMEOUT_MS = 45000;

// 对话列表底部锚点：scroll-into-view 滚动到它即可到达列表末尾
const CHAT_BOTTOM_ID = 'chat-bottom';

// 输入栏胶囊高度（rpx）：按「两行」预估，见 chat.wxss 的 .sender-bar。
// 首屏按最大高度预留才不会盖住最后一条消息，onReady 实测后会按真实高度修正。
const SENDER_BAR_RPX = 136;
// 胶囊上方留白（rpx）：取自 .sender-wrap 的 padding-top（--sp-2）
const SENDER_PAD_TOP_RPX = 16;
// 胶囊下方留白（rpx）：取自 .sender-bar 的 margin-bottom（--sp-2）；
// 必须与 chatHeight 的估算一起算，否则首帧会少让出这 8px
const SENDER_BAR_MARGIN_BOTTOM_RPX = 16;
// 卡片外边距：自绘胶囊后没有组件外边距了，只保留底部与安全区之间的距离
const SENDER_MARGIN_TOP_RPX = 0;
const SENDER_MARGIN_BOTTOM_PX = 12;
// 对话区底边与输入栏卡片之间的间距（rpx）：原值 29 含组件 margin-top 的 24rpx，
// margin-top 归零后改为 5，不同步减会留下那段空隙
const SENDER_GAP_RPX = 5;
// 键盘弹起时，输入栏在键盘顶边之上再抬的间距（rpx）。
// 只按键盘高度上抬的话，胶囊底边正好压在键盘顶边上：机型不同（键盘高度、
// 安全区、是否有候选栏）看起来时紧时松。这里统一加一段固定间距，
// 与键盘多高无关；用 rpx 是为了跟其他间距一样随屏幕宽度等比缩放，
// 各尺寸下观感一致（换算在 onLoad 里做，chatHeight 用的是 px）
const KEYBOARD_GAP_RPX = 16;

// 消息条目结构（自绘气泡）：
// - 纯文本消息：content[0].data 是原文，blocks 是解析后的渲染结构
// - 卡片消息（quote/kline 等）：content 为空数组，卡片本体由
//   components/finance/* 组件渲染（行情卡、K 线图的版式 markdown 还原不出来）
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

// 结构化卡片消息：八张卡（quote / kline / valuation / fundflow / report /
// research / event / compare）的本体全部由 components/finance/* 组件渲染，
// 页面只负责把后端 card / table 事件的字段映射成组件 props（组件内不做业务计算）

// 后端 decimal 字段可能是数字也可能是字符串，统一转 number；空值给 null
function numOrNull(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

// 交易状态：后端给 trading / closed，转成用户看得懂的中文
function statusTextOf(s) {
  if (s === 'trading') return '交易中';
  if (s === 'closed') return '已收盘';
  return s || '';
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

// 聊天场景的 K 线周期：table 事件只带 rows，不带周期与股票信息，
// 因此这里只展示日 K 一档（完整五周期切换属于 K 线详情页，见改造指南目录规划）
const KLINE_PERIODS_CHAT = [{ key: 'day', label: '日K' }];

// K 线卡最多渲染的根数。比旧表格的 10 行多：图表需要足够根数才能看出趋势与均线，
// 且 SVG 背景图按 viewBox 缩放，根数多不会增加渲染成本
const KLINE_CARD_ROWS = 30;

function quoteCard(q) {
  // 后端 QuoteDetail：涨跌幅字段是 change_percent（可能为字符串）；
  // 成交额单位是万元，卡内统一按元换算（utils/format 的亿万换算是元口径）
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

// meta 是 table 事件的整个 payload（可能带 code / name：本轮查过行情时后端会补上，
// K 线卡标题因此不再是空的）
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
    klinePeriods: KLINE_PERIODS_CHAT,
    klineActivePeriod: 'day',
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

// ===== 估值 / 资金流向 / 财报速览三张卡 =====
// 数据源已换成 akshare（东财/新浪），字段名沿用原 Tushare 口径，映射在此完成。
// 由 agent 的 _emit_structured 以 card 事件下发，
// 组件（components/finance/*）只做展示。行载荷统一可能是「数组包一行」或「直接一行」

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

// 万元 -> 元（估值 / 资金流的金额字段单位是万元，卡内亿万换算是元口径）
function wanToYuan(v) {
  const n = numOrNull(v);
  return n == null ? null : n * 10000;
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
// 命中龙虎榜时（agent 的 get_top_list）payload 带上 lhb 席位区块：
// 只取席位/买入/卖出三个量，金额单位万元 -> 统一换算成元
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
      // 占成交额比 / 北向持股 / 连续流入天数 / 近 5 日趋势：对应数据源已下线（
      // 见 server/README 的降级说明），恒空，卡片不出这几块，不编造数值
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

// 同业对比卡：get_peer_compare 载荷 { items, ts_code }。
// 维度全部为当日快照（价/涨跌幅/PE/PB/总市值），组件内做表格渲染与标优
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
      // 数据日期：与其它卡片同一写法（估值 / 资金流向的「数据日期 YYYY-MM-DD」）
      note: [inc.ts_code || ind.ts_code || cf.ts_code, date ? '数据日期 ' + date : '']
        .filter(Boolean).join(' · ')
    }
  };
}

// 研报观点卡：get_research_reports 的单行（机构 / 评级 / 报告名称 / 原文链接）。
// 东财研报没有目标价与盈利预测调整方向，targetPrice / upside / forecastDir 传 null，
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

// 公司事件卡：get_stock_news 的当日资讯由后端折成时间轴事件（类型也是后端
// 按标题关键词归纳的，前端不做二次推断）。整轮合成一张卡
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

// ===== 卡片 =====
// 一次回复最多出一张卡（后端按问题意图只挑一张），且一律排在正文之后：
// card / table 事件先暂存到 _pendingCard，整轮结束（正文逐字打完）后由 _drainCards 落位。
// 不做 loading 占位卡：占位卡要排在正文之前才有意义，而这一版卡片必须在正文之后。

// ===== 语音输入 =====
// 点一下开始聆听、再点一下把识别结果直接发给 agent（不是「按住说话」）
// 录音时长的两条边界：短于下限当作误触、不发出；触及上限插件自动结束，走同一条发出链路
const REC_MIN_MS = 1000;
const REC_MAX_MS = 60000;
// 语音键的三态文案
const VOICE_TIP_IDLE = '点一下说话';
const VOICE_TIP_LIVE = '聆听中 · 再点发送';
const VOICE_TIP_STOP = '正在转写…';
// 中间结果的视图刷新间隔：识别回调远快于渲染需要，合批后只落最后一次
const VOICE_TEXT_FLUSH_MS = 100;

// 流式渲染节奏：逐字推进的帧间隔(ms)、追赶系数、单帧推进上限、
// 滚动跟随的去抖间隔(ms)。
// 逐字而非整段落视图：token 到达速度远快于人的阅读速度，一次性铺满会让
// 文字「跳」出来；这里按帧揭示，正常节奏下每帧 1 字，服务端一次给一大段时
// 按积压比例加速，把延迟稳定在半秒左右，既不卡顿也不会拖成长篇打字动画
const TYPE_TICK_MS = 28;
const TYPE_CATCHUP = 18;
const TYPE_MAX_STEP = 12;
const SCROLL_FOLLOW_MS = 80;

// 加载态节奏：文案跟着真实耗时推进，先给一句轻的，超过阈值才承认「这次慢」。
// 阈值按用户能忍的节奏定——8 秒内没有任何解释会觉得卡死，20 秒以上必须给台阶
const LOAD_SEARCH_MS = 1600;   // 理解 → 检索：首个 token 迟迟不来时换成交代动作
const LOAD_LONG_MS = 12000;    // 检索 → 核对整理：进入「耗时较长」档
const LOAD_FINAL_MS = 26000;   // 核对 → 仍在整理：超长档，补一句安抚
// 生成停顿：一帧打完、下一段 token 还没到，隔这么久才补「正在继续生成」，
// 正常节奏下这句话不会亮，避免全程挂一条多余说明
const STREAM_STALL_MS = 1200;

// 加载态文案表。只写「我们确实在做的事」，不承诺剩余时间、不催用户等待
const LOAD_COPY = {
  think: { text: '正在理解你的问题' },
  search: { text: '正在查找行情与资料' },
  read: { text: '正在整理查到的资料' },
  long: { text: '正在核对数据并组织回答', hint: '这次要查的资料比较多' },
  final: { text: '还在整理，内容比平时多', hint: '内容较长时会慢一些，请再稍等' },
  retry: { text: '连接断开了，正在重连', hint: '网络恢复后会自动接着回答' }
};

Page({
  data: {
    drawerTop: 44,
    input: '',
    sending: false,
    padBottom: 0,
    // 输入栏容器的实际下内边距：键盘弹起时安全区在键盘之下，不再留白
    senderPadBottom: 0,
    // 键盘弹起时输入栏容器向上偏移的距离（px）
    keyboardOffset: 0,
    chatHeight: 0,
    drawerOpen: false,
    // 「新对话」按钮的创建中状态：注册会话期间按钮转圈 + 文案换成「创建中…」，
    // 同时兼作连点闸门（一次点击 = 一条会话，见 onNewChat）
    newChatting: false,
    // 对话区滚动位置：用数值 scroll-top 控制。
    // 旧写法 scroll-into-view 每次都要「先清空锚点再置位」，会不停重启滚动动画。
    // 现在只有「用户主动发消息 / 切换会话」这类显式场景才定位到底部；
    // AI 回复期间不再跟随，新内容向下溢出，视口停在用户当前看的位置
    scrollTop: 0,
    scrollAnimated: false,
    menus: MENUS,
    // 历史对话列表（抽屉）：{ session_id, title, updated_at, active }，
    // active 标记当前会话（选中效果），loadSessions 时按 _sessionId 计算
    sessions: [],
    // 删除会话确认弹窗（胶囊弹层，替代原先的原生 wx.showModal）
    deleteVisible: false,

    // 输入模式：false = 键盘输入，true = 语音输入（模式键在两者之间切换）
    voiceMode: false,
    // textarea 的受控 focus：切回键盘态时靠它主动唤起键盘
    inputFocus: false,
    // 语音输入：recording = 正在聆听（再点一下收尾并发出）
    recording: false,
    voiceTip: VOICE_TIP_IDLE,
    // 聆听中实时识别出的文字：显示在输入栏上方的预输入框里
    voiceText: '',
    // 空会话不铺问候气泡：首屏直接交给下方的引导问题
    messages: [],
    // 首屏引导问题：onLoad 里随机生成（行情 / 资金 / 估值三类意图），
    // 点一条即发送，省掉「我能问什么」这一轮思考
    quickAsks: [],
    // ===== 加载态（发送后到首个 token 到达前）=====
    // 只用一个块表达：占在 AI 回复该出现的位置，文案随阶段变化。
    // 拆成「思考气泡 + 底部状态条」两处会出现两套动效同时转的情况
    loadVisible: false,
    loadText: '',
    // 长耗时档才出现的第二行说明；平时为空，块高不会多占一行
    loadHint: '',
    // 'warn' = 断线重连：文案与动效都降下来，等网络而不是等模型
    loadTone: '',
    // 生成中光标之后的补充说明：只在正文停顿（等服务端下一段）时亮起
    streamStalled: false,
    streamStallText: ''
  },

  onLoad(options) {
    // 先铺本地兜底，再用后端结果替换：接口慢或失败时首屏不留白
    this.setData({ quickAsks: buildFallbackQuickAsks() });

    // 每个对话页实例 = 一条独立会话：
    // - 带 ?session=<id>（新对话注册后压栈 / 历史列表点选）：复用该会话，
    //   后端 thread_id 由它决定，打开即恢复这条对话的上下文；
    // - 不带参（首页进入）：现场生成一条，首条消息发出时才进历史列表
    const resumeSid = options && options.session
      ? decodeURIComponent(options.session)
      : '';
    // ?new=1 表示这是「新对话」刚压栈出来的空会话：会话是这一刻才注册的，
    // 消息必为空，不需要回拉（回拉还要多等一个请求，且空结果会覆盖首屏）
    const isNewChat = !!(options && String(options.new) === '1');
    this._sessionId = resumeSid || agentSocket.freshSessionId();
    // 会话 id 定了才能拉后端建议：去重是按 session 分桶的
    this.loadQuickAsks();
    // 本页发起的最新一轮 rid：agent 单例的请求级事件全带 __rid，
    // 只渲染自己发起的那轮，旧页在途流的 token 不会串进本页
    this._myReqId = 0;
    // 抽屉收起动画是否已完成：新对话跳转要等它，否则新页面会盖掉动画。
    // 初始为 true（页面刚进、抽屉本来就是关的，没有动画要等）
    this._drawerClosed = true;
    // 本页挂在 agent 单例上的监听（event -> callback），onUnload 按引用精确摘除
    this._agentHandlers = {};

    let statusBarHeight = 44;
    let safeBottom = 0;
    let windowHeight = 667;
    let windowWidth = 375;
    try {
      const win = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      statusBarHeight = win.statusBarHeight || 44;
      windowHeight = win.windowHeight || 667;
      windowWidth = win.windowWidth || 375;
      safeBottom =
        (win.safeAreaInsets && win.safeAreaInsets.bottom) ||
        (win.screenHeight && win.safeArea ? win.screenHeight - win.safeArea.bottom : 0) ||
        0;
    } catch (e) {
      // 保持默认值
    }

    // 导航栏高度 = 状态栏 + 44px 内容区（自研 navbar 高度确定，无需实测）
    const navH = statusBarHeight + 44;
    const padBottom = Math.max(0, safeBottom);
    // rpx → px：chatHeight 是按 px 算的，设计稿的 rpx 要先换算
    const rpx = windowWidth / 750;
    // 输入栏让出的空间（首帧估算值，onReady 里会按实测值修正）：
    // 胶囊 + 上下留白 + 与对话区的间距 + 底部余量，安全区内边距在下面单独扣
    const senderH =
      (SENDER_BAR_RPX + SENDER_PAD_TOP_RPX + SENDER_BAR_MARGIN_BOTTOM_RPX + SENDER_GAP_RPX) * rpx +
      SENDER_MARGIN_BOTTOM_PX;

    this._baseH = windowHeight;
    this._curH = windowHeight;
    this._navH = navH;
    this._senderH = senderH;
    this._senderMarginTop = SENDER_MARGIN_TOP_RPX * rpx;
    this._senderGap = SENDER_GAP_RPX * rpx;
    // 键盘弹起时额外上抬的间距（px）：键盘高度是 px，这里一次性换算好
    this._keyboardGap = KEYBOARD_GAP_RPX * rpx;
    this._measureTimer = 0;
    this._sendWatchdog = 0;
    this._kbHeight = 0;
    // 流式输出追踪：当前 streaming 消息在 messages 里的下标（-1 表示无）、
    // 已收到的全文、已揭示到第几个字、逐字推进的定时器
    this._streamIndex = -1;
    this._streamText = '';
    this._streamShown = 0;
    this._typeTimer = 0;
    // 加载态：本轮开始时刻、工具阶段（running/done）、三档定时器、生成停顿定时器
    this._loadAt = 0;
    this._toolLabel = '';
    this._toolPhase = '';
    this._loadTimers = [];
    this._stallTimer = 0;
    // 本轮是否已收到 done（等文字打完才真正收尾）
    this._streamClosing = false;
    // 本轮是否已经开始出正文：正文一出现，加载块就让位（加载文案不再刷）
    this._roundTextStarted = false;
    // 本轮待落位的卡片（最多一条）：整轮结束前先存着，绝不插到正文中间
    this._pendingCard = null;

    // 抽屉内容避让状态栏：statusBarHeight 下方再留 28px（原 44，按需求整体上移一点）。
    // 抽屉在左侧、胶囊菜单在右上，横向不重叠，故减小留白不会撞上菜单按钮
    const drawerTop = statusBarHeight + 28;
    const chatHeight = Math.max(160, windowHeight - navH - senderH - padBottom);

    this.setData({
      drawerTop,
      padBottom,
      senderPadBottom: padBottom,
      keyboardOffset: 0,
      chatHeight
    });
    // 恢复历史会话的消息（空会话保持空列表，首屏只剩引导问题）。
    // 新对话（?new=1）跳过：会话刚注册、必为空，不用多打一次接口。
    // 这里不预先滚到底：空会话滚到底会把问候语顶出屏幕，滚动交给消息回填后
    if (resumeSid && !isNewChat) this.loadSessionMessages(resumeSid);

    if (wx.onWindowResize) {
      this._onResize = (res) => {
        const h = (res && res.size && res.size.windowHeight) || this._baseH;
        if (h === this._curH) return;
        this._curH = h;
        this.applyKeyboard();
      };
      wx.onWindowResize(this._onResize);
    }

    // 接入 Agent SSE：先挂监听，发送时由 agentSocket 内部按提问开流
    this._initAgentSocket();
  },

  // ===== Agent SSE =====
  // agentSocket 是单例；这里只负责挂/摘本页的监听（SSE 为每问一请求，无持久连接）。
  // 新对话页压栈后，单例被多个对话页实例共享，因此：
  // 1. 请求级事件统一走 on() 包一层 rid 过滤——只渲染本页自己发起的那一轮
  //    （__rid 由 services/agent 在 sendMessage 时打下），旧页在途流的
  //    token / 卡片不会串进新页的空会话；
  // 2. 监听引用全部存进 this._agentHandlers，onUnload 按引用精确摘除——
  //    off(event) 不传 callback 会清空该事件的全部监听，把另一个实例的一起误删
  _initAgentSocket() {
    const on = (ev, fn) => {
      const wrapped = (data) => {
        if (!data || data.__rid !== this._myReqId) return;
        fn(data);
      };
      this._agentHandlers[ev] = wrapped;
      agentSocket.on(ev, wrapped);
    };

    // 流式 token：逐字追加到当前 streaming 状态的 assistant 消息。
    // 性能约束：token 频率很高，不能每次都把整个 messages 回传给视图层——
    // 增量先积在 _streamText 里，按帧（约 100ms）合批后用路径级 setData
    // （messages[i].content[0].data）只更新那一段文本
    on('token', (data) => {
      const delta = (data && data.delta) || '';
      if (!delta) return;
      this._appendToken(delta);
    });

    // 结构化卡片：八类（事件契约见 agent 的 _emit_structured 与 server/README.md）。
    // 后端一次回复只发一张，这里先暂存，等正文逐字打完再落位：
    // 文字还在长、卡片已经蹦出来，注意力会被劈成两半
    on('card', (data) => {
      if (!data || !data.payload) return;
      let msg = null;
      if (data.type === 'quote_card') msg = quoteCard(data.payload);
      else if (data.type === 'valuation_card') msg = valuationCard(data.payload);
      else if (data.type === 'fundflow_card') msg = fundflowCard(data.payload);
      else if (data.type === 'report_card') msg = reportCard(data.payload);
      else if (data.type === 'compare_card') msg = compareCard(data.payload);
      else if (data.type === 'research_card') msg = researchCard(data.payload);
      else if (data.type === 'event_card') msg = eventCard(data.payload);
      if (!msg) return;
      this._queueCard(msg);
    });

    // K 线表格：payload 约定为 { rows: [...] }，兼容直接给数组；
    // 同轮查过行情时后端会补上 code / name，一并交给卡片当标题
    on('table', (data) => {
      if (!data || data.type !== 'kline_table' || !data.payload) return;
      const rows = data.payload.rows || data.payload;
      if (!Array.isArray(rows) || !rows.length) return;
      const msg = klineCard(rows, data.payload);
      if (!msg) return;
      this._queueCard(msg);
    });

    // 后端主动推的状态提示：当作本阶段的文案（优先级低于长耗时档）
    on('status', (data) => {
      const label = (data && data.label) || '';
      if (!label) return;
      this._toolLabel = label;
      this._toolPhase = 'running';
      this._refreshLoad();
    });

    // 工具调用/结束：只驱动加载态文案。取数进度不再用占位卡表达——
    // 卡片一律排在正文之后，占位卡没有能站住的位置。
    // 取数回来到首个 token 之间是模型在组织语言，换成「正在整理」
    on('tool_start', (data) => {
      this._toolLabel = (data && data.label) || '正在获取数据';
      this._toolPhase = 'running';
      this._refreshLoad();
    });

    on('tool_end', () => {
      this._toolLabel = '';
      this._toolPhase = 'done';
      this._refreshLoad();
    });

    // 错误：不再逐字往外吐，已收到的正文一次性显示完再收尾；
    // 已经暂存的卡仍然放出来（数据是好的），别让用户白等一轮
    on('error', (data) => {
      this._snapStreamEnd();
      this._finishStreaming();
      this._clearSendWatchdog();
      this._endLoad();
      this.setData({ sending: false });
      this._drainCards();
      this.toast((data && data.msg) || '服务异常');
    });

    // 本轮回答结束：先记下「该收尾了」，等逐字打完再真正结束（_endTurn）。
    // sending 也在那时才解锁——解锁早了，用户能在正文还在打的时候发新问题，
    // 新一轮 token 会续进上一条气泡里
    on('done', () => {
      this._clearSendWatchdog();
      this._streamClosing = true;
      this._endLoad();
      if (this._streamIndex < 0 || this._streamShown >= this._streamText.length) {
        this._endTurn();
        return;
      }
      this._startTyping();   // 等新 token 期间定时器会停，这里兜底续上
    });

    // 断连：连接级事件（不属于某一轮），不做 rid 过滤；
    // 只在正在等回答时给个提示，重连由连接层自动完成。
    // 文案切到「重连」档并保留在界面上——正文已经开始时也让这行留在末尾，
    // 否则用户只会看到文字突然不动，不知道发生了什么
    this._agentHandlers.disconnected = () => {
      if (!this.data.sending) return;
      // 同一时刻只说一件事：断线时撤掉「继续生成」那句，别让两条等待文案叠着
      this._clearStreamStall();
      this.setData({ loadTone: 'warn', streamStalled: false, streamStallText: '' });
      this._refreshLoad();
    };
    agentSocket.on('disconnected', this._agentHandlers.disconnected);

    agentSocket.connect();
  },

  // 把增量文本积进缓冲；首个 token 到达时确定目标消息（没有 streaming 消息
  // 就新建一条）。token 本身不直接落视图，交给逐字定时器按帧揭示
  _appendToken(delta) {
    // 已经有内容在回来，说明这一轮是活的，撤掉看门狗
    this._clearSendWatchdog();
    // 首个 token 到达即收起加载块（断线提示也一并撤），交给流式正文；
    // 之后每来一段都会清掉「停顿说明」，文字一直在动就不需要那句补充
    this._endLoad();
    if (this._streamIndex < 0) {
      const messages = this.data.messages;
      const last = messages[messages.length - 1];
      if (last && last.role === 'assistant' && last.status === 'streaming') {
        this._streamIndex = messages.length - 1;
        this._streamText = (last.content[0] && last.content[0].data) || '';
        this._streamShown = this._streamText.length;
      } else {
        this._streamIndex = messages.length;
        this._streamText = '';
        this._streamShown = 0;
        this.setData({
          messages: messages.concat([
            {
              role: 'assistant',
              status: 'streaming',
              content: [{ type: 'markdown', data: '' }],
              // 流式消息同样带 blocks：每帧重解析，版式随文本逐字收敛
              blocks: []
            }
          ])
        });
      }
    }
    this._streamText += delta;
    this._roundTextStarted = true;
    this._startTyping();
  },

  // 逐字推进的驱动：只在没有定时器时挂下一帧，token 再密也不会越排越多
  _startTyping() {
    if (this._typeTimer || this._unloaded) return;
    this._typeTimer = setTimeout(() => {
      this._typeTimer = 0;
      this._typeTick();
    }, TYPE_TICK_MS);
  },

  // 一帧：按积压量决定这一帧揭示几个字，重绘后继续下一帧。
  // 积压归零即停帧（等下一个 token 拉起）：done 已经来过就收尾整轮。
  // 卡片不在这里放行——打字追上 token 只是缓冲空了，正文随时还会续写，
  // 落卡一律等到整轮结束（见 _drainCards）
  _typeTick() {
    if (this._streamIndex < 0) return;
    const total = this._streamText.length;
    const backlog = total - this._streamShown;
    if (backlog <= 0) {
      if (this._streamClosing) this._endTurn();
      else {
        // 字已经追平、下一段还没来：从这里开始计停顿，真停了才补一句说明
        this._watchStreamStall();
      }
      return;
    }
    const step = Math.min(TYPE_MAX_STEP, Math.max(1, Math.ceil(backlog / TYPE_CATCHUP)));
    this._streamShown = Math.min(total, this._streamShown + step);
    this._renderStream();
    this._startTyping();
  },

  // 把当前应显示的文本落到视图：文本与 markdown 块同一次 setData 写入，
  // 避免出现「文字新、版式旧」的一帧。
  // 这里刻意不跟随滚动：正文逐字变长时只让它向下溢出，视口停在用户当前看的位置。
  // 跟着底部走会变成「每帧把页面往下推」，读上一段时视线一直被拽走。
  _renderStream() {
    const idx = this._streamIndex;
    if (idx < 0) return;
    const text = this._streamText.slice(0, this._streamShown);
    const patch = {};
    patch['messages[' + idx + '].content[0].data'] = text;
    patch['messages[' + idx + '].blocks'] = parseMarkdown(text);
    this.setData(patch);
  },

  // 立刻把剩下的字全部显示：出错、切会话、新一轮发送这类不能等打字机的场合
  _snapStreamEnd() {
    if (this._typeTimer) {
      clearTimeout(this._typeTimer);
      this._typeTimer = 0;
    }
    if (this._streamIndex < 0) return;
    if (this._streamShown >= this._streamText.length) return;
    this._streamShown = this._streamText.length;
    this._renderStream();
  },

  // 流式消息收尾：status 转 complete，复位流式追踪状态。
  // 不碰 sending：解锁发送只在 _endTurn 里做（正文打完才放开）
  _finishStreaming() {
    const idx = this._streamIndex;
    this._streamIndex = -1;
    this._streamText = '';
    this._streamShown = 0;
    if (idx < 0) return;
    const patch = {};
    patch['messages[' + idx + '].status'] = 'complete';
    this.setData(patch);
  },

  // ===== 卡片落位时序：暂存 -> 正文打完 -> 插到末尾 =====

  // 收下后端下发的那张卡。一轮只留一张：真有多张也只用第一张，
  // 与后端「一次回复最多一张」的产品规则对齐
  _queueCard(msg) {
    if (!this._pendingCard) this._pendingCard = msg;
    this._drainCards();
  },

  // 落位。只认一个闸门：整轮结束（done 已到、正文逐字打完、sending 已解锁）。
  // 「这一帧把缓冲区放完了」不等于「正文写完了」——后端在开始生成正文之前
  // 就下发卡片，若在打字追上 token 的空档插卡，视觉上就是「先卡片、后文字」。
  _drainCards() {
    if (!this._pendingCard) return;
    if (this.data.sending) return;
    const msg = this._pendingCard;
    this._pendingCard = null;
    this._placeCard(msg);
  },

  // 一轮真正结束：收尾流式消息、放行暂存的卡片，最后才解锁发送
  // （解锁早了会把新一轮 token 续进这条旧气泡）
  _endTurn() {
    this._streamClosing = false;
    this._snapStreamEnd();
    this._finishStreaming();
    // 先解锁发送（「等正文」那道闸门随之打开），再落卡
    this._endLoad();
    this.setData({ sending: false });
    this._drainCards();
  },

  // ===== 加载态 =====
  // 一轮回答只有一处加载提示：AI 回复该出现的左侧（.ai-load）。
  // 文案按优先级取一档：重连 > 长耗时 > 工具取数 > 整理 > 检索 > 理解
  _startLoad() {
    this._loadAt = Date.now();
    this._toolLabel = '';
    this._toolPhase = '';
    this._clearLoadTimers();
    this._refreshLoad();
    // 三档定时器只管「到点重算一次」，不做文案队列：
    // 工具事件随时会插入，队列式推进会与真实阶段打架
    this._loadTimers = [
      setTimeout(() => this._refreshLoad(), LOAD_SEARCH_MS),
      setTimeout(() => this._refreshLoad(), LOAD_LONG_MS),
      setTimeout(() => this._refreshLoad(), LOAD_FINAL_MS)
    ];
  },

  _refreshLoad() {
    if (this._unloaded || !this.data.sending) return;
    // 正文已经在出：加载块让给流式消息（断线除外，见下）
    if (this._roundTextStarted && this.data.loadTone !== 'warn') return;
    const elapsed = Date.now() - (this._loadAt || Date.now());
    let copy;
    if (this.data.loadTone === 'warn') copy = LOAD_COPY.retry;
    else if (elapsed >= LOAD_FINAL_MS) copy = LOAD_COPY.final;
    else if (elapsed >= LOAD_LONG_MS) copy = LOAD_COPY.long;
    else if (this._toolPhase === 'running' && this._toolLabel) copy = { text: this._toolLabel };
    else if (this._toolPhase === 'done') copy = LOAD_COPY.read;
    else if (elapsed >= LOAD_SEARCH_MS) copy = LOAD_COPY.search;
    else copy = LOAD_COPY.think;
    this.setData({
      loadVisible: true,
      loadText: copy.text,
      loadHint: copy.hint || ''
    });
  },

  _endLoad() {
    this._clearLoadTimers();
    this._clearStreamStall();
    if (!this.data.loadVisible && !this.data.streamStalled && !this.data.loadTone) return;
    this.setData({
      loadVisible: false,
      loadText: '',
      loadHint: '',
      loadTone: '',
      streamStalled: false,
      streamStallText: ''
    });
  },

  _clearLoadTimers() {
    // 每个 token 都会走到这里，空数组时直接返回（token 频率高，别做无意义清理）
    if (!this._loadTimers || !this._loadTimers.length) return;
    this._loadTimers.forEach(clearTimeout);
    this._loadTimers = [];
  },

  // 正文停顿：字追平了但这一轮还没结束，说明在等服务端下一段。
  // 真停够 STREAM_STALL_MS 才亮一句说明，正常节奏下不会出现
  _watchStreamStall() {
    if (this._stallTimer || this._unloaded || this._streamIndex < 0) return;
    this._stallTimer = setTimeout(() => {
      this._stallTimer = 0;
      if (this._unloaded || this._streamIndex < 0) return;
      if (this._streamShown < this._streamText.length) return;
      this.setData({
        streamStalled: true,
        // 长回答的停顿更让人怀疑是不是卡住，文案点明「还在写」而不是「卡住了」
        streamStallText: this._streamText.length > 400 ? '回答较长，正在继续输出…' : '正在继续生成…'
      });
    }, STREAM_STALL_MS);
  },

  _clearStreamStall() {
    if (this._stallTimer) {
      clearTimeout(this._stallTimer);
      this._stallTimer = 0;
    }
  },

  // 卡片插到消息列表末尾：落位时整轮正文已经写完，所以卡片永远排在这轮文字之后。
  // 同样不滚动：整轮结束也不是「回到底部」的理由，视口留在用户正在读的地方
  _placeCard(msg) {
    this.setData({ messages: this.data.messages.concat([msg]) });
  },

  onReady() {
    // 输入栏高度按实测修正（多行输入会长高，首帧估的是两行高度）；
    // 导航栏高度确定（状态栏 + 44），无需测量
    this.measureSender();
  },

  onUnload() {
    // 置位后录音回调直接放弃结果，不再往已销毁的页面上 setData
    this._unloaded = true;
    // 摘监听放在最前：后面的音频 destroy 在开发者工具模拟器里会抛异常
    // （InnerAudioContext 从未播放过时其内部实例不存在），异常会中断 onUnload，
    // 监听清理必须在此之前完成，否则旧页会一直挂在单例上收事件
    // 必须按引用逐个摘：不传 callback 的 off() 会清空该事件的全部监听，
    // 新对话压栈场景下会把另一个对话页实例的监听一起误删
    Object.keys(this._agentHandlers || {}).forEach((ev) => {
      agentSocket.off(ev, this._agentHandlers[ev]);
    });
    this._agentHandlers = {};
    if (this._onResize && wx.offWindowResize) wx.offWindowResize(this._onResize);
    if (this._scrollTimer) clearTimeout(this._scrollTimer);
    if (this._measureTimer) clearTimeout(this._measureTimer);
    if (this._typeTimer) clearTimeout(this._typeTimer);
    this._clearSendWatchdog();
    this._clearLoadTimers();
    this._clearStreamStall();
    // 语音：走的时候停干净，否则插件还在后台录音
    if (this._recording) abortRecognize();
    if (this._voiceTextTimer) {
      clearTimeout(this._voiceTextTimer);
      this._voiceTextTimer = 0;
    }
  },

  // sender-wrap 是绝对定位容器（自成 BFC，子元素外边距不会合并出去），
  // 它的高度 = 卡片上外边距 + 卡片 + 卡片下外边距 + 容器的下内边距。
  // 扣掉上外边距与下内边距（padBottom 在对话区高度里另算）就是卡片 + 下边距，
  // 再加上设计间距，才是对话区真正要让出的空间。
  // 注意下内边距要读实时的 senderPadBottom：键盘弹起时它是 0
  measureSender() {
    if (!wx.createSelectorQuery) return;
    wx.createSelectorQuery()
      .in(this)
      .select('.sender-wrap')
      .boundingClientRect((rect) => {
        if (!rect || !rect.height) return;
        this._senderH =
          rect.height - this.data.senderPadBottom - this._senderMarginTop + this._senderGap;
        this.applyKeyboard();
      })
      .exec();
  },

  // 输入多行时卡片会长高（textarea 开了 auto-height），键盘收放后重测一次。
  // 这里做节流：连续触发只保留最后一次，且测量本身不会再触发新任务，不会成环
  scheduleMeasureSender() {
    if (this._measureTimer) clearTimeout(this._measureTimer);
    this._measureTimer = setTimeout(() => {
      this._measureTimer = 0;
      this.measureSender();
    }, 320);
  },

  // 输入事件（textarea）
  onInput(e) {
    this.setData({ input: (e && e.detail && e.detail.value) || '' });
    // 单行 -> 两行时胶囊会变高，对话区要让出相应空间，否则最后一条消息会被压住。
    // 这里走节流测量：连续打字只在停顿时量一次
    this.scheduleMeasureSender();
  },

  // focus 由受控字段回写：切回键盘态时要靠它主动唤起键盘，
  // 用户自己点输入框时也会走到这里，保持视图与状态一致
  onInputFocus() {
    if (!this.data.inputFocus) this.setData({ inputFocus: true });
  },

  onInputBlur() {
    if (this.data.inputFocus) this.setData({ inputFocus: false });
  },

  // 键盘 / 语音互切。语音态下整块不渲染 textarea，键盘随之收起；
  // 再点一次切回键盘态，并主动聚焦把键盘唤起来
  onToggleInputMode() {
    // 聆听中点键盘键：先丢弃这次识别再切，否则终稿会落进键盘态的输入框
    if (this._recording) this.onVoiceCancel();

    if (this.data.voiceMode) {
      this.setData({ voiceMode: false, inputFocus: true });
      return;
    }

    this.setData({ voiceMode: true, inputFocus: false, voiceTip: VOICE_TIP_IDLE });
    // 组件移除后键盘并不会立刻收，这里显式收一次
    if (wx.hideKeyboard) wx.hideKeyboard();
    // 键盘高度事件不一定补发 0，手动归零，防止输入栏悬在半空
    this._kbHeight = 0;
    this.applyKeyboard();
  },

  onSend() {
    // 语音态胶囊里没有文字可发：这个键的语义是「收尾并发出」，
    // 只在聆听中有效（未聆听时是弱色，点了不做任何事）
    if (this.data.voiceMode) {
      if (this._recording) this.stopVoiceAndSend();
      return;
    }
    this.sendText(this.data.input);
  },

  // 拉后端建议问题：失败保留本地兜底，不 toast、不打断首屏
  loadQuickAsks() {
    listQuickAsks(4, this._sessionId || '')
      .then((data) => {
        const list = (data && data.quick_asks) || [];
        if (!list.length) return;
        // 用户已经开口就不再替换，避免把建议塞进对话中段
        if (this.data.messages.length || this.data.sending) return;
        this.setData({ quickAsks: list });
      })
      .catch(() => {
        // 兜底已在 onLoad 铺好，这里静默
      });
  },

  // 首屏引导问题：展示用 display，真正发出去的是 query（带股票代码与维度，信息更完整）
  onQuickAsk(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    const q = ds.q || '';
    if (!q) return;
    // 点击上报只服务热度池，失败不影响发问，不等它的结果
    if (ds.code) reportQuickAskClick(ds.code, ds.id || '').catch(() => {});
    this.sendText(q);
  },

  // 发送一条文本：打字与语音识别结果走同一条链路，
  // 避免「识别出来的话」和「打出来的话」在界面上表现不一致
  sendText(raw) {
    const text = (raw || '').trim();
    if (!text || this.data.sending) return;

    // 上一轮先落定（正常情况下 _endTurn 已经收过尾，这里是兜底）：
    // 剩余文字一次性铺完，上一轮没落位的卡片作废
    this._snapStreamEnd();
    this._finishStreaming();
    this._pendingCard = null;

    this._roundTextStarted = false;
    this.setData({
      messages: this.data.messages.concat([userMsg(text)]),
      input: '',
      sending: true
    });
    // 加载块与用户消息同帧落位：先发一句「正在理解」，别让界面空着等
    this._startLoad();
    this.scrollToBottom();
    this._armSendWatchdog();

    // 通过 SSE 发送：每次提问开一条独立流；传入本页 session_id（后端据此拼 thread_id），
    // 并记下本轮 rid，回来的事件只渲染本页发起的这一轮
    this._myReqId = agentSocket.sendMessage(text, this._sessionId);
  },

  // 发送看门狗：sending 只在 done / error 复位；服务没起或连接始终没建立时这两个事件
  // 永远不来，sending 一直为 true、后续发送被静默吞掉（表现为「点了发送没反应」）。
  // 收到第一个 token 即说明链路活着，立刻撤掉（见 _appendToken）
  _armSendWatchdog() {
    this._clearSendWatchdog();
    this._sendWatchdog = setTimeout(() => {
      this._sendWatchdog = 0;
      if (!this.data.sending) return;
      // 超时收尾同 error：剩余文字直接铺完；卡片数据是好的，照常落位
      this._snapStreamEnd();
      this._finishStreaming();
      this._endLoad();
      this.setData({ sending: false });
      this._drainCards();
      this.toast('服务暂时没有响应，请重试');
    }, SEND_TIMEOUT_MS);
  },

  _clearSendWatchdog() {
    if (this._sendWatchdog) {
      clearTimeout(this._sendWatchdog);
      this._sendWatchdog = 0;
    }
  },

  // 键盘弹起后的布局：输入栏容器整体上移 kb，对话区同步收窄 kb，
  // 两者共用同一个键盘高度，保证卡片与对话区的间距始终是设计稿的 29rpx
  applyKeyboard() {
    const alreadyShifted = Math.max(0, this._baseH - this._curH);
    // 键盘弹起时窗口可能已被系统收缩（Android 会 resize，iOS 不 resize），
    // 减掉已收缩的部分，否则键盘高度会被重复计算
    const kb = Math.max(0, (this._kbHeight || 0) - alreadyShifted);
    // 输入栏的上抬量 = 键盘高度 + 固定间距：只抬键盘高度的话胶囊会贴着键盘顶边
    const lift = kb > 0 ? kb + (this._keyboardGap || 0) : 0;
    // 键盘弹起时安全区落在键盘之下，输入栏不再为它留白
    const senderPad = kb > 0 ? 0 : this.data.padBottom;
    const availH = this._curH - lift;
    // 兜底值取小值：兜得太高会让对话区反向溢出到输入栏上，把卡片盖住
    const chatHeight = Math.max(80, availH - this._navH - this._senderH - senderPad);
    if (
      chatHeight === this.data.chatHeight &&
      lift === this.data.keyboardOffset &&
      senderPad === this.data.senderPadBottom
    ) {
      return;
    }
    this.setData({ chatHeight, keyboardOffset: lift, senderPadBottom: senderPad });
  },

  onKeyboardHeightChange(e) {
    const height = (e && e.detail && e.detail.height) || 0;
    const kb = height > 0 ? height : 0;
    if (kb === this._kbHeight) return;
    this._kbHeight = kb;
    this.applyKeyboard();
    // 输入过程中卡片会随行数变高（textarea 开了 auto-height），键盘收/起稳定后再量一次
    this.scheduleMeasureSender();
  },

  /* ===== 语音输入 =====
     点一下开始聆听（预输入框实时出字），再点一下把终稿直接发给 agent */

  // 语音键的唯一入口：未在聆听就开录，正在聆听就收尾并发出
  onVoiceTap() {
    if (this._authorizing) return; // 授权回调还没回来，忽略连点
    if (this._recording) {
      this.stopVoiceAndSend();
      return;
    }
    if (this.data.sending) {
      this.toast('上一条还在回答，稍等一下');
      return;
    }
    this.beginVoice();
  },

  // 开录前确认麦克风权限：之前拒绝过的话 authorize 会直接失败，
  // 那时只能让用户去设置页打开，这里给个明确出口，别点了没反应
  beginVoice() {
    this._authorizing = true;
    wx.authorize({
      scope: 'scope.record',
      success: () => {
        this._authorizing = false;
        if (this._unloaded) return;
        this.startRecord();
      },
      fail: () => {
        this._authorizing = false;
        this.setData({ voiceTip: VOICE_TIP_IDLE });
        wx.showModal({
          title: '需要麦克风权限',
          content: '语音输入需要麦克风权限，是否去设置页开启？',
          confirmText: '去设置',
          success: (r) => {
            if (r.confirm) wx.openSetting();
          }
        });
      }
    });
  },

  // 再点一下：停止录音，终稿回来后由 onVoiceStop 直接发出。
  // _stopping 挡连点：等终稿的这段时间里再点会重复 stop，插件侧会抛错
  stopVoiceAndSend() {
    if (this._stopping) return;
    this._stopping = true;
    this.setData({ voiceTip: VOICE_TIP_STOP });
    stopRecognize();
  },

  // 取消：丢弃本次结果。先置 aborted 再 abort，
  // 否则插件迟到的终稿会把用户刚说的话发出去
  onVoiceCancel() {
    if (!this._recording) return;
    this._recordCanceled = true;
    this._recording = false;
    this._stopping = false;
    this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE });
    this.clearVoiceText();
    abortRecognize();
  },

  // 停录 / 到时长上限的统一收尾：终稿直接发给 agent
  onVoiceStop(res) {
    if (this._unloaded) return;
    const canceled = this._recordCanceled;
    const text = String((res && res.text) || '').trim();
    const ms = (res && res.duration) || Date.now() - (this._recordStartAt || 0);

    this._recording = false;
    this._stopping = false;
    this._recordCanceled = false;
    this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE });
    this.clearVoiceText();

    if (canceled) return;
    // 一个字都没识别出来：当误触，不发出空消息
    if (!text) {
      this.toast(ms < REC_MIN_MS ? '说话时间太短' : '没听清，请再说一次', 1200);
      return;
    }
    // 上一轮还没答完：不吞掉这句话，落到输入框并切回键盘，让用户自己决定何时发
    if (this.data.sending) {
      this.setData({ voiceMode: false, input: text, inputFocus: true });
      this.scheduleMeasureSender();
      this.toast('上一条还在回答，已填到输入框');
      return;
    }
    this.sendText(text);
  },

  onVoiceError(message) {
    if (this._unloaded) return;
    const canceled = this._recordCanceled;

    this._recording = false;
    this._stopping = false;
    this._recordCanceled = false;
    this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE });
    this.clearVoiceText();
    if (canceled) return;
    this.toast(message || '没听清，请再说一次');
  },

  // 中间结果：只刷新预输入框，不碰输入框（终稿由 onVoiceStop 直接发出）
  onVoicePartial(text) {
    if (this._unloaded) return;
    this._voicePartial = String(text || '');
    if (this._voiceTextTimer) return;
    this._voiceTextTimer = setTimeout(() => {
      this._voiceTextTimer = 0;
      if (this._unloaded) return;
      this.setData({ voiceText: this._voicePartial });
    }, VOICE_TEXT_FLUSH_MS);
  },

  clearVoiceText() {
    if (this._voiceTextTimer) {
      clearTimeout(this._voiceTextTimer);
      this._voiceTextTimer = 0;
    }
    this._voicePartial = '';
    if (this.data.voiceText) this.setData({ voiceText: '' });
  },

  startRecord() {
    this._recording = true;
    this._stopping = false;
    this._recordCanceled = false;
    this._recordStartAt = Date.now();
    this._voicePartial = '';

    this.setData({ recording: true, voiceTip: VOICE_TIP_LIVE, voiceText: '' });

    // 识别全程在插件侧完成：中间结果进预输入框，收尾后 onStop 带终稿回来
    startRecognize(
      {
        onPartial: (text) => this.onVoicePartial(text),
        onFinal: (res) => this.onVoiceStop(res),
        onError: (message) => this.onVoiceError(message)
      },
      { duration: REC_MAX_MS }
    );
  },

  // 滚到底：用数值 scroll-top 定位，不再走 scroll-into-view
  // （旧写法每帧「清空锚点 → 重设锚点」会不停重启滚动动画，画面上下抽动）。
  // 只在显式场景调用：发送新消息、切换/恢复会话、新建会话；
  // AI 流式正文与卡片落位都不调它——回复期间视口保持不动。
  scrollToBottom() {
    // 空会话没有「底部」可看：首屏只有引导问题，滚到底只会把顶部内容顶出屏幕
    if (!this.data.messages.length) return;
    if (this._scrollTimer) return;
    this._scrollTimer = setTimeout(() => {
      this._scrollTimer = 0;
      const query = wx.createSelectorQuery().in(this);
      query.select('.chat-inner').boundingClientRect();
      query.exec((res) => {
        const rect = res && res[0];
        if (!rect || !rect.height) return;
        // 内容总高直接交给 scroll-top，视图层会自动夹到「内容高度 - 容器高度」。
        // 不做「高度没变就跳过」的去重：切换到更短的会话后旧值更大，
        // 跳过会让下一次发消息滚不到底
        this.setData({ scrollTop: Math.ceil(rect.height), scrollAnimated: true });
      });
    }, SCROLL_FOLLOW_MS);
  },

  // ===== 侧边抽屉 =====
  onMenuTap() {
    this._pendingMenu = '';
    this.setData({ drawerOpen: true });
    this.loadSessions(); // 每次打开抽屉刷新历史列表
  },

  // 点行情卡 -> 个股详情。目前只有开发版能进（卡片画廊是假数据页），
  // 体验版/正式版给一句提示而不是跳假数据：真实详情页做完后把 isDev 判断去掉即可。
  // 卡片内没有可点的子元素，bindtap 绑在组件根上即可整卡响应；
  // 没有 code（理论上不会出现）时不跳
  onQuoteCardTap(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    if (!ds.code) return;
    if (!isDev) {
      this.toast('个股详情正在开发中');
      return;
    }
    wx.navigateTo({
      url: '/pages/test/index?code=' + encodeURIComponent(ds.code) +
        (ds.name ? '&name=' + encodeURIComponent(ds.name) : '')
    });
  },

  // 点遮罩关闭抽屉（base-popup 的 mask 事件）。
  // 左滑关闭不走这里（onSwipeMove 直接置 false），两者最终都汇到 drawerOpen
  onDrawerMask() {
    if (this.data.drawerOpen) this.setData({ drawerOpen: false });
  },

  // 抽屉只在主页面存在，子页面没有，因此不保留「当前页」状态，
  // 每一项都是纯粹的跳转入口。
  // 这里只负责「开始收起」并记下要去哪，真正的跳转放到收起动画结束之后：
  // popup 的 transition 是靠定时器收尾的，若同一帧就 navigateTo，
  // 新页面会直接盖上来把收起动画掐断，真机上看着就是「抽屉没收起」
  onMenuSelect(e) {
    this._pendingMenu = e.currentTarget.dataset.menu || '';
    if (!this.data.drawerOpen) {
      // 理论上不会走到这里（菜单只在抽屉展开时可点），兜底直接跳
      this.flushPendingNav();
      return;
    }
    this.setData({ drawerOpen: false });
  },

  // 新对话：先收抽屉、动画结束再跳转，中间多一步「先注册、成功才压栈新对话页」。
  // 三态都反馈到按钮（wxml 按 newChatting 切换）：创建中挡连点、抽屉照旧立即收起；
  // 成功等 onDrawerLeaved 再压栈；失败（网关 502 / 网络异常 / 登录失效）toast 并留在原会话，
  // 宁可让用户再点一次，也不要跳进一条没注册的会话。
  // 兜底：注册失败不影响对话本身，首条消息发出时 Go 网关仍会 registerSession 补注册。
  onNewChat() {
    if (this.data.newChatting) return; // 连点闸门：一次点击 = 一条会话
    const sid = agentSocket.freshSessionId();
    this.setData({ newChatting: true });
    this._pendingMenu = 'new';
    this._pendingSession = sid;
    this._newChatReady = false;
    // 抽屉开着时先记「动画未完成」，等 onDrawerLeaved 才允许跳转
    this._drawerClosed = !this.data.drawerOpen;

    createSession(sid, '').then(() => {
      // 抽屉可能正在收动画、也可能压根没开过，两种情况都交给 _flushNewChat 判定
      this._newChatReady = true;
      this._flushNewChat();
    }).catch(() => {
      this._pendingMenu = '';
      this._pendingSession = '';
      this._newChatReady = false;
      this.setData({ newChatting: false });
      this.toast('新建对话失败，请重试');
    });

    if (this._drawerClosed) {
      // 抽屉本来就是关的：没有动画要等，注册回来后由 then 里的 _flushNewChat 跳
      this._flushNewChat();
      return;
    }
    this.setData({ drawerOpen: false });
  },

  // 新对话的跳转闸门：「注册成功」与「抽屉收起动画结束」两个条件都满足才压栈。
  // 只满足其一时不跳——否则要么跳进一条没注册的会话，要么掐断抽屉的收起动画
  _flushNewChat() {
    if (this._pendingMenu !== 'new' || !this._newChatReady || !this._drawerClosed) return;
    this._pendingMenu = '';
    this._newChatReady = false;
    // 压栈新对话页：旧对话原样留在栈里（可返回继续），新页是独立实例、
    // 独立会话（?session= 带上刚注册的 id，新页 onLoad 据此沿用），
    // 从它进子页返回照样回到它自己。
    // 页面栈上限 10 层，满时 navigateTo 会失败，退化为 reLaunch 重置为一个新对话页
    const sid = this._pendingSession || '';
    this._pendingSession = '';
    this.setData({ newChatting: false });
    const url = NEW_CHAT_URL + (sid ? '&session=' + encodeURIComponent(sid) : '');
    wx.navigateTo({
      url,
      fail: () => wx.reLaunch({ url })
    });
  },

  // popup 收起动画结束（transition mixin 的 leaved 事件）：此刻再跳转，动画已完整播完。
  // 新对话的跳转还要等注册回来，两个条件在 _flushNewChat 里汇合
  onDrawerLeaved() {
    this._drawerClosed = true;
    this.flushPendingNav();
  },

  flushPendingNav() {
    const menu = this._pendingMenu;
    if (menu === 'new') {
      // 新对话不在这里直接跳：注册没回来就压栈，会跳进一条没注册的会话。
      // 交给 _flushNewChat 判定，注册回来后自行补跳
      this._flushNewChat();
      return;
    }
    this._pendingMenu = '';
    const url = menu && ROUTES[menu];
    if (url) wx.navigateTo({ url });
  },

  // ===== 历史对话 =====
  // 拉会话列表并标记当前会话（抽屉里的选中效果）。
  // 失败静默：列表为空只是没历史，不该弹 toast 打断用户
  loadSessions() {
    return listSessions().then((data) => {
      const list = (data && data.sessions) || [];
      const sessions = list.map((s) => Object.assign({}, s, {
        active: s.session_id === this._sessionId
      }));
      this.setData({ sessions });
    }).catch(() => {});
  },

  // 历史列表点选：原地切到该会话并加载它的纯文本消息。
  // 抽屉保持打开——用户还停在抽屉上继续挑，不因为点了一下就被踢回对话区
  onSessionSelect(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    const sid = ds.sid;
    if (!sid) return;
    // 点的就是当前会话：什么都不做，抽屉照旧开着
    if (sid === this._sessionId) return;

    this._abortInFlight();
    this._sessionId = sid;
    this.loadSessions();
    this.loadSessionMessages(sid);
  },

  // 切会话 / 重建会话前掐断在途流：旧会话还在回来的 token、卡片，以及 abort 触发的
  // error 事件，都不该落进新会话的界面（否则上下文就串了）。
  // _myReqId 先归零再断——abort 的 error 打的正是「被掐断那一轮」的戳，
  // 归零后它会被本页的 rid 闸门过滤掉，不会误弹「连接异常」。
  // 只在本页确实有流在跑时才断：单例可能被栈里其它对话页实例共用
  _abortInFlight() {
    if (!this.data.sending) return;
    this._myReqId = 0;
    agentSocket.disconnect();
    this._clearSendWatchdog();
    this._snapStreamEnd();
    this._finishStreaming();
    this._pendingCard = null;
    this._endLoad();
    this.setData({ sending: false });
  },

  // 当前会话被删后原地换一条新会话：不跳页、不收起抽屉，
  // 用户仍停在这一页的抽屉上，只是当前项换成了新的空会话。
  // deletedSid 用于在刷新回来的列表里再兜一次底：删除刚落库，
  // 列表接口可能还带着那条，不滤掉它就会「删了又回来」
  _resetToNewSession(deletedSid) {
    const sid = agentSocket.freshSessionId();
    this._abortInFlight();
    this._sessionId = sid;
    this.setData({ messages: [] });
    this.scrollToBottom();
    // 注册失败不阻断：首条消息发出时 Go 网关仍会 registerSession 补注册
    createSession(sid, '').catch(() => {});
    this.loadSessions().then(() => {
      if (!deletedSid || this._unloaded) return;
      this.setData({
        sessions: (this.data.sessions || []).filter((s) => s.session_id !== deletedSid)
      });
    });
  },

  // 恢复某会话的消息（文本气泡；结构化卡片不持久化，恢复后只有文本）。
  // 失败时清空消息 + 轻提示——宁可显示空会话，也别把上一条对话的
  // 消息留在界面上冒充新会话的内容
  loadSessionMessages(sid) {
    return sessionMessages(sid).then((data) => {
      const list = (data && data.messages) || [];
      const msgs = list.map((m) =>
        (m.role === 'user' ? userMsg(m.content) : assistantMsg(m.content))
      );
      this.setData({ messages: msgs });
      this.scrollToBottom();
    }).catch(() => {
      this.setData({ messages: [] });
      this.toast('会话加载失败');
    });
  },

  // 删除会话：先弹胶囊确认框（与自选股 / 提醒页同一套 base-popup，
  // 不再用原生 wx.showModal——两端样式不一致，也不跟主题走）
  onSessionDelete(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    if (!ds.sid) return;
    this._deletingSid = ds.sid;
    this.setData({ deleteVisible: true });
  },

  cancelDeleteSession() {
    if (!this.data.deleteVisible) return;
    this._deletingSid = '';
    this.setData({ deleteVisible: false });
  },

  // 确认删除：连 checkpoint 与摘要一起清，不可恢复。
  // 删的是当前会话时原地换一条新会话（不跳页）——当前页继续留着只会对着
  // 一堆已不属于任何 thread 的消息发问
  confirmDeleteSession() {
    const sid = this._deletingSid;
    this._deletingSid = '';
    this.setData({ deleteVisible: false });
    if (!sid) return;

    // 先在本地摘掉再打后端：列表接口往往比删除先返回，
    // 那条会话会「删了又冒出来」，看起来就像删不掉
    this.setData({
      sessions: (this.data.sessions || []).filter((s) => s.session_id !== sid)
    });

    const done = () => {
      if (sid === this._sessionId) {
        this._resetToNewSession(sid);
        return;
      }
      this.loadSessions();
    };

    deleteSession(sid).then(done).catch((err) => {
      // 404 = 服务端本来就没有这条（从未注册，或已被删掉过）。
      // 这种情况按「已删除」处理，否则用户永远删不掉一条本地残留
      if (err && Number(err.code) === 404) {
        done();
        return;
      }
      this.toast('删除失败');
      this.loadSessions(); // 回滚：把列表拉回服务端真实状态
    });
  },

  // 轻提示：自研 base-toast（替代 wx.showToast 默认样式）
  toast(message, duration) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message, duration);
  },

  // markdown 链接点击：小程序不能直接打开外部 web 页面，
  // 统一复制链接 + 轻提示（研报 / 事件卡的原文链接合规要求同理）
  onMdLinkTap(e) {
    const href = e.currentTarget.dataset && e.currentTarget.dataset.href;
    if (!href) return;
    wx.setClipboardData({
      data: href,
      success: () => this.toast('链接已复制')
    });
  },

  // ===== 手势开关抽屉：右滑打开，左滑关闭 =====
  // 输入栏内的手势起点标记（wxml 用 bindtouchstart 绑到 .sender-wrap）。
  // 用「标记 + 判断」取代原来的 catch：catch 会让整个输入栏的子元素点击失效，
  // 输入框点不动、模式键和发送键全都不响应。这里只记一个信号，
  // 由 onSwipeStart 决定这次手势是否让给输入（光标移动 / 按住说话）
  onSenderTouchStart() {
    this._inSender = true;
  },

  // 点输入区任意位置都聚焦 textarea：原生组件的点击热区未必铺满整块，
  // 给一个明确的聚焦入口，点空白也能唤起键盘
  onTapInputField() {
    if (!this.data.inputFocus) this.setData({ inputFocus: true });
  },

  onSwipeStart(e) {
    this._startX = e.touches[0].clientX;
    this._startY = e.touches[0].clientY;
    this._lastX = this._startX;
    this._lastY = this._startY;
    // 子元素的 bindtouchstart 先于根节点执行，这里读到的是本次手势的起点归属；
    // 读完即清，下一次手势重新判定
    const inSender = this._inSender;
    this._inSender = false;
    // 起点在输入栏（或正在录音）：这一整段手势都不用于开关抽屉。
    // 录音这条是兜底：按住说话时若滑出按钮范围，事件仍可能落到根节点
    this._swipeLocked = !!this._recording || !!inSender;
  },

  onSwipeMove(e) {
    if (this._recording) return;
    this._lastX = e.touches[0].clientX;
    this._lastY = e.touches[0].clientY;
    if (this._swipeLocked) return;
    const dx = this._lastX - this._startX;
    const dy = this._lastY - this._startY;
    if (Math.abs(dx) < SWIPE_MIN || Math.abs(dx) <= Math.abs(dy)) return;

    this._swipeLocked = true;
    if (dx > 0 && !this.data.drawerOpen) {
      this.setData({ drawerOpen: true });
      this.loadSessions(); // 滑动打开抽屉同样刷新历史列表
    } else if (dx < 0 && this.data.drawerOpen) {
      this.setData({ drawerOpen: false });
    }
  },

  onSwipeEnd() {
    this._swipeLocked = false;
  }
});
