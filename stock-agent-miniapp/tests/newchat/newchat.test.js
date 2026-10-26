/**
 * 「新对话」按钮的端到端交互测试（前端侧）。
 *
 * 覆盖点对应需求里的四条：
 *   1. 按钮触发的事件与调用的后端接口、参数         —— 用例 1
 *   2. 加载中 / 成功 / 失败三态的交互与错误提示     —— 用例 1 / 2 / 3
 *   3. 连点防护                                     —— 用例 4
 *   4. 新建后界面正确显示、会话上下文一致           —— 用例 5 / 6 / 7 / 8 / 9
 *
 * 运行：npm test （或 node tests/newchat/newchat.test.js）
 */
const assert = require('assert');
const { createChatPage } = require('./harness');

const NEW_CHAT_URL = '/pages/chat/chat?new=1';

const cases = [];
const test = (name, fn) => cases.push({ name, fn });

// ---------- 1. 加载中：进入创建态并发起注册 ----------
test('点击「新对话」：按钮进入创建中，并调用 POST /agent/sessions 注册一条会话', async () => {
  const { page, calls } = createChatPage();
  page.onLoad({});
  page.setData({ drawerOpen: true });

  page.onNewChat();

  assert.strictEqual(page.data.newChatting, true, '按钮应立即进入创建中状态');
  assert.strictEqual(calls.createSession.length, 1, '应恰好发起一次注册请求');
  const arg = calls.createSession[0];
  assert.ok(/^sess_[0-9]+_[a-z0-9]+$/.test(arg.sid), 'session_id 形如 sess_<时间戳>_<随机>：' + arg.sid);
  // 后端两道校验（Go validSessionID / Python _SESSION_ID_RE）都只放行
  // [A-Za-z0-9_-] 且长度 ≤64：session_id 会拼进 thread_id 与 Redis key
  assert.ok(
    /^[A-Za-z0-9_-]{1,64}$/.test(arg.sid),
    'session_id 必须落在后端白名单内：' + arg.sid
  );
  assert.strictEqual(arg.title, '', '新对话的标题为空，后端落占位标题「新对话」');
  assert.strictEqual(calls.navigateTo.length, 0, '注册还没回来，不应跳转');
});

// ---------- 2. 成功：抽屉收起后压栈新对话页 ----------
test('注册成功：抽屉收起动画结束后压栈新对话页，URL 带新 session_id，创建中复位', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({});
  page.setData({ drawerOpen: true });

  page.onNewChat();
  await flush();

  const sid = calls.createSession[0].sid;
  assert.strictEqual(page.data.drawerOpen, false, '抽屉应已开始收起');
  assert.strictEqual(calls.navigateTo.length, 0, '动画未结束前不跳转（否则会掐断收起动画）');

  page.onDrawerLeaved(); // 收起动画结束
  assert.strictEqual(calls.navigateTo.length, 1, '动画结束后应压栈新对话页');
  assert.strictEqual(
    calls.navigateTo[0].url,
    NEW_CHAT_URL + '&session=' + encodeURIComponent(sid),
    '新页面必须带上刚注册的 session_id'
  );
  assert.strictEqual(page.data.newChatting, false, '跳转后创建中状态应复位');
});

// ---------- 3. 失败：提示且留在原会话 ----------
test('注册失败：toast 提示、不跳转、留在原会话、创建中复位', async () => {
  const { page, calls, flush } = createChatPage({
    createSession: () => Promise.reject(new Error('502'))
  });
  page.onLoad({ session: 'sess_origin' });
  page.setData({ drawerOpen: true });
  const sidBefore = page._sessionId;

  page.onNewChat();
  await flush();

  assert.deepStrictEqual(calls.toast, ['新建对话失败，请重试'], '失败要给用户明确提示');
  assert.strictEqual(calls.navigateTo.length, 0, '注册失败不应跳进一条没注册的会话');
  assert.strictEqual(calls.reLaunch.length, 0, '同样不应 reLaunch');
  assert.strictEqual(page.data.newChatting, false, '失败后按钮必须可再次点击');
  assert.strictEqual(page._sessionId, sidBefore, '失败后仍留在原会话，上下文不丢');

  // 可重试：第二次点击照常发起注册
  page.onNewChat();
  assert.strictEqual(calls.createSession.length, 2, '失败后应允许重试');
});

// ---------- 4. 连点防护 ----------
test('连点三次：只注册一条会话、只压一个页面', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({});
  page.setData({ drawerOpen: true });

  page.onNewChat();
  page.onNewChat();
  page.onNewChat();
  await flush();
  page.onDrawerLeaved();

  assert.strictEqual(calls.createSession.length, 1, '连点期间只应注册一条会话');
  assert.strictEqual(calls.navigateTo.length, 1, '连点期间只应压栈一个页面');
});

// ---------- 5. 抽屉未打开：注册回来即跳转 ----------
test('抽屉未打开时点击：注册成功即跳转，不必等动画', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({});
  page.setData({ drawerOpen: false });

  page.onNewChat();
  await flush();

  const sid = calls.createSession[0].sid;
  assert.strictEqual(calls.navigateTo.length, 1, '没有动画要等时，注册回来应立即跳转');
  assert.strictEqual(calls.navigateTo[0].url, NEW_CHAT_URL + '&session=' + encodeURIComponent(sid));
  assert.strictEqual(page.data.newChatting, false);
});

