/**
 * 核心纯逻辑单测：数字格式化 / markdown 解析 / SSE 分帧与 UTF-8 增量解码。
 *
 * 这三块是最容易回归、又最难靠手点发现的地方：
 * - format 决定所有金融卡上的数字（错一位小数点看不出来）；
 * - markdown 决定 AI 回答的版式（解析崩了整条气泡空白）；
 * - agent 的分帧决定中文会不会被拆成半个字（跨 chunk 截断只有真流式才暴露）。
 *
 * 运行：npm test（与 newchat 交互测试一起跑）
 */
const assert = require('assert');
const path = require('path');
const { loadModule } = require('./loader');

const ROOT = path.resolve(__dirname, '..', '..');
const cases = [];
const test = (name, fn) => cases.push({ name, fn });

/* ===================== utils/format ===================== */
const fmt = loadModule(path.join(ROOT, 'utils', 'format.js'));

test('fmtNum：千分位 + 固定小数位，非法值降级为 --', () => {
  assert.strictEqual(fmt.fmtNum(1234567.891), '1,234,567.89');
  assert.strictEqual(fmt.fmtNum('1234.5', 1), '1,234.5');
  assert.strictEqual(fmt.fmtNum(null), '--');
  assert.strictEqual(fmt.fmtNum('abc'), '--');
  assert.strictEqual(fmt.fmtNum(''), '--');
});

test('fmtSign / fmtPct：正数带 +，负数带 -，0 不带号', () => {
  assert.strictEqual(fmt.fmtSign(1.2), '+1.20');
  assert.strictEqual(fmt.fmtSign(-0.5), '-0.50');
  assert.strictEqual(fmt.fmtPct(0), '0.00%');
  assert.strictEqual(fmt.fmtPct(-0.65), '-0.65%');
  assert.strictEqual(fmt.fmtPct(null), '--%');
});

test('fmtPctPlain：水平值不带正负号（毛利率不能读成同比）', () => {
  assert.strictEqual(fmt.fmtPctPlain(91.78), '91.78%');
  assert.strictEqual(fmt.fmtPctPlain(-3.2), '-3.20%');
});

test('fmtAmount：亿 / 万 / 万亿 三档换算', () => {
  assert.strictEqual(fmt.fmtAmount(1914800000000), '1.91万亿');
  assert.strictEqual(fmt.fmtAmount(4612000000), '46.12亿');
  assert.strictEqual(fmt.fmtAmount(128600), '12.86万');
  assert.strictEqual(fmt.fmtAmount(-4612000000), '-46.12亿');
  assert.strictEqual(fmt.fmtAmount(999), '999.00');
});

test('direction：>0 up / <0 down / 0 与空 flat', () => {
  assert.strictEqual(fmt.direction(1.2), 'up');
  assert.strictEqual(fmt.direction(-1), 'down');
  assert.strictEqual(fmt.direction(0), 'flat');
  assert.strictEqual(fmt.direction(null), 'flat');
});

/* ===================== utils/markdown ===================== */
const md = loadModule(path.join(ROOT, 'utils', 'markdown.js'), {
  marked: require('marked')
});

test('markdown：空文本返回空数组，不抛错', () => {
  // 用 length 断言而不是 deepStrictEqual：返回值创建在 vm 沙箱里，
  // 与测试进程的 Array 不同源，深比较会因原型不同而失败
  assert.strictEqual(md.parseMarkdown('').length, 0);
  assert.strictEqual(md.parseMarkdown('   ').length, 0);
  assert.strictEqual(md.parseMarkdown(null).length, 0);
});

test('markdown：标题深度与段落', () => {
  const blocks = md.parseMarkdown('## 结论\n\n当前价格 1680.50 元。');
  assert.strictEqual(blocks[0].tag, 'h');
  assert.strictEqual(blocks[0].depth, 2);
  assert.strictEqual(blocks[1].tag, 'p');
  assert.strictEqual(blocks[1].inlines[0].text, '当前价格 1680.50 元。');
});

test('markdown：行内粗体 / 行内代码 / 链接标记', () => {
  const blocks = md.parseMarkdown('涨了 **1.23%**，见 `600519` 与 [原文](https://example.com/a)');
  const inlines = blocks[0].inlines;
  const strong = inlines.filter((n) => n.strong).map((n) => n.text).join('');
  assert.strictEqual(strong, '1.23%');
  const code = inlines.filter((n) => n.code).map((n) => n.text).join('');
  assert.strictEqual(code, '600519');
  const link = inlines.filter((n) => n.href)[0];
  assert.ok(link, '链接片段必须带 href');
  assert.strictEqual(link.href, 'https://example.com/a');
  assert.strictEqual(link.text, '原文');
});

