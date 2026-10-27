/**
 * services/asr.js 的会话分发单测：用一个假插件，验证「插件回调 → 当前会话」这一层
 * 没有丢事件。真机上如果流式不出字，先排除这一层，再怀疑插件本身。
 *
 * 运行：npm run test:asr
 */
const assert = require('assert');
const path = require('path');
const { loadModule } = require('./loader');

const ROOT = path.resolve(__dirname, '..', '..');
const cases = [];
const test = (name, fn) => cases.push({ name, fn });

// 每次新建一份模块实例：模块内是单例状态，测试之间不能互相污染
function makeAsr() {
  const calls = { start: [], stop: 0 };
  const manager = {
    start: (opts) => calls.start.push(opts),
    stop: () => {
      calls.stop += 1;
    }
  };
  const plugin = { getRecordRecognitionManager: () => manager };
  const asr = loadModule(path.join(ROOT, 'services', 'asr.js'), {}, {
    requirePlugin: () => plugin,
    // 测试按正式环境跑：不打开发版的中间结果日志
    wx: { getAccountInfoSync: () => ({ miniProgram: { envVersion: 'release' } }) }
  });
  return { asr, manager, calls };
}

test('onRecognize：中间结果原样转发给当前会话', () => {
  const { asr, manager } = makeAsr();
  const partials = [];
  asr.startRecognize({ onPartial: (t) => partials.push(t) }, {});

  manager.onRecognize({ result: '你' });
  manager.onRecognize({ result: '你好' });

  assert.deepStrictEqual(partials, ['你', '你好']);
});

test('onStop：终稿带文本与录音时长', () => {
  const { asr, manager } = makeAsr();
  const finals = [];
  asr.startRecognize({ onFinal: (r) => finals.push(r) }, {});

  manager.onRecognize({ result: '你好' });
  manager.onStop({ result: '你好世界', duration: 1234 });

  assert.strictEqual(finals.length, 1);
  assert.strictEqual(finals[0].text, '你好世界');
  assert.strictEqual(finals[0].duration, 1234);
});

test('静音判停：开口后静音超过 silenceMs 自动 stop', async () => {
  const { asr, manager, calls } = makeAsr();
  asr.startRecognize({ onPartial: () => {}, onFinal: () => {}, onError: () => {} }, { silenceMs: 60 });

  manager.onRecognize({ result: '在说' });
  await new Promise((r) => setTimeout(r, 250));

  assert.ok(calls.stop >= 1, '静音到期应调用 stop');
});

test('abort 之后迟到的终稿不再回调（页面靠这条不必自己判「已取消」）', () => {
  const { asr, manager } = makeAsr();
  const finals = [];
  asr.startRecognize({ onFinal: (r) => finals.push(r) }, {});

  asr.abortRecognize();
  manager.onStop({ result: '迟到的终稿' });

  assert.deepStrictEqual(finals, []);
});

/* ===================== 运行 ===================== */
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[语音识别] 会话分发测试');
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
  // 静音用例留下 6s 的兜底定时器，不等它自然到期
  process.exit(failures.length ? 1 : 0);
})();
