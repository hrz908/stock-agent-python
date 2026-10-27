// Agent SSE 服务：管理与后端的 AI 对话流式连接，页面只调这里，不直接碰 wx.request。
//
// 协议（与 src/gateway/api/v1/chat.py 约定）：
//   上行：POST { query, session_id }，Header Bearer 鉴权
//   下行：text/event-stream，每帧 event: <type>\ndata: <json>\n\n，
//         type ∈ token / card / table / status / tool_start / tool_end / sources / error / ping / done
//
// 端到端真流式：网关直接消费 agent 的事件生成器并逐帧 Flush，小程序用 wx.request(enableChunked)
// 增量收包、增量 UTF-8 解码、即时派发，不做攒批。SSE 为「每问一请求」，无持久连接，connect() 是兼容 no-op。
//
// 超时：必须 ≥ 后端 agent.run_timeout（默认 120s），否则长回答会被客户端先掐断
//（表现为「连接异常」，而服务端还在生成）。

import { apiBase, agentSsePath } from '../config';
import { getToken, ensureLogin } from '../utils/request';

// 生成 session_id：后端据此拼 thread_id 区分上下文；页面经 freshSessionId() 取用，
// 每个对话页实例一条独立会话（多实例共享单例也不会串上下文）
function generateSessionId() {
  return 'sess_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
}

class AgentSSE {
  constructor() {
    this.task = null; // 当前 wx.request task
    this.sessionId = generateSessionId();
    // 发起轮计数：每次 sendMessage +1，请求级事件全部打 __rid，
    // 页面只渲染自己发起的那一轮（多对话页实例共享单例时的防串话闸门）
    this._reqId = 0;
    // 最近一次被 _abort 掐断的轮次：abort 触发的 fail 归属那一轮，
    // 避免「新起一轮时 abort 旧流」让新页面误弹连接异常
    this._killedRid = null;
    this.listeners = {}; // event -> [callback]
    this._buffer = ''; // 已解码、待切分 SSE 帧的文本
    this._pending = new Uint8Array(0); // 未凑齐 UTF-8 字符的字节残留
    this._isSSE = false; // 响应头是否为 text/event-stream
    this._chunkSeen = false; // 是否收到过分块（用于不支持 onChunkReceived 时的整段兜底）
    this._ended = false; // 是否已收到 done
    // 本轮的提问与会话：401 重登后要原样重放，故随轮保存
    this._round = { query: '', session: this.sessionId, retried: false };
    // 代次：每次 abort 自增。异步的重登回调靠它判断「这条流是否已被顶掉」
    this._gen = 0;
  }

  // 兼容旧调用：SSE 每问一请求，无持久连接
  connect() {}

  // 只生成 session_id，不动单例的在途流与默认 session
  //（区别于 newSession：那个会 abort 在途请求并改写默认值）
  freshSessionId() {
    return generateSessionId();
  }

  // 发送提问并开启一条独立 SSE 流；sessionId 由页面传入（不传用单例默认值），
  // 返回本轮 rid，页面凭它过滤事件
  sendMessage(query, sessionId) {
    // 上一轮若仍在流（理论上发送态会挡住），先中止避免两条流交错
    this._abort();
    this._reqId += 1;
    // 本轮上下文留给 _open：401 重登后要原样重放，rid 不变，
    // 页面侧的 __rid 闸门因此不用感知「重放过一次」
    this._round = { query: query, session: sessionId || this.sessionId, retried: false };
    this._open(getToken());
    return this._reqId;
  }