test('markdown：闭合标记后跟中文标点时粗体仍生效（marked 4 边界缺陷兜底）', () => {
  const blocks = md.parseMarkdown('涨幅 **1.23%**，收盘');
  const strong = blocks[0].inlines
    .filter((n) => n.strong)
    .map((n) => n.text)
    .join('');
  assert.strictEqual(strong, '1.23%', '** 不应作为字面量漏到界面上');
  assert.strictEqual(
    blocks[0].inlines.map((n) => n.text).join(''),
    '涨幅 1.23%，收盘'
  );
});

test('markdown：嵌套列表摊平成 depth，子项 depth+1', () => {
  const blocks = md.parseMarkdown('- 一\n  - 一一\n- 二');
  assert.strictEqual(blocks[0].tag, 'ul');
  assert.strictEqual(blocks[0].items.length, 3);
  assert.strictEqual(blocks[0].items[1].depth, 1);
});

test('markdown：连续换行折叠成单次，空行不再撑出整行空白', () => {
  // 还原截图里的松散排版：条目内「小标题 + 空行 + 正文」
  const blocks = md.parseMarkdown('引言：周线三连阴。\n\n- **形态**\n\n  ：周收盘价依次为 1546.63 → 1117。');
  const flat = JSON.stringify(blocks);
  assert.ok(flat.indexOf('\\n\\n') === -1, 'pre-wrap 容器里不得出现空行');
  const body = blocks[1].items[0].inlines.map((n) => n.text).join('');
  assert.strictEqual(body, '形态\n：周收盘价依次为 1546.63 → 1117。', '条目内只保留一次换行');
});

test('markdown：表格拆表头与行，且不再输出无人消费的 align', () => {
  const blocks = md.parseMarkdown('| 指标 | 值 |\n| --- | --- |\n| PE | 24.18 |');
  const table = blocks[0];
  assert.strictEqual(table.tag, 'table');
  assert.strictEqual(table.head.length, 2);
  assert.strictEqual(table.rows.length, 1);
  assert.strictEqual(table.rows[0][1][0].text, '24.18');
  assert.strictEqual(table.align, undefined);
});

test('markdown：原始 HTML 一律丢弃（安全边界）', () => {
  const blocks = md.parseMarkdown('<script>alert(1)</script>\n\n正常文本');
  const flat = JSON.stringify(blocks);
  assert.ok(flat.indexOf('<script>') === -1, '不得把原始 HTML 透给视图层');
});

test('markdown 流式：逐字喂入，定型段落不再重建，收尾结构与整篇解析一致', () => {
  const src = '## 结论\n\n当前价格 **1680.50** 元，成交量放大。\n\n- 支撑位 1650\n- 压力位 1720\n';
  const stream = md.createMarkdownStream();
  let prev = [];
  let r = null;
  for (let i = 1; i <= src.length; i++) {
    r = stream.update(src.slice(0, i));
    // 已定型的段落一个字都不许再变：视图层已经把它们画出来了，
    // 回退或重建就是肉眼看到的「版式抖一下」
    for (let k = 0; k < prev.length; k++) {
      assert.strictEqual(
        JSON.stringify(r.blocks[k]),
        JSON.stringify(prev[k]),
        '第 ' + i + ' 个字时第 ' + k + ' 段被改写'
      );
    }
    assert.ok(r.blocks.length >= prev.length, '定型段落只增不减');
    prev = r.blocks;
  }
  // 对象产在 vm 沙箱里，与本进程不同源，先过一遍 JSON 归一化再深比较
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(r.blocks.concat(r.tail))),
    JSON.parse(JSON.stringify(md.parseMarkdown(src))),
    '流式拼出的结构必须与整篇一次性解析等价'
  );
});

test('markdown 流式：没有新段落定型时复用同一份定型结果', () => {
  const stream = md.createMarkdownStream();
  const a = stream.update('第一段\n\n第二段正在写');
  const b = stream.update('第一段\n\n第二段正在写，还没写完');
  assert.strictEqual(b.blocks, a.blocks, '定型结果不该每帧重建');
  assert.strictEqual(b.committed, false, '没有新空行边界就不算定型');
  assert.strictEqual(b.tail[0].tag, 'p');
});

