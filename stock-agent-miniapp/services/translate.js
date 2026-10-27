// 文本翻译：走微信同声传译插件的 getTextTranslator（纯前端，不经自家网关）。
//
// 三条约束来自线上表现：
//   1) 插件不适合并发打——统一串行队列，一条回来再发下一条，避免配额瞬间打满；
//   2) 插件偶发不回调——单次 8s 超时，否则队列永久卡死、面板一直「翻译中」；
//   3) 同传会反复翻相似短句——LRU 命中直接返回，省一次往返也省配额。
//
// 语言码用插件约定：zh_CN / en_US（与 asr 的 lang 同一套）。
// 以后换自建 WebSocket 流式翻译时只替换本文件：页面只依赖 translate() 这个接口。
import { createLRU } from '../utils/lru';

const PLUGIN_ID = 'WechatSI';
const TIMEOUT_MS = 8000;
// 缓存条数：一条译文几十字节，200 条足够覆盖一场对话，又不会无限涨
const CACHE_LIMIT = 200;
// 队列上限：过老的片段翻出来也没人看了，直接判失败，别把延迟越堆越长
const QUEUE_LIMIT = 8;
// 插件对超长文本会失败，也会白烧一次配额，先截断
const MAX_CONTENT = 400;

let translator = null;
let running = false;

const cache = createLRU(CACHE_LIMIT);
const queue = [];

function ensureTranslator() {
  if (translator) return translator;
  const plugin = requirePlugin(PLUGIN_ID);
  translator = plugin.getTextTranslator();
  return translator;
}

function keyOf(text, from, to) {
  return from + '>' + to + '|' + text;
}

function normalize(text) {
  const s = String(text === null || text === undefined ? '' : text).trim();
  return s.length > MAX_CONTENT ? s.slice(0, MAX_CONTENT) : s;
}

// 单次请求：插件只认 success / fail 两个回调，这里统一收成 Promise 并加超时
function requestOnce(text, from, to) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('翻译超时'));
    }, TIMEOUT_MS);

    function finish(fn, value) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    }

    try {
      ensureTranslator().translate({
        lfrom: from,
        lto: to,
        content: text,
        success: (res) => {
          const out = res && (res.targetText || res.target || res.ret || '');
          finish(resolve, String(out || '').trim());
        },
        fail: (err) => {
          finish(reject, new Error((err && (err.msg || err.errMsg)) || '翻译失败'));
        }
      });
    } catch (e) {
      // 插件没声明 / 未在后台添加时 requirePlugin 会抛错
      finish(reject, new Error('翻译不可用'));
    }
  });
}

function drain() {
  if (running) return;
  const task = queue.shift();
  if (!task) return;
  running = true;
  requestOnce(task.text, task.from, task.to).then(
    (value) => {
      running = false;
      if (value) cache.set(task.key, value);
      task.resolve(value);
      drain();
    },
    (err) => {
      running = false;
      task.reject(err);
      drain();
    }
  );
}

// translate(text, { from, to }) -> Promise<string>
// 命不中缓存就排队；同一时刻只有一条在途，顺序即调用顺序
function translate(text, options) {
  const opts = options || {};
  const content = normalize(text);
  const from = opts.from || 'zh_CN';
  const to = opts.to || 'en_US';
  if (!content) return Promise.resolve('');

  const key = keyOf(content, from, to);
  const hit = cache.get(key);
  if (hit !== undefined) return Promise.resolve(hit);

  return new Promise((resolve, reject) => {
    queue.push({ text: content, from, to, key, resolve, reject });
    while (queue.length > QUEUE_LIMIT) {
      const dropped = queue.shift();
      dropped.reject(new Error('翻译队列溢出'));
    }
    drain();
  });
}

export { translate };
