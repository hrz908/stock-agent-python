/**
 * 聊天页语音输入（语音转文字）的交互测试。
 *
 * 锁的是「什么时候发、什么时候只是停」这条最容易回归的分支：
 *   - 手动点停 = 立即发送识别出的文字
 *   - 静音自动停 = 文字停在面板上，再点一次才发
 *   - 取消 = 直接丢弃
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

test('中间结果：第一段立刻上屏，后续按 150ms 合批', async () => {
  const { page, calls } = setup();
  page.startRecord();
  const handlers = calls.startRecognize[0].handlers;

  handlers.onPartial('贵州茅台');
  assert.strictEqual(page.data.voiceText, '贵州茅台', '第一段要立刻显示，不等节流');

  handlers.onPartial('贵州茅台今天');
  assert.strictEqual(page.data.voiceText, '贵州茅台', '节流窗口内不再触发 setData');

  await new Promise((r) => setTimeout(r, 200));
  assert.strictEqual(page.data.voiceText, '贵州茅台今天', '合批结束后落到最新一段');
});

test('长句中间结果：面板只显示最新一截（以刚说的话为准）', () => {
  const { page } = setup();
  page.startRecord();

  page.onVoicePartial('一'.repeat(60) + '这句话是最新的');
  assert.ok(page.data.voiceText.indexOf('这句话是最新的') !== -1, '最新的话必须看得见');
  assert.ok(page.data.voiceText.length <= 45, '只保留最新一截，不把整段塞进三行框');
  assert.strictEqual(page.data.voiceText.charAt(0), '…', '被截断时要有省略号');
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

test('取消：丢弃终稿，不发送', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.onVoiceStop({ text: '不该发出去', duration: 2000 });
  page.onVoiceCancel();

  assert.deepStrictEqual(sent, []);
  assert.strictEqual(page.data.voiceHolding, false);
  assert.strictEqual(page.data.voiceText, '');
});

test('切回键盘：待发终稿一并丢弃，不误发', () => {
  const { page, sent } = setup();
  page.startRecord();
  page.onVoiceStop({ text: '别发这句', duration: 2000 });

  page.onToggleInputMode();
  assert.strictEqual(page.data.voiceMode, false);
  assert.strictEqual(page.data.voiceHolding, false);
  assert.deepStrictEqual(sent, []);
});

// ---------- 运行 ----------
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[语音转写] 页面交互测试');
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
