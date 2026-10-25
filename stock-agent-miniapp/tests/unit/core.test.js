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