// ---------- 6. 新对话页首屏 ----------
test('新对话页 onLoad(?new=1&session=)：首屏无消息、不回拉消息、沿用注册好的 session', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({ new: '1', session: 'sess_new_1' });
  await flush();

  assert.strictEqual(page._sessionId, 'sess_new_1', '新页面必须沿用注册好的会话');
  assert.strictEqual(calls.sessionMessages.length, 0, '空会话无需回拉消息（省一次请求 + 避免空结果覆盖首屏）');
  assert.strictEqual(page.data.messages.length, 0, '首屏不铺问候语，直接留给引导问题');
  assert.strictEqual(page.data.newChatting, false, '新页面不继承上一页的创建中状态');
});

// ---------- 7. 历史会话恢复 ----------
test('历史会话 onLoad(?session=)：回拉并渲染该会话的消息', async () => {
  const { page, calls, flush } = createChatPage({
    messages: {
      sess_old: [
        { role: 'user', content: '茅台现在多少钱' },
        { role: 'assistant', content: '当前 1680.50 元' }
      ]
    }
  });
  page.onLoad({ session: 'sess_old' });
  await flush();

  assert.deepStrictEqual(calls.sessionMessages, ['sess_old'], '应回拉该会话的消息');
  assert.strictEqual(page.data.messages.length, 2);
  assert.strictEqual(page.data.messages[0].role, 'user');
  assert.strictEqual(page.data.messages[0].content[0].data, '茅台现在多少钱');
  assert.strictEqual(page.data.messages[1].role, 'assistant');
});

// ---------- 8. 切会话的上下文隔离 ----------
test('切会话：掐断在途流、发送态复位，旧会话的后续回包不再落到新会话', async () => {
  const { page, calls, flush } = createChatPage({ messages: { sess_new: [] } });
  page.onLoad({ session: 'sess_old' });

  page.sendText('贵州茅台怎么样'); // 本页发起一轮，sending=true
  assert.strictEqual(page.data.sending, true);
  // 发送前要过一次登录（ensureLogin 是 Promise），落定后再断言请求参数
  await flush();
  assert.strictEqual(calls.sendMessage[0].sid, 'sess_old', '发送必须带本页的 session_id');

  page.onSessionSelect({ currentTarget: { dataset: { sid: 'sess_new' } } });
  await flush();

  assert.strictEqual(calls.disconnect.length, 1, '切会话要掐断在途流');
  assert.strictEqual(page._myReqId, 0, 'rid 闸门归零：旧轮的 token / abort 错误不再渲染到新会话');
  assert.strictEqual(page.data.sending, false, '发送态必须复位，否则新会话发不出消息');
  assert.strictEqual(page._sessionId, 'sess_new');
  // 进页面时已经回拉过 sess_old，切换后再拉 sess_new
  assert.deepStrictEqual(calls.sessionMessages, ['sess_old', 'sess_new'], '切换后应加载新会话的消息');
});

// ---------- 9. 新对话后发送沿用新 session ----------
test('新对话页发送：带上新会话的 session_id（上下文不串到上一轮）', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({ new: '1', session: 'sess_fresh' });

  page.sendText('宁德时代资金流');
  await flush();

  assert.strictEqual(calls.sendMessage.length, 1);
  assert.strictEqual(calls.sendMessage[0].sid, 'sess_fresh', '新会话的提问必须落在新 thread 上');
});

// ---------- 10. 删除当前会话后原地重置为新对话 ----------
test('删除当前会话：原地换一条新会话，且不得自动注册空会话', async () => {
  const { page, calls, flush } = createChatPage();
  page.onLoad({ session: 'sess_gone' });

  page.onSessionDelete({ currentTarget: { dataset: { sid: 'sess_gone' } } });
  assert.strictEqual(page.data.deleteVisible, true, '先弹确认框，不直接删');
  page.confirmDeleteSession();
  await flush();

  assert.deepStrictEqual(calls.deleteSession, ['sess_gone']);
  // 自动注册会立刻在历史列表原地补一条「新对话」，用户看到的就是「第一条删不掉」
  assert.strictEqual(calls.createSession.length, 0, '删除后不得自动注册空会话');
  assert.ok(/^sess_/.test(page._sessionId) && page._sessionId !== 'sess_gone', '当前页要换成一条全新会话');
  assert.strictEqual(calls.navigateTo.length, 0, '原地换会话，不跳页（抽屉仍开着）');
  assert.strictEqual(page.data.messages.length, 0, '新会话首屏不留旧消息');
});

// ---------- 11. 删除与在途注册的竞态 ----------
test('删除当前会话时已有注册在途：沿用该注册，不重复注册第二条', async () => {
  // 手动控制的 Promise：把「注册在途」这个状态固定住，避免用例自身产生竞态
  let resolveCreate;
  const pending = new Promise((r) => {
    resolveCreate = r;
  });
  const { page, calls, flush } = createChatPage({ createSession: () => pending });
  page.onLoad({ session: 'sess_gone' });

  page.onNewChat(); // 注册在途
  page.onSessionDelete({ currentTarget: { dataset: { sid: 'sess_gone' } } });
  await flush();
  assert.strictEqual(calls.createSession.length, 1, '在途注册被复用，不应再发起第二条');

  resolveCreate({ session_id: calls.createSession[0].sid }); // 注册回来
  await flush();
  assert.strictEqual(calls.navigateTo.length, 1, '注册完成后跳转到那条新会话');
});

// ---------- 运行 ----------
(async () => {
  let passed = 0;
  const failures = [];
  console.log('\n[新对话] 前端交互测试');
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
