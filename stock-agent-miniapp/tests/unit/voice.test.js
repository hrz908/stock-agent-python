/**
 * 聊天语音同传的纯逻辑单测：LRU 缓存 + 稳定片段切分。
 *
 * 这两块决定「同一句话会不会被重复送去翻译」和「缓存会不会把最该留下的那条淘汰掉」，
 * 都是线上看不出来、但直接烧插件配额的地方，所以单独锁住行为。
 *
 * 运行：npm run test:voice
 */
const assert = require('assert');
const path = require('path');
const { loadModule } = require('./loader');

const ROOT = path.resolve(__dirname, '..', '..');
const cases = [];
const test = (name, fn) => cases.push({ name, fn });

/* ===================== utils/lru ===================== */
const { createLRU } = loadModule(path.join(ROOT, 'utils', 'lru.js'));

test('lru：命中后刷新顺序，淘汰最久未使用的那条', () => {
  const cache = createLRU(2);
  cache.set('a', '1');
  cache.set('b', '2');
  assert.strictEqual(cache.get('a'), '1', 'a 命中');
  cache.set('c', '3'); // 此时 b 最久没用，应该被淘汰
  assert.strictEqual(cache.has('b'), false);
  assert.strictEqual(cache.get('a'), '1');
  assert.strictEqual(cache.get('c'), '3');
  assert.strictEqual(cache.size(), 2);
});

test('lru：同一个 key 重写不会撑大容量', () => {
  const cache = createLRU(2);
  cache.set('a', '1');
  cache.set('a', '2');
  assert.strictEqual(cache.size(), 1);
  assert.strictEqual(cache.get('a'), '2');
});

test('lru：淘汰时回调拿到被淘汰的值', () => {
  const evicted = [];
  const cache = createLRU(1, { onEvict: (value, key) => evicted.push(key + '=' + value) });
  cache.set('a', 'wxfile://1');
  cache.set('b', 'wxfile://2');
  assert.strictEqual(evicted.length, 1);
  assert.strictEqual(evicted[0], 'a=wxfile://1');
});

test('lru：非法容量按 1 处理，delete 与 clear 都能清干净', () => {
  const cache = createLRU(0);
  assert.strictEqual(cache.limit, 1);
  cache.set('a', 1);
  cache.set('b', 2);
  assert.strictEqual(cache.size(), 1);
  cache.set('c', 3);
  assert.strictEqual(cache.delete('c'), true);
  assert.strictEqual(cache.size(), 0);
  cache.set('d', 4);
  cache.clear();
  assert.strictEqual(cache.size(), 0);
  assert.strictEqual(cache.get('d'), undefined);
});

/* ===================== utils/segment ===================== */
const { takeStableSegment } = loadModule(path.join(ROOT, 'utils', 'segment.js'));

test('segment：不足 4 个字不派发', () => {
  assert.strictEqual(takeStableSegment('你好', 0), null);
  assert.strictEqual(takeStableSegment('你好世界', 0).segment, '你好世界');
});

test('segment：只切已派发之后的新增部分', () => {
  const text = '你好世界继续说吧';
  const r = takeStableSegment(text, 4, { minLen: 4 });
  assert.strictEqual(r.segment, '继续说吧');
  assert.strictEqual(r.consumed, 4);
  assert.strictEqual(text.slice(4, 4 + r.consumed), '继续说吧');
});

test('segment：已经全部派发过时返回 null（防重复翻译烧配额）', () => {
  const text = '今天大盘高开低走';
  assert.strictEqual(takeStableSegment(text, text.length), null);
});

test('segment：长增量优先切在最后一个标点之后', () => {
  const text = '今天市场表现不错，成交量也放大了，后续继续观察';
  const r = takeStableSegment(text, 0, { preferLen: 14, maxLen: 28 });
  assert.strictEqual(r.segment, '今天市场表现不错，成交量也放大了，');
  assert.strictEqual(r.consumed, 17);
});

test('segment：一直没标点又超过上限时硬切，但不把英文单词切两半', () => {
  const text = 'hello world hello world hello world';
  const r = takeStableSegment(text, 0, { minLen: 4, preferLen: 14, maxLen: 28 });
  assert.strictEqual(r.consumed, 24, '切在最后一个空格之后');
  assert.strictEqual(r.segment, 'hello world hello world ');
});

test('segment：只有空白字符时不当成一个片段', () => {
  assert.strictEqual(takeStableSegment('    ', 0), null);
});

test('segment：下标越界不抛错，按已全部派发处理', () => {
  assert.strictEqual(takeStableSegment('你好世界', 999), null);
  assert.strictEqual(takeStableSegment(null, 0), null);
});

/* ===================== 运行 ===================== */
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[语音同传] 单元测试');
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
