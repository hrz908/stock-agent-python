// 语音播报（TTS）：微信同声传译插件的 textToSpeech + 单例 innerAudioContext。
//
// 同传的节奏是「译文一到就要出声」，所以做两件事：
//   1) 译文回来的那一刻先 prefetch 把音频落成本地文件（LRU 缓存），
//      真正轮到播放时 speak() 直接命中，切句几乎没有网络等待；
//   2) 全局只留一个 innerAudioContext，逐条串行播放，绝不并发互相打断。
// 临时文件由微信管理，LRU 只淘汰引用、不删文件：filename 可能是 http(s) 或
// wxfile:// 临时路径，unlink 在部分基础库上会抛错，收益不值得。
import { createLRU } from '../utils/lru';

const PLUGIN_ID = 'WechatSI';
const TIMEOUT_MS = 8000;
// 音频缓存条数：一条几秒，24 条足够覆盖「上一句还在播、下一句已经好了」的窗口
const CACHE_LIMIT = 24;
// 播放超时：插件偶发不回调 ended，按字数估一个上限，超时直接进下一条
const PLAY_TIMEOUT_BASE = 2000;
const PLAY_TIMEOUT_PER_CHAR = 260;
const PLAY_TIMEOUT_MAX = 20000;

let pluginRef = null;
let audio = null;
// 每次 release 递增：卸载时仍在途的合成请求回来后会拿到旧代号，直接放弃播放，
// 否则会重新建一个没人用的音频上下文
let generation = 0;

const cache = createLRU(CACHE_LIMIT);
const pending = {};

function ensurePlugin() {
  if (pluginRef) return pluginRef;
  pluginRef = requirePlugin(PLUGIN_ID);
  return pluginRef;
}

// 单例音频上下文：页面卸载时 release() 销毁，下次进入再建
function ensureAudio() {
  if (audio) return audio;
  audio = wx.createInnerAudioContext();
  // 同传是「听」的功能，静音键打开时也要出声；用户可随时关掉播报开关
  audio.obeyMuteSwitch = false;
  return audio;
}

function keyOf(text, lang) {
  return lang + '|' + text;
}

function normalize(text) {
  return String(text === null || text === undefined ? '' : text).trim();
}

function synthesize(text, lang) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('配音超时'));
    }, TIMEOUT_MS);

    function finish(fn, value) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    }

    try {
      ensurePlugin().textToSpeech({
        lang: lang,
        tts: true,
        content: text,
        success: (res) => {
          const filename = res && res.filename;
          if (!filename) {
            finish(reject, new Error('配音失败'));
            return;
          }
          finish(resolve, filename);
        },
        fail: (err) => {
          finish(reject, new Error((err && (err.msg || err.errMsg)) || '配音失败'));
        }
      });
    } catch (e) {
      finish(reject, new Error('配音不可用'));
    }
  });
}

// 合成去重：prefetch 与 speak 常常前后脚触发同一条文本，
// 共用一个在途 Promise，避免同一句话合成两次（白烧一次配额）。
function synthesizeOnce(content, code) {
  const key = keyOf(content, code);
  const hit = cache.get(key);
  if (hit !== undefined) return Promise.resolve(hit);
  if (pending[key]) return pending[key];

  const task = synthesize(content, code).then(
    (filename) => {
      delete pending[key];
      cache.set(key, filename);
      return filename;
    },
    (err) => {
      delete pending[key];
      throw err;
    }
  );
  pending[key] = task;
  return task;
}

// 预取：只落缓存，不播。排在播放队列后面的句子靠它提前把音频拉下来
function prefetch(text, lang) {
  const content = normalize(text);
  if (!content) return;
  // 失败不打扰用户：真正要播时 speak 会再试一次
  synthesizeOnce(content, lang || 'en_US').catch(() => {});
}

function play(filename, content) {
  return new Promise((resolve) => {
    const ctx = ensureAudio();
    let settled = false;
    // stop() 触发的 onStop 可能晚于新一条的监听注册：没真正 onPlay 之前一律不算数，
    // 否则新一句刚设置好就被上一句的收尾事件当场「播完」
    let started = false;

    const timeout = Math.min(
      PLAY_TIMEOUT_MAX,
      PLAY_TIMEOUT_BASE + content.length * PLAY_TIMEOUT_PER_CHAR
    );
    const timer = setTimeout(finish, timeout);

    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // 必须摘监听：单例上下文上的回调不清会越积越多，下一次播放触发一串旧回调
      if (ctx.offPlay) ctx.offPlay(onPlay);
      if (ctx.offEnded) ctx.offEnded(onEnded);
      if (ctx.offError) ctx.offError(onError);
      if (ctx.offStop) ctx.offStop(onStop);
      resolve();
    }

    function onPlay() {
      started = true;
    }
    function onEnded() {
      finish();
    }
    function onError() {
      finish();
    }
    function onStop() {
      if (!started) return;
      finish();
    }

    try {
      // 上一条还没播完就整句压过来：直接打断，听最新的
      ctx.stop();
      ctx.src = filename;
      ctx.onPlay(onPlay);
      ctx.onEnded(onEnded);
      ctx.onError(onError);
      ctx.onStop(onStop);
      ctx.play();
    } catch (e) {
      finish();
    }
  });
}

// speak(text, lang) -> Promise<void>，播完 / 失败 / 超时都会 resolve，队列不会被卡住
function speak(text, lang) {
  const content = normalize(text);
  const code = lang || 'en_US';
  if (!content) return Promise.resolve();

  const gen = generation;
  const ready = synthesizeOnce(content, code);

  return ready
    .then((filename) => {
      if (gen !== generation) return;
      return play(filename, content);
    })
    .catch(() => {});
}

function stop() {
  if (!audio) return;
  try {
    audio.stop();
  } catch (e) {
    // 没播过时 stop 会抛错，忽略
  }
}

// 页面卸载：停播 + 销毁上下文。不销毁的话下次进入会拿到一个已停止的实例，
// 表现为「第一次点播报没声音」。
function release() {
  generation += 1;
  if (!audio) return;
  stop();
  try {
    audio.destroy();
  } catch (e) {
    // 同 stop：内部实例可能还没建起来
  }
  audio = null;
}

export { prefetch, speak, stop, release };
