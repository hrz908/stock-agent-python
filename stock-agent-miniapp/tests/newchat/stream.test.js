/**
 * 流式正文渲染（逐字揭示 + 增量版式）的回归用例。
 *
 * 背景：视图层渲染的是 msg.blocks，不是 content[0].data。曾经每帧只更新后者、
 * blocks 按 120ms 节流整段重解析，屏幕上的文字就成了每 120ms 跳一截
 * （用户反馈的「一段一段地出来」）。这里锁死三条不变式：
 *   1. 同一段文字内推进时只 patch 尾段下标，绝不整份 blocks 回传；
 *   2. 段落定型（空行）时才整段重写，并把全文落到 content 底稿上；
 *   3. 收尾（done / error / 切会话）一次性铺完，版式与文字都不落后。
 *
 * 运行：npm test（或 node tests/newchat/stream.test.js）
 */
const assert = require('assert');
const { createChatPage } = require('./harness');

const cases = [];
const test = (name, fn) => cases.push({ name, fn });

// 直接驱动页面内部接口，不走真实定时器：逐字帧的时序在 CI 上不可靠。
// 每次喂完一段就把看门狗与逐字定时器停掉，再手动铺到全文渲染一帧
function step(page, delta) {
  page._appendToken(delta);
  page._clearSendWatchdog(); // 45s 空闲看门狗会拖住测试进程
  if (page._typeTimer) {
    clearTimeout(page._typeTimer);
    page._typeTimer = 0;
  }
  page._streamShown = page._streamText.length;
  page._renderStream();
}

// 记录每帧 setData 的补丁键，用来断言「回传了多少东西」
function record(page) {
  const patches = [];
  const raw = page.setData;
  page.setData = function (patch) {
    patches.push(patch);
    return raw.call(page, patch);
  };
  return patches;
}

test('同一段内推进：每帧只 patch 尾段下标，不重传整篇 blocks', () => {
  const { page } = createChatPage();
  page.onLoad({});
  const patches = record(page);

  step(page, '第一段正文。\n\n第二段正在写');
  const first = patches[patches.length - 1];
  assert.deepStrictEqual(
    Object.keys(first).sort(),
    ['messages[0].blocks', 'messages[0].content[0].data'],
    '首帧要整段写一次（段落刚定型）'
  );

  step(page, '，还在继续');
  const second = patches[patches.length - 1];
  assert.deepStrictEqual(
    Object.keys(second),
    ['messages[0].blocks[1]'],
    '尾段内推进只允许 patch 最后一段，整份 blocks 回传会让视图层每帧重建整条气泡'
  );
  // 视图数据仍然完整：两段都在，最后一段带上新字
  const blocks = JSON.parse(JSON.stringify(page.data.messages[0].blocks));
  assert.strictEqual(blocks.length, 2);
  assert.ok(JSON.stringify(blocks[1]).indexOf('还在继续') !== -1);
});

test('段落定型：整段重写一次，定型过的段不再变', () => {
  const { page } = createChatPage();
  page.onLoad({});
  const patches = record(page);

  step(page, '第一段。\n\n第二段正在写');
  const before = JSON.stringify(page.data.messages[0].blocks[0]);

  step(page, '，还在继续。\n\n第三段');
  const patch = patches[patches.length - 1];
  assert.deepStrictEqual(
    Object.keys(patch).sort(),
    ['messages[0].blocks', 'messages[0].content[0].data'],
    '新段落定型后下标整体后移，只能整段重写'
  );
  const blocks = JSON.parse(JSON.stringify(page.data.messages[0].blocks));
  assert.strictEqual(blocks.length, 3);
  assert.strictEqual(JSON.stringify(blocks[0]), before, '已定型的段落一个字都不该变');
});

test('收尾：剩余文字一次性铺完，blocks 与 content 都是全文', () => {
  const { page } = createChatPage();
  page.onLoad({});
  step(page, '第一段。\n\n第二段');

  page._snapStreamEnd();
  const msg = page.data.messages[0];
  assert.strictEqual(msg.content[0].data, '第一段。\n\n第二段', 'content 是重进本页恢复在途流的底稿');
  const blocks = JSON.parse(JSON.stringify(msg.blocks));
  assert.strictEqual(blocks.length, 2);
  assert.ok(JSON.stringify(blocks[1]).indexOf('第二段') !== -1);
});

// ---------- 运行 ----------
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[流式渲染] 前端交互测试');
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