test('markdown 流式：代码块内部的空行不算段落边界', () => {
  const stream = md.createMarkdownStream();
  // 围栏还没闭合：这个空行不能切，否则代码块会被拆成两个半截
  // （未闭合期间 marked 本就把围栏当段落渲染，这里只校验「什么都没定型」）
  const mid = stream.update('```\na = 1\n\nb = 2');
  assert.strictEqual(mid.blocks.length, 0, '围栏内部不得定型');
  assert.strictEqual(mid.committed, false);
  assert.ok(JSON.stringify(mid.tail).indexOf('b = 2') !== -1, '整段都还在尾段里，没被切走');

  const done = stream.update('```\na = 1\n\nb = 2\n```\n\n收尾');
  const codes = done.blocks.filter((b) => b.tag === 'code');
  assert.strictEqual(codes.length, 1, '围栏闭合后整块一次性定型');
  assert.ok(codes[0].text.indexOf('b = 2') !== -1);
  assert.strictEqual(done.tail[0].tag, 'p');
});

/* ===================== services/agent 分帧 ===================== */
const agent = loadModule(path.join(ROOT, 'services', 'agent.js'), {
  '../config': { apiBase: 'https://x', agentSsePath: '/api/v1/agent/stream' },
  '../utils/request': { getToken: () => 't', ensureLogin: () => Promise.resolve('t') }
});

test('agent：中文被拆到两个 chunk 时能拼回完整字符', () => {
  const bytes = Buffer.from('贵州茅台现在 1680.50 元', 'utf8');
  agent._pending = new Uint8Array(0);
  // 故意在第 4 个字节（一个汉字的中间）切开
  const a = agent._decodeChunk(new Uint8Array(bytes.subarray(0, 4)));
  const b = agent._decodeChunk(new Uint8Array(bytes.subarray(4)));
  assert.strictEqual(a + b, '贵州茅台现在 1680.50 元');
});

test('agent：一帧被拆成两个 chunk 时按 \\n\\n 正确合帧', () => {
  const got = [];
  agent.on('token', (d) => got.push(d.delta));
  agent._buffer = '';
  agent._pending = new Uint8Array(0);
  agent._buffer += 'event: token\ndata: {"delta":"你"}\n\nevent: tok';
  agent._drain();
  agent._buffer += 'en\ndata: {"delta":"好"}\n\n';
  agent._drain();
  assert.deepStrictEqual(got, ['你', '好']);
});

test('agent：事件带上本次请求的 __rid，ping 不下发', () => {
  const seen = [];
  const pings = [];
  agent.on('status', (d) => seen.push(d));
  agent.on('ping', (d) => pings.push(d));
  agent._buffer = 'event: status\ndata: {"label":"正在取数"}\n\nevent: ping\ndata: {}\n\n';
  agent._drain();
  assert.strictEqual(seen.length, 1);
  assert.strictEqual(seen[0].label, '正在取数');
  assert.strictEqual(seen[0].__rid, agent.currentRequestId());
  assert.strictEqual(pings.length, 0, '心跳不该进页面');
});

test('agent：null 数据不会让页面侧取字段时抛错', () => {
  const got = [];
  agent.on('tool_end', (d) => got.push(d));
  agent._buffer = 'event: tool_end\ndata: null\n\n';
  agent._drain();
  assert.strictEqual(got.length, 1);
  assert.ok(got[0] && typeof got[0] === 'object');
});

/* ===================== pages/chat/cards 映射 ===================== */
const cards = loadModule(path.join(ROOT, 'pages', 'chat', 'cards.js'), {
  '../../utils/markdown': { parseMarkdown: (t) => [{ type: 'text', text: t }] }
});

test('cards.quoteCard：字符串数字转 number，成交额万元→元', () => {
  const msg = cards.quoteCard({
    name: '贵州茅台',
    code: 'SH600519',
    status: 'trading',
    price: '1523.66',
    change: '-23.44',
    change_percent: '-1.52',
    amount: '461200',
    turnover: '0.37',
    total_mv: '191480000',
    time: '15:00'
  });
  assert.strictEqual(msg.card, 'quote');
  assert.strictEqual(msg.quote.price, 1523.66);
  assert.strictEqual(msg.quote.changePct, -1.52);
  assert.strictEqual(msg.quote.amount, 461200 * 10000, '成交额必须按万元换算成元');
  assert.strictEqual(msg.quote.status, '交易中');
  assert.strictEqual(msg.quote.note, '行情时间 15:00');
  // 没有分时数据时必须显式给空数组，否则组件拿到 undefined
  assert.strictEqual(Array.isArray(msg.quote.points), true);
});

test('cards.quoteCard：未知交易状态不外泄英文枚举', () => {
  const msg = cards.quoteCard({ name: 'X', status: 'halted_forever' });
  assert.strictEqual(msg.quote.status, '');
});