  // 打开一条 SSE 流。抽出来是为了 401 重放时复用同一套收包逻辑
  _open(token) {
    this._buffer = '';
    this._pending = new Uint8Array(0);
    this._isSSE = false;
    this._chunkSeen = false;
    this._ended = false;
    const round = this._round;
    const gen = this._gen;

    const task = wx.request({
      url: `${apiBase}${agentSsePath}`,
      method: 'POST',
      header: {
        Authorization: token ? `Bearer ${token}` : '',
        'Content-Type': 'application/json',
        Accept: 'text/event-stream'
      },
      enableChunked: true,
      responseType: 'arraybuffer',
      // 与后端 run_timeout 对齐（默认 120s）；不设会走微信默认 60s，长回答被腰斩
      timeout: 180000,
      data: { query: round.query, session_id: round.session },
      success: (res) => {
        this.task = null;
        const code = res && res.statusCode;
        // token 过期：静默重登一次再重放原提问，不把「未登录」抛给用户。
        // 只重试一次，避免账号异常时打成死循环
        if (code === 401 && !round.retried) {
          round.retried = true;
          ensureLogin(true).then((nt) => {
            // 重登期间用户可能已切会话 / 发了新问题，这条流不该再被拉起来
            if (gen !== this._gen) return;
            if (!nt) {
              this._emit('error', { code: 'SSE_NO_STREAM', message: '登录已失效，请重试' });
              return;
            }
            this._open(nt);
          });
          return;
        }
        // 已经逐块解析过、状态码也正常：content-type 可能压根没回调（老基础库），
        // 以「实际收到分块」为准，别在这里误报「服务响应异常」
        if (this._chunkSeen && !(code && code >= 400)) return;
        // 非 SSE 响应（限流 / 参数错误 / 鉴权失败都是 JSON 信封）：没有事件流
        if (!this._isSSE || (code && code >= 400)) {
          this._emit('error', {
            code: 'SSE_NO_STREAM',
            message: this._errorMessage(res, code)
          });
          return;
        }
        // 兜底：部分环境 onChunkReceived 不触发，整段响应在 success 里给，
        // 仅在没逐块解析过时收尾解析一遍，避免重复派发
        if (!this._chunkSeen && res && res.data) {
          this._buffer += this._decodeChunk(new Uint8Array(res.data));
          this._drain();
        }
      },
      fail: (err) => {
        this.task = null;
        // abort 造成的失败归属「被掐断的那一轮」：新对话页发送时 abort 旧流，
        // 这条 error 应打给旧页，新页面不该弹连接异常
        const msg = (err && err.errMsg) || '';
        const rid = msg.indexOf('abort') !== -1 ? this._killedRid : null;
        this._emit('error', {
          __rid: rid == null ? undefined : rid,
          code: 'SSE_FAIL',
          message: '连接异常'
        });
      }
    });
    this.task = task;

    // 记录响应内容类型，用于 success 阶段判断是否真走了 SSE。
    // 能力判断不能省：老基础库没有 onHeadersReceived，直接调用会在这里抛 TypeError
    if (typeof task.onHeadersReceived === 'function') {
      task.onHeadersReceived((res) => {
        const header = (res && res.header) || {};
        const ct = header['Content-Type'] || header['content-type'] || '';
        this._isSSE = ct.indexOf('text/event-stream') !== -1;
      });
    }

    // 增量收包：ArrayBuffer -> 增量 UTF-8 解码 -> 累积 -> 切帧派发。
    // 同样做能力判断：不支持分块时整段响应会在 success 里给，由那里的兜底解析接管
    if (typeof task.onChunkReceived === 'function') {
      task.onChunkReceived((res) => {
        this._chunkSeen = true;
        const data = res.data;
        const text =
          typeof data === 'string' ? data : this._decodeChunk(new Uint8Array(data));
        this._buffer += text;
        this._drain();
      });
    }
  }

  // 非 SSE 响应的用户文案：网关在写流之前就拒绝时会回标准 JSON 信封，
  // 里面才是「今日提问次数已用完」「提问过长」这类真实原因，
  // 直接吞掉会让用户以为服务挂了
  _errorMessage(res, code) {
    if (code === 401) return '登录已失效，请重试';
    try {
      const raw = res && res.data;
      const text =
        typeof raw === 'string' ? raw : this._decodeChunk(new Uint8Array(raw || []));
      const body = JSON.parse(text || '{}');
      if (body && typeof body.message === 'string' && body.message) return body.message;
    } catch (e) {
      // 非 JSON（502 页面等）走下面的兜底文案
    }
    if (code === 429) return '请求过于频繁，请稍后再试';
    return '服务响应异常';
  }

  // 增量 UTF-8 解码：新字节拼到残留字节后尽量多解出完整字符，
  // 不完整的多字节开头留到 _pending（解决中文被分帧截断）
  _decodeChunk(bytes) {
    const all = new Uint8Array(this._pending.length + bytes.length);
    all.set(this._pending, 0);
    all.set(bytes, this._pending.length);
    const out = [];
    let i = 0;
    while (i < all.length) {
      const b = all[i];
      let len = 1;
      if (b < 0x80) len = 1;
      else if ((b & 0xe0) === 0xc0) len = 2;
      else if ((b & 0xf0) === 0xe0) len = 3;
      else if ((b & 0xf8) === 0xf0) len = 4;
      else {
        i++;
        continue; // 非法字节，跳过
      }
      if (i + len > all.length) break; // 不完整，等下一帧
      let cp;
      if (len === 1) cp = b;
      else if (len === 2) cp = ((b & 0x1f) << 6) | (all[i + 1] & 0x3f);
      else if (len === 3)
        cp = ((b & 0x0f) << 12) | ((all[i + 1] & 0x3f) << 6) | (all[i + 2] & 0x3f);
      else
        cp =
          ((b & 0x07) << 18) |
          ((all[i + 1] & 0x3f) << 12) |
          ((all[i + 2] & 0x3f) << 6) |
          (all[i + 3] & 0x3f);
      out.push(cp);
      i += len;
    }
    this._pending = all.subarray(i);
    let s = '';
    for (let k = 0; k < out.length; k++) s += String.fromCodePoint(out[k]);
    return s;
  }

