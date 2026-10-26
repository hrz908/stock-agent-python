/**
 * 小程序页面测试底座（零依赖，Node 原生跑）。
 *
 * 为什么手工搭：小程序页面是「ESM 源码 + Page() 注册 + wx.* 全局 API」的形态，
 * 既不能在 Node 的 CJS 加载器里直接 require（import 语法报错），也拿不到 wx 全局。
 * 装 jest/babel 一整套只为了跑几个页面交互用例不划算，故这里做最小实现：
 *   1. 把 chat.js 顶部的 import 改写成 __req() 调用（其余代码原样执行，不转译）；
 *   2. 造一个假的 wx 与 Page()，把页面配置对象抓出来；
 *   3. 按小程序语义补上实例方法 setData（支持 'a[0].b' 路径写法）与 selectComponent。
 *
 * 这样测到的是**真实的 chat.js 逻辑**：被测代码一行没改，只有环境是假的。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { loadModule } = require('../unit/loader');

const ROOT = path.resolve(__dirname, '..', '..');
const CHAT_JS = path.join(ROOT, 'pages', 'chat', 'chat.js');
const CARDS_JS = path.join(ROOT, 'pages', 'chat', 'cards.js');

// ESM -> CJS：只处理 chat.js 的顶层 import（默认导入 / 命名导入两种形态）。
// 必须锚到行首且限定子句字符集：文件顶部的注释里出现过
// 「不要改成 import { generateSessionId }」这样的字样，宽松的正则会从注释里的
// import 一路贪婪到第一个真实的 from，把整段代码替换坏
const RE_IMPORT = /^import\s+([\w{}\s,]+?)\s+from\s+['"]([^'"]+)['"];?/gm;

function toCommonJS(src) {
  return src.replace(RE_IMPORT, (_, clause, mod) => {
    const c = clause.trim();
    if (c.startsWith('{')) {
      const names = c
        .slice(1, -1)
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        // 命名导入的别名（x as y）在解构里要写成 x: y，否则语法不合法
        .map((s) => s.replace(/^(\w+)\s+as\s+(\w+)$/, '$1: $2'));
      return 'const { ' + names.join(', ') + " } = __req('" + mod + "');";
    }
    return 'const ' + c + " = __req('" + mod + "').default;";
  });
}

// setData 的路径写法：'messages[2].content[0].data' -> 逐层定位后赋值
function setByPath(target, keyPath, value) {
  const tokens = String(keyPath)
    .replace(/\[(\d+)\]/g, '.$1')
    .split('.')
    .filter((s) => s !== '');
  let cur = target;
  for (let i = 0; i < tokens.length - 1; i++) {
    const k = tokens[i];
    if (cur[k] === null || cur[k] === undefined) {
      cur[k] = /^\d+$/.test(tokens[i + 1]) ? [] : {};
    }
    cur = cur[k];
  }
  cur[tokens[tokens.length - 1]] = value;
}

function noop() {}

// 假的 wx 全局：只实现 chat.js 会用到的 API，并把调用记进 calls 供断言
function makeWx(calls) {
  const chainQuery = {
    in() {
      return this;
    },
    select() {
      return this;
    },
    boundingClientRect() {
      return this;
    },
    // 容器高度为 0 时 scrollToBottom 直接返回，测试无需关心滚动
    exec(cb) {
      if (cb) cb([{ height: 0 }]);
    }
  };
  return {
    getWindowInfo: () => ({
      statusBarHeight: 44,
      windowHeight: 667,
      windowWidth: 375,
      safeAreaInsets: { bottom: 0 }
    }),
    getSystemInfoSync: () => ({ statusBarHeight: 44, windowHeight: 667, windowWidth: 375 }),
    onWindowResize: noop,
    offWindowResize: noop,
    hideKeyboard: noop,
    getRecorderManager: () => ({ onStop: noop, onError: noop, start: noop, stop: noop }),
    createInnerAudioContext: () => ({
      onEnded: noop,
      onError: noop,
      stop: noop,
      destroy: noop,
      play: noop,
      src: ''
    }),
    createSelectorQuery: () => chainQuery,
    navigateTo: (o) => calls.navigateTo.push(o),
    reLaunch: (o) => calls.reLaunch.push(o),
    showModal: (o) => {
      calls.showModal.push(o);
      if (o && o.success) o.success({ confirm: true });
    },
    setClipboardData: noop,
    showToast: noop
  };
}

/**
 * 造一个对话页实例。
 * opts.createSession / opts.sessions / opts.messages 用来注入不同的后端表现。
 * 返回 { page, calls, agent, flush }：
 * - page   页面实例（data / 方法齐备，可直接调用生命周期与事件回调）
 * - calls  所有跨边界调用的流水（wx 跳转、toast、各 service 请求）
 * - agent  agentSocket 单例的替身
 * - flush  等所有 pending Promise 落定
 */
