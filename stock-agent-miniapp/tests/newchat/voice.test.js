/**
 * 聊天页语音态（同声传译）的交互测试。
 *
 * 锁的是「什么时候发、什么时候只是停」这条最容易回归的分支：
 *   - 手动点停 = 立即发送原文
 *   - 静音自动停 = 终稿停在面板上，再点一次才发
 *   - 取消 = 原文与译文都丢弃
 *   - 聆听中不许切方向（切了识别语言和译文会对不上）
 *
 * 运行：npm run test:voicechat
 */
const assert = require('assert');
const { createChatPage } = require('./harness');

const cases = [];
const test = (name, fn) => cases.push({ name, fn });

// 进入语音态，并把 sendText 换成记录器（这一层只验「发没发、发了什么」）
function setup() {
  const h = createChatPage();
  h.page.onLoad({});
  h.page.setData({ voiceMode: true });
  const sent = [];
  h.page.sendText = (text) => sent.push(text);
  return { page: h.page, calls: h.calls, sent };
}

test('手动点停：终稿直接发出，面板收起', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.stopVoiceAndSend();
  assert.strictEqual(page.data.voiceTip, '正在转写…', '等终稿期间应显示转写中');

  page.onVoiceStop({ text: '贵州茅台今天怎么样', duration: 3200 });
  assert.deepStrictEqual(sent, ['贵州茅台今天怎么样']);
  assert.strictEqual(page.data.recording, false);
  assert.strictEqual(page.data.voiceHolding, false);
  assert.strictEqual(page.data.voiceText, '');
});

test('静音自动停：只停在面板上，不发送；再点一次才发', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.onVoiceStop({ text: '贵州茅台今天怎么样', duration: 3200 });

  assert.deepStrictEqual(sent, [], '自动停录不得自动发送');
  assert.strictEqual(page.data.voiceHolding, true);
  assert.strictEqual(page.data.voiceText, '贵州茅台今天怎么样', '终稿要留在面板上可改口');
  assert.strictEqual(page.data.voiceTip, '点发送');

  page.onVoiceTap();
  assert.deepStrictEqual(sent, ['贵州茅台今天怎么样']);
  assert.strictEqual(page.data.voiceHolding, false);
});

test('待发态点发送键：同样发出终稿', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.onVoiceStop({ text: '白银后续怎么看', duration: 2000 });

  page.onSend();
  assert.deepStrictEqual(sent, ['白银后续怎么看']);
  assert.strictEqual(page.data.voiceHolding, false);
});

test('取消：丢弃终稿与译文，不发送', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.onVoiceStop({ text: '不该发出去', duration: 2000 });
  page.onVoiceCancel();

  assert.deepStrictEqual(sent, []);
  assert.strictEqual(page.data.voiceHolding, false);
  assert.strictEqual(page.data.voiceText, '');
  assert.strictEqual(page.data.voiceDst, '');
});

test('方向：聆听中不允许切换，空闲时可切并同步文案', () => {
  const { page, calls } = setup();
  page.startRecord();
  page.onVoiceSwapDir();
  assert.strictEqual(page.data.voiceDir, 'zh2en', '聆听中不应改方向');
  assert.ok(calls.toast.indexOf('先发送或取消这一次') !== -1, '要给出不能切的原因');

  page.onVoiceCancel();
  page.onVoiceSwapDir();
  assert.strictEqual(page.data.voiceDir, 'en2zh');
  assert.strictEqual(page.data.voiceLangLabel, 'EN → 中');
});

// ---------- 运行 ----------
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[语音同传] 页面交互测试');
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
  if (failures.length) {
    console.log('  失败：' + failures.join(' | '));
  }
  // 页面里挂了 45s 的发送看门狗定时器，不等它自然到期
  process.exit(failures.length ? 1 : 0);
})();