test('cards.klineCard：只取最近 15 根，副标题如实标注总数', () => {
  const rows = [];
  for (let i = 0; i < 30; i++) {
    rows.push({ date: 'd' + i, open: '10', high: '11', low: '9', close: String(10 + i / 10), volume: '1000' });
  }
  const msg = cards.klineCard(rows, { name: '贵州茅台', code: 'SH600519' });
  assert.strictEqual(msg.kline.length, 15);
  assert.strictEqual(msg.kline[14].d, 'd29');
  assert.ok(msg.klineSub.indexOf('最近 15 个交易日') === 0);
  assert.ok(msg.klineSub.indexOf('共 30 根') > 0);
  assert.strictEqual(msg.klineName, '贵州茅台');
  // 均线与渲染区间等长（不能拿 30 根算 MA 再配 15 根蜡烛）
  assert.strictEqual(msg.klineMa.ma20.length, 15);
});

test('cards.klineCard：不足两根时不崩，空数组也能返回结构', () => {
  const msg = cards.klineCard([], null);
  assert.strictEqual(msg.kline.length, 0);
  assert.strictEqual(msg.klinePct, null);
  assert.strictEqual(msg.klineSupport, null);
  assert.strictEqual(msg.klineSub.indexOf('最近 0 个交易日'), 0);
});

test('cards.valuationCard / fundflowCard：数组包一行与直接一行两种载荷都吃', () => {
  const row = { ts_code: '600519.SH', trade_date: '20261024', pe: '24.18', total_mv: '191480000' };
  const a = cards.valuationCard([row]);
  const b = cards.valuationCard(row);
  assert.strictEqual(a.valuation.peTtm, null);
  assert.strictEqual(a.valuation.totalCap, 191480000 * 10000);
  assert.strictEqual(b.valuation.note, '600519.SH · 数据日期 2026-10-24');
  assert.strictEqual(cards.valuationCard(null), null);

  const flow = cards.fundflowCard({
    ts_code: '600519.SH',
    net_mf_amount: '12860',
    buy_elg_amount: '100',
    sell_elg_amount: '60',
    lhb: [{ seat: '机构专用', buy: '100', sell: '20' }, { seat: '', buy: '1' }]
  });
  assert.strictEqual(flow.fundflow.mainNet, 12860 * 10000);
  assert.strictEqual(flow.fundflow.tiers[0].value, 40 * 10000, '四档净额 =（买入-卖出）×10000');
  assert.strictEqual(flow.fundflow.lhb.length, 1, '无席位的龙虎榜行必须丢掉');
});

test('cards.compareCard：少于两个标的返回 null（组件按空卡降级）', () => {
  assert.strictEqual(cards.compareCard({ items: [{ name: 'A' }] }), null);
  assert.strictEqual(cards.compareCard({ items: [] }), null);
  const ok = cards.compareCard({ ts_code: 'SH600519', items: [{ name: 'A' }, { name: 'B', error: 'x' }, { name: 'C' }] });
  assert.strictEqual(ok.compare.items.length, 2, '带 error 的项要剔除');
});

test('cards.eventCard：没有有效事件时返回 null，不再渲染空时间轴', () => {
  assert.strictEqual(cards.eventCard({ events: [] }), null);
  assert.strictEqual(cards.eventCard({ events: [{ date: '2026-10-01' }] }), null);
  const ok = cards.eventCard({ events: [{ title: '解禁', important: 1 }], range: '近 30 天' });
  assert.strictEqual(ok.event.events[0].important, true);
  assert.strictEqual(ok.event.range, '近 30 天');
});

test('cards.reportCard：报告期标签与缺表容错', () => {
  const msg = cards.reportCard({ income: { end_date: '20260630', revenue: '100' }, indicator: { or_yoy: '12.5' } });
  assert.strictEqual(msg.report.periodLabel, '2026 中报');
  assert.strictEqual(msg.report.revenue.yoy, 12.5);
  assert.strictEqual(msg.report.cashFlow.value, null);
  assert.strictEqual(msg.report.beat, '');
});

/* ===================== 运行 ===================== */
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[核心逻辑] 单元测试');
  for (const c of cases) {
    try {
      await c.fn();
      console.log('  PASS  ' + c.name);
      passed++;
    } catch (e) {
      console.log('  FAIL  ' + c.name);
      console.log('        ' + (e && e.message));
      failures.push(c.name);
    }
  }
  console.log('\n  通过 ' + passed + '/' + cases.length);
  if (failures.length) console.log('  失败：' + failures.join(' | '));
  process.exit(failures.length ? 1 : 0);
})();
