// 语音转文字走「微信同声传译」插件（WechatSI），识别在插件侧完成，不再经过自家网关。
//
// 前提有两步：小程序后台添加插件、app.json 的 plugins 里声明 provider（wx069ba97219f66d99）。
// 管理器是插件级单例，回调只能属性赋值（manager.onStop = fn），
// 而对话页允许压栈多个实例，所以这里统一接一次回调再分发给「当前会话」，
// 页面只拿 start / stop / abort 三个动作。
const PLUGIN_ID = 'WechatSI';
// 停录后等插件回调的兜底时限：插件异常时不回 onStop，
// 不清会话会让之后每次录音都卡在排队里（表现为按住完全没反应）
const STALE_RESET_MS = 2000;

let manager = null;
let current = null; // 正在进行的识别会话
let pending = null; // 上一条还没收尾时排队的新会话
let resetTimer = 0;

function noop() {}

function safeStop() {
  try {
    manager.stop();
  } catch (e) {
    // 没在录时 stop 会抛错，忽略即可
  }
}

function clearReset() {
  if (resetTimer) clearTimeout(resetTimer);
  resetTimer = 0;
}

function scheduleReset() {
  clearReset();
  resetTimer = setTimeout(() => {
    resetTimer = 0;
    const s = current;
    const p = pending;
    current = null;
    pending = null;
    // 取消过的不通知（页面已复位）；排队中的会话也要给个失败回调，否则视图停在录音态
    if (s && !s.aborted) s.onError('识别超时');
    if (p) p.handlers.onError('识别超时');
  }, STALE_RESET_MS);
}

// 上一条收尾后再启动排队的会话：插件同一时间只允许一次识别任务
function flushPending() {
  const p = pending;
  pending = null;
  if (!p) return;
  startNow(p.handlers, p.options);
}

function ensureManager() {
  if (manager) return manager;
  const plugin = requirePlugin(PLUGIN_ID);
  manager = plugin.getRecordRecognitionManager();

  // 插件会无条件回调 onStart，没绑定的话等于塞了个 undefined 进去
  manager.onStart = noop;

  manager.onRecognize = (res) => {
    const s = current;
    if (!s || s.aborted) return;
    s.onPartial(String((res && res.result) || ''));
  };

  manager.onStop = (res) => {
    clearReset();
    const s = current;
    current = null;
    // 取消过的不再回终稿：调用方已经复位视图，迟到的文本会串进下一次输入
    if (s && !s.aborted) {
      s.onFinal({
        text: String((res && res.result) || ''),
        duration: (res && res.duration) || Date.now() - s.startedAt
      });
    }
    flushPending();
  };

  manager.onError = (res) => {
    clearReset();
    const s = current;
    current = null;
    if (s && !s.aborted) s.onError((res && (res.msg || res.errMsg)) || '识别失败');
    flushPending();
  };

  return manager;
}

function startNow(handlers, options) {
  const session = {
    onPartial: handlers.onPartial || noop,
    onFinal: handlers.onFinal || noop,
    onError: handlers.onError || noop,
    aborted: false,
    startedAt: Date.now()
  };
  current = session;
  try {
    // duration 上限 60000，到点插件自动停并回调 onStop，与手动松手同一条收尾链路
    manager.start({ duration: options.duration || 60000, lang: options.lang || 'zh_CN' });
  } catch (e) {
    current = null;
    session.onError('语音识别不可用');
  }
}

// 开始识别：handlers = { onPartial(text), onFinal({text, duration}), onError(msg) }
export function startRecognize(handlers, options) {
  const h = handlers || {};
  const o = options || {};
  try {
    ensureManager();
  } catch (e) {
    // 插件没声明 / 未在后台添加时 requirePlugin 会抛错，转成一次失败回调
    if (h.onError) h.onError('语音识别不可用');
    return;
  }

  // 上一条还在收尾（取消、刚松手）：排队等它的 onStop，否则旧终稿会串到新一次录音里
  if (current) {
    current.aborted = true;
    pending = { handlers: h, options: o };
    safeStop();
    scheduleReset();
    return;
  }
  startNow(h, o);
}

// 松手：停止录音，等插件回调 onStop 给终稿
export function stopRecognize() {
  if (!current || current.aborted) return;
  safeStop();
  scheduleReset();
}

// 取消（上滑放弃）：丢弃本次结果，不等终稿
export function abortRecognize() {
  if (!current) return;
  current.aborted = true;
  safeStop();
  scheduleReset();
}

export default { startRecognize, stopRecognize, abortRecognize };