  // 按 SSE 帧分隔符 \n\n 切出完整事件块并解析。
  // 逐个 slice 缓冲是 O(帧数 × 缓冲长)；这里只游标前进、结束时一次性裁掉已消费前缀
  _drain() {
    const buf = this._buffer;
    let start = 0;
    let idx;
    while ((idx = buf.indexOf('\n\n', start)) !== -1) {
      this._parseBlock(buf.slice(start, idx));
      start = idx + 2;
      // 监听器里可能已开新流（_open 重置了缓冲），此时不能把旧缓冲写回去
      if (this._buffer !== buf) return;
    }
    if (start > 0) this._buffer = buf.slice(start);
  }

  _parseBlock(block) {
    let event = 'message';
    const dataLines = [];
    const lines = block.split('\n');
    for (let li = 0; li < lines.length; li++) {
      const line = lines[li];
      if (line === '') continue;
      if (line[0] === ':') continue; // 注释行
      if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        // SSE：冒号后可选一个空格
        dataLines.push(line.charAt(5) === ' ' ? line.slice(6) : line.slice(5));
      }
      // id / retry 等字段忽略
    }
    const dataStr = dataLines.join('\n');
    let data = dataStr;
    try {
      data = JSON.parse(dataStr);
    } catch (e) {
      // 解析失败保留原文，交给上层判断
    }
    this._handleEvent({ event, data });
  }

  // 分发服务端事件到页面监听器；done 标记本轮正常结束
  _handleEvent(event) {
    const { event: type, data } = event || {};
    switch (type) {
      case 'token':
      case 'card':
      case 'table':
      case 'status':
      case 'tool_start':
      case 'tool_end':
      case 'sources':
      case 'error':
      case 'done':
        if (type === 'done') this._ended = true;
        this._emit(type, data);
        break;
      case 'ping':
        // 心跳：SSE 无协议层 pong，业务层无需回应
        break;
      default:
        console.log('[AgentSSE] unknown event:', type);
    }
  }

  on(event, callback) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(callback);
  }

  // 不传 callback 时摘掉该事件的全部监听（页面 onUnload 用）
  off(event, callback) {
    if (!this.listeners[event]) return;
    if (!callback) {
      delete this.listeners[event];
      return;
    }
    this.listeners[event] = this.listeners[event].filter((cb) => cb !== callback);
  }

  _emit(event, data) {
    // 请求级事件统一打「发起轮」戳；data 里显式带了 __rid 则以它为准
    //（供 abort 失败归属被掐断的那轮）
    const tagged = this._tag(data);
    const cbs = this.listeners[event] || [];
    cbs.forEach((cb) => {
      try {
        cb(tagged);
      } catch (e) {
        console.error('[AgentSSE] listener error:', e);
      }
    });
  }

  // 给事件数据补 __rid；非对象数据包一层，保证页面侧取戳不炸。
  // 直接写回解析产物：每个事件都是 JSON.parse 的新对象，不必为打戳再拷一份（token 事件最频繁）
  _tag(data) {
    const rid = this._reqId || 0;
    if (data && typeof data === 'object') {
      if (data.__rid == null) data.__rid = rid;
      return data;
    }
    return { __rid: rid, data: data };
  }

  // 当前在途轮的 rid：页面卸载时用它判断「在跑的这条流是不是本页发起的」，
  // 是才断开——单例可能正被栈里另一个对话页使用
  currentRequestId() {
    return this._reqId;
  }

  // 中止当前流（发送新提问 / 新建会话 / 断开时调用）
  _abort() {
    // 代次自增放在最前：即使当前没有 task（例如正卡在 401 重登里），
    // 也要让那一次异步重放失效
    this._gen += 1;
    if (this.task) {
      // 记下被掐断的轮次：abort 触发的 fail 要把 error 归属这一轮，
      // 而不是让随后新起的那一轮误弹「连接异常」
      this._killedRid = this._reqId;
      try {
        this.task.abort();
      } catch (e) {
        // ignore
      }
      this.task = null;
    }
  }

  // 主动断开：中止在途请求（SSE 无持久连接，无需关 socket）
  disconnect() {
    this._abort();
  }

  // 新建会话：换 session_id，后端上下文随之重置
  newSession() {
    this._abort();
    this.sessionId = generateSessionId();
  }
}

const agentSocket = new AgentSSE();
export default agentSocket;