function createChatPage(opts) {
  opts = opts || {};
  const calls = {
    navigateTo: [],
    reLaunch: [],
    showModal: [],
    toast: [],
    createSession: [],
    sessionMessages: [],
    listSessions: [],
    deleteSession: [],
    sendMessage: [],
    disconnect: []
  };

  let seq = 0;
  const agent = {
    connect: noop,
    on: noop,
    off: noop,
    // 与 services/agent.js 的 generateSessionId 同形状，id 格式断言才有意义
    freshSessionId: () => 'sess_' + (Date.now() + seq++) + '_' + Math.random().toString(36).slice(2, 11),
    sendMessage: (query, sid) => {
      calls.sendMessage.push({ query: query, sid: sid });
      return 1;
    },
    disconnect: () => calls.disconnect.push(true)
  };

  const toastComp = {
    show: (message) => calls.toast.push(message)
  };

  const modules = {
    '../../services/agent': { default: agent },
    // 语音识别的替身：本套用例不触发录音，只保证模块导入时能取到三个动作
    '../../services/asr': {
      startRecognize: noop,
      stopRecognize: noop,
      abortRecognize: noop
    },
    '../../services/sessions': {
      listSessions: () => {
        calls.listSessions.push(true);
        return Promise.resolve({ sessions: opts.sessions || [] });
      },
      createSession: (sid, title) => {
        calls.createSession.push({ sid: sid, title: title });
        return opts.createSession
          ? opts.createSession(sid, title)
          : Promise.resolve({ session_id: sid });
      },
      sessionMessages: (sid) => {
        calls.sessionMessages.push(sid);
        const bag = opts.messages || {};
        return Promise.resolve({ messages: bag[sid] || [] });
      },
      deleteSession: (sid) => {
        calls.deleteSession.push(sid);
        return opts.deleteSession ? opts.deleteSession(sid) : Promise.resolve({});
      }
    },
    // 首屏建议问题：返回空列表，页面保留本地兜底（本套用例不校验建议内容）
    '../../services/quickAsk': {
      list: () => Promise.resolve({ quick_asks: [] }),
      click: noop
    },
    // 只返回结构够用的最小形态：本套用例不校验 markdown 渲染
    '../../utils/markdown': { parseMarkdown: (t) => [{ type: 'text', text: t }] },
    // 字号档位：本套用例不校验样式，onShow 能拿到补丁即可
    '../../utils/font-scale': {
      fontScalePatch: () => ({ fontScale: 'standard', fontScaleClass: '' })
    },
    // 卡片映射用真实实现（纯函数，无 wx 依赖）：替身一旦与实现漂移，
    // 「历史会话渲染出几条消息」这类断言就失去意义
    './cards': loadModule(CARDS_JS, {
      '../../utils/markdown': { parseMarkdown: (t) => [{ type: 'text', text: t }] }
    }),
    // 登录：sendText 会先 await 一次 ensureLogin（冷启动无 token 时不空手发 SSE），
    // 这里永远给一个可用 token
    '../../utils/request': {
      getToken: () => 'test-token',
      ensureLogin: () => Promise.resolve('test-token'),
      setSession: noop,
      clearSession: noop
    },
    // 环境开关：用例里按开发版跑（isDev 只影响行情卡跳详情页的分支）
    '../../config': { isDev: true, apiBase: 'https://test.local', wsBase: 'wss://test.local' }
  };

  const source = toCommonJS(fs.readFileSync(CHAT_JS, 'utf8'));
  let config = null;
  const sandbox = {
    __req: (id) => {
      const m = modules[id];
      if (!m) throw new Error('缺少模块替身: ' + id);
      return m;
    },
    Page: (cfg) => {
      config = cfg;
    },
    wx: makeWx(calls),
    console: console,
    // vm 的新 context 不带定时器，从宿主借（页面里的合批刷新 / 发送看门狗都靠它）
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval
  };
  vm.runInNewContext(source, sandbox, { filename: CHAT_JS });
  if (!config) throw new Error('chat.js 未调用 Page()');

  // 实例：方法与配置同源，data 深拷贝（多实例互不共享，模拟页面栈）
  const page = {};
  Object.keys(config).forEach((k) => {
    page[k] = config[k];
  });
  page.data = JSON.parse(JSON.stringify(config.data));
  page.setData = function (patch, cb) {
    Object.keys(patch).forEach((k) => setByPath(page.data, k, patch[k]));
    if (typeof cb === 'function') cb();
  };
  page.selectComponent = (sel) => (sel === '#base-toast' ? toastComp : null);

  return {
    page: page,
    calls: calls,
    agent: agent,
    flush: () => new Promise((r) => setImmediate(() => setImmediate(r)))
  };
}

module.exports = { createChatPage, CHAT_JS };
