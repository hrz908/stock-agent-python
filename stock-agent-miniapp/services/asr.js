// 语音转文字走「微信同声传译」插件（WechatSI），识别在插件侧完成，不再经过自家网关。
//
// 前提有两步：小程序后台添加插件、app.json 的 plugins 里声明 provider（wx069ba97219f66d99）。
// 管理器是插件级单例，回调只能属性赋值（manager.onStop = fn），
// 而对话页允许压栈多个实例，所以这里统一接一次回调再分发给「当前会话」，
// 页面只拿 start / stop / abort / warmup 四个动作。
const PLUGIN_ID = 'WechatSI';
// 停录后等插件回调的兜底时限：插件异常时不回 onStop，
// 不清会话会让之后每次录音都卡在排队里（表现为点了麦克风完全没反应）。
// 不能给太短：长录音的终稿往往要几秒才回来，2s 会把正常识别误判成超时。
// 但一句都没识别出来时没有终稿要等，这时用短的一档，别让人对着「正在转写…」干等
const STALE_RESET_MS = 6000;
const STALE_RESET_QUICK_MS = 2500;

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
  // 有中间结果 = 有终稿要等，给足时间；一个字都没出 = 多半是误触，快速收尾
  const wait = current && current.heard ? STALE_RESET_MS : STALE_RESET_QUICK_MS;
  resetTimer = setTimeout(() => {
    resetTimer = 0;
    const s = current;
    const p = pending;
    current = null;
    pending = null;
    clearSilence(s);
    // 取消过的不通知（页面已复位）；排队中的会话也要给个失败回调，否则视图停在录音态
    if (s && !s.aborted) s.onError('识别超时');
    if (p) p.handlers.onError('识别超时');
  }, wait);
}

/* ===== 静音自动收尾 =====
   只在调用方显式传 silenceMs 时启用：语音输入需要「说完就出终稿」，
   其它调用方不传就保持原行为，不能被悄悄改掉。
   插件没有静音检测参数，只能在中间结果停更时自己判停。 */
function clearSilence(session) {
  if (session && session.silenceTimer) {
    clearTimeout(session.silenceTimer);
    session.silenceTimer = 0;
  }
}

function armSilence(session) {
  if (!session || !session.silenceMs) return;
  clearSilence(session);
  // 首次开口前给更长的宽限：刚点下按钮时用户往往还在组织语言，
  // 按静音时长直接判停会把人「还没开口就掐掉」
  const wait = session.heard ? session.silenceMs : Math.max(4000, session.silenceMs * 3);
  session.silenceTimer = setTimeout(() => {
    session.silenceTimer = 0;
    if (current !== session || session.aborted) return;
    safeStop();
    scheduleReset();
  }, wait);
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
    const text = String((res && res.result) || '');
    // 空串不算开口：插件启动瞬间会先回一帧空结果，按它起算会把静音判停提前
    if (text) {
      s.heard = true;
      armSilence(s);
    }
    s.onPartial(text);
  };

  manager.onStop = (res) => {
    clearReset();
    const s = current;
    current = null;
    clearSilence(s);
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
    clearSilence(s);
    if (s && !s.aborted) s.onError((res && (res.msg || res.errMsg)) || '识别失败');
    flushPending();
  };

  return manager;
}

function startNow(handlers, options) {
  const opts = options || {};
  const session = {
    onPartial: handlers.onPartial || noop,
    onFinal: handlers.onFinal || noop,
    onError: handlers.onError || noop,
    aborted: false,
    // 静音判停（0 = 关闭）
    silenceMs: Math.max(0, Number(opts.silenceMs) || 0),
    heard: false,
    silenceTimer: 0,
    startedAt: Date.now()
  };
  current = session;
  try {
    // duration 上限 60000，到点插件自动停并回调 onStop，与手动松手同一条收尾链路
    manager.start({ duration: opts.duration || 60000, lang: opts.lang || 'zh_CN' });
    armSilence(session);
  } catch (e) {
    current = null;
    clearSilence(session);
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
    clearSilence(current);
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
  // 手动 stop 优先：清掉静音计时，免得它再补一次 stop
  clearSilence(current);
  safeStop();
  scheduleReset();
}

// 取消（上滑放弃）：丢弃本次结果，不等终稿
export function abortRecognize() {
  if (!current) return;
  current.aborted = true;
  clearSilence(current);
  safeStop();
  scheduleReset();
}

// 预热：requirePlugin + getRecordRecognitionManager 首次调用有初始化开销，
// 页面上把这一步挪到「切到语音态」时做，别占按下麦克风的那一下
export function warmupRecognize() {
  try {
    ensureManager();
    return true;
  } catch (e) {
    return false;
  }
}
