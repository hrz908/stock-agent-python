// 对话页：消息收发走 Agent SSE（services/agent.js 单例），token 边收边按帧逐字揭示；
// 纯文本走自绘气泡 + markdown（utils/markdown.js：段落定型后不再重算，只重解析尾段），
// 行情/K线等结构化数据走自绘卡片消息；
// 「新对话」压栈本页新实例，每实例一条独立 session + 独立消息状态（见 onNewChat / NEW_CHAT_URL）。

// 注意：这里只能用默认导入。「默认 + 命名」混合导入会让 devtools 的 SWC 热编译引入
// @swc/runtime/_interop_require_wildcard（本项目不存在），chat.js 加载即抛错、页面方法全丢
//（表现为 WXML 正常渲染但点击全部无效）。session_id 走 agentSocket.freshSessionId()。
import agentSocket from '../../services/agent';
import { startRecognize, stopRecognize, abortRecognize } from '../../services/asr';
import * as translator from '../../services/translate';
import * as tts from '../../services/tts';
import { takeStableSegment } from '../../utils/segment';
import {
  listSessions,
  createSession,
  sessionMessages,
  deleteSession
} from '../../services/sessions';
import { createMarkdownStream, dedupeParagraphs } from '../../utils/markdown';
import { fontScalePatch } from '../../utils/font-scale';
// 消息与卡片映射：纯函数模块，字段口径与 components/finance/* 的 props 一一对应
import {
  assistantMsg,
  userMsg,
  quoteCard,
  klineCard,
  valuationCard,
  fundflowCard,
  compareCard,
  reportCard,
  researchCard,
  eventCard
} from './cards';
import { list as listQuickAsks, click as reportQuickAskClick } from '../../services/quickAsk';
import { ensureLogin } from '../../utils/request';
import { isDev } from '../../config';

// 侧边抽屉导航项：每行 = 文字 + 右侧箭头（左侧不放图标）
// 不列「主页」：抽屉本来就只在主页存在，这一项点了没地方去
const MENUS = [
  { key: 'kline', label: '卡片K线' },
  { key: 'watchlist', label: '自选股浮动' },
  { key: 'alerts', label: '提醒推送' },
  { key: 'settings', label: '设置' }
];

// 首屏引导问题：正常走后端 /quick-asks（热度候选池 + 加权抽样 + 状态选模板 +
// 会话去重），这里只保留本地兜底——接口不可用（离线 / 未登录 / 池子为空）时
// 首屏仍然有东西可点，不能是一张白纸
const FALLBACK_STOCKS = [
  { code: '600519', name: '贵州茅台' },
  { code: '300750', name: '宁德时代' },
  { code: '002594', name: '比亚迪' },
  { code: '600036', name: '招商银行' },
  { code: '300059', name: '东方财富' },
  { code: '000858', name: '五粮液' },
  { code: '601899', name: '紫金矿业' },
  { code: '002475', name: '立讯精密' }
];
// 兜底问句：与后端 template.go 同一口径（只代入股票名也通顺，且现有工具答得出）
const FALLBACK_TEMPLATES = [
  { tag: '盘面', build: (s) => `${s.name}现在什么价？` },
  { tag: '资金', build: (s) => `${s.name}最近主力资金在流入还是流出？` },
  { tag: '估值', build: (s) => `${s.name}现在的市盈率和市净率是多少？` },
  { tag: '风险', build: (s) => `${s.name}最近有哪些需要留意的风险？` }
];

// 抽三条不重复的股票，逐个套模板；展示与发送用同一句（兜底态不拼额外限定）
function buildFallbackQuickAsks() {
  const pool = FALLBACK_STOCKS.slice();
  return FALLBACK_TEMPLATES.map((tpl, i) => {
    const idx = Math.floor(Math.random() * pool.length);
    const stock = pool.splice(idx, 1)[0] || FALLBACK_STOCKS[0];
    const display = tpl.build(stock);
    return {
      id: `local_${stock.code}_${Date.now()}_${i}`,
      display,
      query: display,
      stock_code: stock.code,
      stock_name: stock.name,
      tag: tpl.tag
    };
  });
}

// 抽屉导航项 → 子页面路由，key 与 MENUS 一一对应
const ROUTES = {
  watchlist: '/pages/watchlist/watchlist',
  alerts: '/pages/alerts/alerts',
  settings: '/pages/settings/settings',
  kline: '/pages/kline/kline'
};

// 「新对话」：注册一条新 session 后压栈本页的一个新实例 = 一条全新会话
// （独立消息、独立会话），旧对话留在栈里可返回；
// 从新对话页进子页，返回照样回到新对话页。?session= 带上注册好的 id
const NEW_CHAT_URL = '/pages/chat/chat?new=1';

// 触发抽屉开关的最小横向滑动距离（px）
const SWIPE_MIN = 40;

// 发送看门狗：超过这个时长没有任何新内容，就认为这一轮卡死（毫秒）。
// 语义是「空闲超时」不是「首包超时」——每收到一个 token 都会重置；
// 否则正文出到一半服务端静默挂掉时 sending 会一直为 true，用户再也发不出去
const SEND_TIMEOUT_MS = 45000;

// 输入栏胶囊高度（rpx）：按「两行」预估，见 chat.wxss 的 .sender-bar。
// 首屏按最大高度预留才不会盖住最后一条消息，onReady 实测后会按真实高度修正。
const SENDER_BAR_RPX = 136;
// 输入栏上方留白（rpx）：取自 .sender-wrap 的 padding-top（--space-md）
const SENDER_PAD_TOP_RPX = 16;
// 输入栏下方留白（rpx）：取自 .sender-bar 的 margin-bottom（--space-md）；
// 必须与 chatHeight 的估算一起算，否则首帧会少让出这 8px
const SENDER_BAR_MARGIN_BOTTOM_RPX = 16;
// 卡片外边距：自绘胶囊后没有组件外边距了，只保留底部与安全区之间的距离
const SENDER_MARGIN_TOP_RPX = 0;
const SENDER_MARGIN_BOTTOM_PX = 12;
// 对话区底边与输入栏卡片之间的间距（rpx）：原值 29 含组件 margin-top 的 24rpx，
// margin-top 归零后改为 5，不同步减会留下那段空隙
const SENDER_GAP_RPX = 5;
// 键盘弹起时，输入栏在键盘顶边之上再抬的间距（rpx）。
// 只按键盘高度上抬的话，胶囊底边正好压在键盘顶边上：机型不同（键盘高度、
// 安全区、是否有候选栏）看起来时紧时松。这里统一加一段固定间距，
// 与键盘多高无关；用 rpx 是为了跟其他间距一样随屏幕宽度等比缩放，
// 各尺寸下观感一致（换算在 onLoad 里做，chatHeight 用的是 px）
const KEYBOARD_GAP_RPX = 16;

// 消息结构与八张卡的字段映射全部在 ./cards.js（纯函数，可脱离页面单测）：
// 这里只负责把 SSE 事件转成 message 塞进列表，不再写字段映射。

// ===== 卡片 =====
// 一次回复最多出一张卡（后端按问题意图只挑一张），排在正文之后：
// 后端在正文流式结束后才下发，收到即追加到回复末尾（见 _placeCard），
// 同类卡片一轮只落一张（见 _queueCard）。

// ===== 语音输入（同声传译）=====
// 点一下开始聆听：面板里边出原文边出译文，译文逐段补齐并自动播报；
// 再点一下把**原文**发给 agent（译文只做展示与播报，不参与提问）。
// 收尾有两条路：手动点停 = 立即发送；静音 1.5s 自动停 = 终稿停在面板，
// 仍要再点一次才发（保留改口机会，见 DESIGN.md 对「松手即发」的否决）。
const REC_MIN_MS = 1000;
// 单条语音上限：聊天一句话用不到 60s，缩短后到点也能更早出终稿
const REC_MAX_MS = 30000;
// 静音判停：说完停 1.5s 就收尾（只停录，不发送）
const VOICE_SILENCE_MS = 1500;
// 语音键文案：未开始 / 聆听中 / 等终稿 / 已停录待发
const VOICE_TIP_IDLE = '点击聆听';
const VOICE_TIP_LIVE = '再点发送';
const VOICE_TIP_STOP = '正在转写…';
const VOICE_TIP_HOLD = '点发送';
// 中间结果的视图刷新间隔：识别回调远快于渲染需要，合批后只落最后一次
// （150ms 是同传手感与 setData 次数之间的平衡点）
const VOICE_TEXT_FLUSH_MS = 150;
// 稳定片段：攒够 4 个字就送翻（与识别并行），长句切到 28 字
const VOICE_SEG_MIN = 4;
const VOICE_SEG_MAX = 28;
// 播报队列上限：落后太多说明播放追不上识别，丢最旧的，别让耳朵追几分钟前的话
const VOICE_SPEECH_MAX = 6;
// 同传方向：源语言决定识别语言，目标语言决定译文与播报音色
const VOICE_LANGS = {
  zh2en: { from: 'zh_CN', to: 'en_US', asr: 'zh_CN', tts: 'en_US', label: '中 → EN' },
  en2zh: { from: 'en_US', to: 'zh_CN', asr: 'en_US', tts: 'zh_CN', label: 'EN → 中' }
};

// 流式渲染节奏：逐字推进的帧间隔(ms)、追赶系数、单帧推进上限、
// 滚动跟随的去抖间隔(ms)。
// 逐字而非整段落视图：token 到达速度远快于人的阅读速度，一次性铺满会让
// 文字「跳」出来；这里按帧揭示，正常节奏下每帧 1 字，服务端一次给一大段时
// 按积压比例加速，把延迟稳定在半秒左右，既不卡顿也不会拖成长篇打字动画。
// 版式与文字同帧推进（见 _renderStream）：每帧只 patch 正在写的那一段，
// 所以帧间隔不必为了省的 CPU 再拉长
const TYPE_TICK_MS = 28;
const TYPE_CATCHUP = 18;
const TYPE_MAX_STEP = 12;
const SCROLL_FOLLOW_MS = 80;

// 历史消息分片：首屏只渲染最近一批，更早的按页向前补，
// 避免长会话一次性解析 + 渲染几百条消息把首屏拖住
const MSG_INITIAL = 30;
const MSG_PAGE = 20;

// 加载态节奏：文案跟着真实耗时推进，先给一句轻的，超过阈值才承认「这次慢」。
// 阈值按用户能忍的节奏定——8 秒内没有任何解释会觉得卡死，20 秒以上必须给台阶
const LOAD_SEARCH_MS = 1600;   // 理解 → 检索：首个 token 迟迟不来时换成交代动作
const LOAD_LONG_MS = 12000;    // 检索 → 核对整理：进入「耗时较长」档
const LOAD_FINAL_MS = 26000;   // 核对 → 仍在整理：超长档，补一句安抚
// 生成停顿：一帧打完、下一段 token 还没到，隔这么久才补「正在继续生成」，
// 正常节奏下这句话不会亮，避免全程挂一条多余说明
const STREAM_STALL_MS = 1200;

// 加载态文案表。只写「我们确实在做的事」，不承诺剩余时间、不催用户等待。
// 没有「重连」档：SSE 是每问一个请求，失败即整轮失败，不存在后台重连状态
const LOAD_COPY = {
  think: { text: '正在理解你的问题' },
  search: { text: '正在查找行情与资料' },
  read: { text: '正在整理查到的资料' },
  long: { text: '正在核对数据并组织回答', hint: '这次要查的资料比较多' },
  final: { text: '还在整理，内容比平时多', hint: '内容较长时会慢一些，请再稍等' }
};

Page({
  data: {
    drawerTop: 44,
    input: '',
    sending: false,
    padBottom: 0,
    // 输入栏容器的实际下内边距：键盘弹起时安全区在键盘之下，不再留白
    senderPadBottom: 0,
    // 键盘弹起时输入栏容器向上偏移的距离（px）
    keyboardOffset: 0,
    chatHeight: 0,
    drawerOpen: false,
    // 「新对话」按钮的创建中状态：注册会话期间按钮转圈 + 文案换成「创建中…」，
    // 同时兼作连点闸门（一次点击 = 一条会话，见 onNewChat）
    newChatting: false,
    // 对话区滚动位置：用数值 scroll-top 控制。
    // 旧写法 scroll-into-view 每次都要「先清空锚点再置位」，会不停重启滚动动画。
    // 现在只有「用户主动发消息 / 切换会话」这类显式场景才定位到底部；
    // AI 回复期间不再跟随，新内容向下溢出，视口停在用户当前看的位置
    scrollTop: 0,
    scrollAnimated: false,
    menus: MENUS,
    // 历史对话列表（抽屉）：{ session_id, title, updated_at, active }，
    // active 标记当前会话（选中效果），loadSessions 时按 _sessionId 计算
    sessions: [],
    // 删除会话确认弹窗（胶囊弹层，替代原先的原生 wx.showModal）
    deleteVisible: false,

    // 输入模式：false = 键盘输入，true = 语音输入（模式键在两者之间切换）
    voiceMode: false,
    // textarea 的受控 focus：切回键盘态时靠它主动唤起键盘
    inputFocus: false,
    // 语音输入：recording = 正在聆听（再点一下收尾并发出）
    recording: false,
    voiceTip: VOICE_TIP_IDLE,
    // 聆听中实时识别出的文字：显示在输入栏上方的预输入框里
    voiceText: '',
    // 静音自动停录后，终稿停在面板上等用户点发送（此时 recording 为 false）
    voiceHolding: false,
    // 译文：按稳定片段逐段补齐；voiceTranslating 控制「翻译中…」占位
    voiceDst: '',
    voiceTranslating: false,
    voiceDstFailed: false,
    // 同传方向（中→EN / EN→中）与播报开关
    voiceDir: 'zh2en',
    voiceLangLabel: VOICE_LANGS.zh2en.label,
    voiceSpeak: true,
    // 空会话不铺问候气泡：首屏直接交给下方的引导问题
    messages: [],
    // 历史消息分片开关：hasEarlier 为真时列表顶部出现「查看更早」入口
    hasEarlier: false,
    earlierCount: 0,
    // 首屏引导问题：onLoad 里随机生成（行情 / 资金 / 估值三类意图），
    // 点一条即发送，省掉「我能问什么」这一轮思考
    quickAsks: [],
    // ===== 加载态（发送后到首个 token 到达前）=====
    // 只用一个块表达：占在 AI 回复该出现的位置，文案随阶段变化。
    // 拆成「思考气泡 + 底部状态条」两处会出现两套动效同时转的情况
    loadVisible: false,
    loadText: '',
    // 长耗时档才出现的第二行说明；平时为空，块高不会多占一行
    loadHint: '',
    // 生成中光标之后的补充说明：只在正文停顿（等服务端下一段）时亮起
    streamStalled: false,
    streamStallText: ''
  },

  onLoad(options) {
    // 先铺本地兜底，再用后端结果替换：接口慢或失败时首屏不留白
    this.setData({ quickAsks: buildFallbackQuickAsks() });

    // 每个对话页实例 = 一条独立会话：
    // - 带 ?session=<id>（新对话注册后压栈 / 历史列表点选）：复用该会话，
    //   后端 thread_id 由它决定，打开即恢复这条对话的上下文；
    // - 不带参（首页进入）：现场生成一条，首条消息发出时才进历史列表
    const resumeSid = options && options.session
      ? decodeURIComponent(options.session)
      : '';
    // ?new=1 表示这是「新对话」刚压栈出来的空会话：会话是这一刻才注册的，
    // 消息必为空，不需要回拉（回拉还要多等一个请求，且空结果会覆盖首屏）
    const isNewChat = !!(options && String(options.new) === '1');
    this._sessionId = resumeSid || agentSocket.freshSessionId();
    // 会话 id 定了才能拉后端建议：去重是按 session 分桶的
    this.loadQuickAsks();
    // 本页发起的最新一轮 rid：agent 单例的请求级事件全带 __rid，
    // 只渲染自己发起的那轮，旧页在途流的 token 不会串进本页
    this._myReqId = 0;
    // 历史消息加载序号：连点两条会话时，先发后到的响应不许覆盖后选的会话
    this._msgSeq = 0;
    // 发送序号：登录是异步的，登录回来时这一轮可能已被看门狗/切会话收掉，
    // 甚至用户已经发了下一条——只有序号仍是本轮才允许真正开流
    this._sendSeq = 0;
    // 抽屉收起动画是否已完成：新对话跳转要等它，否则新页面会盖掉动画。
    // 初始为 true（页面刚进、抽屉本来就是关的，没有动画要等）
    this._drawerClosed = true;
    // 本页挂在 agent 单例上的监听（event -> callback），onUnload 按引用精确摘除
    this._agentHandlers = {};
    // 语音同传的播报队列：译文按片段到达，串行播，同一时刻只有一句在响
    this._speechQueue = [];
    this._speaking = false;
    this._voiceDstParts = [];
    this._tr = null;

    let statusBarHeight = 44;
    let safeBottom = 0;
    let windowHeight = 667;
    let windowWidth = 375;
    try {
      const win = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      statusBarHeight = win.statusBarHeight || 44;
      windowHeight = win.windowHeight || 667;
      windowWidth = win.windowWidth || 375;
      safeBottom =
        (win.safeAreaInsets && win.safeAreaInsets.bottom) ||
        (win.screenHeight && win.safeArea ? win.screenHeight - win.safeArea.bottom : 0) ||
        0;
    } catch (e) {
      // 保持默认值
    }

    // 导航栏高度 = 状态栏 + 44px 内容区（自研 navbar 高度确定，无需实测）
    const navH = statusBarHeight + 44;
    const padBottom = Math.max(0, safeBottom);
    // rpx → px：chatHeight 是按 px 算的，设计稿的 rpx 要先换算
    const rpx = windowWidth / 750;
    // 输入栏让出的空间（首帧估算值，onReady 里会按实测值修正）：
    // 胶囊 + 上下留白 + 与对话区的间距 + 底部余量，安全区内边距在下面单独扣
    const senderH =
      (SENDER_BAR_RPX + SENDER_PAD_TOP_RPX + SENDER_BAR_MARGIN_BOTTOM_RPX + SENDER_GAP_RPX) * rpx +
      SENDER_MARGIN_BOTTOM_PX;

    this._baseH = windowHeight;
    this._curH = windowHeight;
    this._navH = navH;
    this._senderH = senderH;
    this._senderMarginTop = SENDER_MARGIN_TOP_RPX * rpx;
    this._senderGap = SENDER_GAP_RPX * rpx;
    // 键盘弹起时额外上抬的间距（px）：键盘高度是 px，这里一次性换算好
    this._keyboardGap = KEYBOARD_GAP_RPX * rpx;
    this._measureTimer = 0;
    this._sendWatchdog = 0;
    this._kbHeight = 0;
    // 流式输出追踪：当前 streaming 消息在 messages 里的下标（-1 表示无）、
    // 已收到的全文、已揭示到第几个字、逐字推进的定时器
    this._streamIndex = -1;
    this._streamText = '';
    this._streamShown = 0;
    // 流式 markdown 解析器（已定型段落 + 正在写的尾段）与尾段块数：
    // 尾段块数变化（或段落定型）时视图层要整段重写 blocks，否则只 patch 尾部下标
    this._mdStream = null;
    this._mdTailLen = 0;
    // 本轮 RAG 引用来源（标题去重后挂在 assistant 气泡底部）
    this._roundSources = [];
    // 历史消息分片：池子里是尚未渲染的更早消息
    this._historyPool = [];
    this._typeTimer = 0;
    // 加载态：本轮开始时刻、工具阶段（running/done）、三档定时器、生成停顿定时器
    this._loadAt = 0;
    this._toolLabel = '';
    this._toolPhase = '';
    this._loadTimers = [];
    this._stallTimer = 0;
    // 本轮是否已收到 done（等文字打完才真正收尾）
    this._streamClosing = false;
    // 本轮是否已经开始出正文：正文一出现，加载块就让位（加载文案不再刷）
    this._roundTextStarted = false;
    // 本轮已落位的卡片类型：同类卡片只生成一份（重复事件直接丢掉）
    this._cardTypes = {};

    // 抽屉内容避让状态栏：statusBarHeight 下方再留 28px（原 44，按需求整体上移一点）。
    // 抽屉在左侧、胶囊菜单在右上，横向不重叠，故减小留白不会撞上菜单按钮
    const drawerTop = statusBarHeight + 28;
    const chatHeight = Math.max(160, windowHeight - navH - senderH - padBottom);

    this.setData({
      drawerTop,
      padBottom,
      senderPadBottom: padBottom,
      keyboardOffset: 0,
      chatHeight
    });
    // 恢复历史会话的消息（空会话保持空列表，首屏只剩引导问题）。
    // 新对话（?new=1）跳过：会话刚注册、必为空，不用多打一次接口。
    // 这里不预先滚到底：空会话滚到底会把问候语顶出屏幕，滚动交给消息回填后
    if (resumeSid && !isNewChat) this.loadSessionMessages(resumeSid);

    if (wx.onWindowResize) {
      this._onResize = (res) => {
        const h = (res && res.size && res.size.windowHeight) || this._baseH;
        if (h === this._curH) return;
        this._curH = h;
        this.applyKeyboard();
      };
      wx.onWindowResize(this._onResize);
    }

    // 接入 Agent SSE：先挂监听，发送时由 agentSocket 内部按提问开流
    this._initAgentSocket();
  },

  // ===== Agent SSE =====
  // agentSocket 是单例；这里只负责挂/摘本页的监听（SSE 为每问一请求，无持久连接）。
  // 新对话页压栈后，单例被多个对话页实例共享，因此：
  // 1. 请求级事件统一走 on() 包一层 rid 过滤——只渲染本页自己发起的那一轮
  //    （__rid 由 services/agent 在 sendMessage 时打下），旧页在途流的
  //    token / 卡片不会串进新页的空会话；
  // 2. 监听引用全部存进 this._agentHandlers，onUnload 按引用精确摘除——
  //    off(event) 不传 callback 会清空该事件的全部监听，把另一个实例的一起误删
  _initAgentSocket() {
    const on = (ev, fn) => {
      const wrapped = (data) => {
        if (!data || data.__rid !== this._myReqId) return;
        fn(data);
      };
      this._agentHandlers[ev] = wrapped;
      agentSocket.on(ev, wrapped);
    };

    // 流式 token：逐字追加到当前 streaming 状态的 assistant 消息。
    // 性能约束：token 频率很高，不能每次都把整个 messages 回传给视图层——
    // 增量先积在 _streamText 里，交给逐字定时器按帧揭示（见 _typeTick /
    // _renderStream）：每帧只把正在写的那一段补进 blocks
    on('token', (data) => {
      const delta = (data && data.delta) || '';
      if (!delta) return;
      this._appendToken(delta);
    });

    // 结构化卡片：八类（事件契约见 agent 的 _emit_structured 与 server/README.md）。
    // 后端一次回复只发一张，且在正文之后，收到即追加到回复末尾（见 _queueCard）
    on('card', (data) => {
      if (!data || !data.payload) return;
      let msg = null;
      if (data.type === 'quote_card') msg = quoteCard(data.payload);
      else if (data.type === 'valuation_card') msg = valuationCard(data.payload);
      else if (data.type === 'fundflow_card') msg = fundflowCard(data.payload);
      else if (data.type === 'report_card') msg = reportCard(data.payload);
      else if (data.type === 'compare_card') msg = compareCard(data.payload);
      else if (data.type === 'research_card') msg = researchCard(data.payload);
      else if (data.type === 'event_card') msg = eventCard(data.payload);
      if (!msg) return;
      this._queueCard(msg);
    });

    // K 线表格：payload 约定为 { rows: [...] }，兼容直接给数组；
    // 同轮查过行情时后端会补上 code / name，一并交给卡片当标题
    on('table', (data) => {
      if (!data || data.type !== 'kline_table' || !data.payload) return;
      const rows = data.payload.rows || data.payload;
      if (!Array.isArray(rows) || !rows.length) return;
      const msg = klineCard(rows, data.payload);
      if (!msg) return;
      this._queueCard(msg);
    });

    // 后端主动推的状态提示：当作本阶段的文案（优先级低于长耗时档）
    on('status', (data) => {
      const label = (data && data.label) || '';
      if (!label) return;
      this._toolLabel = label;
      this._toolPhase = 'running';
      this._refreshLoad();
    });

    // 工具调用/结束：只驱动加载态文案，不用占位卡表达取数进度。
    // 取数回来到首个 token 之间是模型在组织语言，换成「正在整理」
    on('tool_start', (data) => {
      this._toolLabel = (data && data.label) || '正在获取数据';
      this._toolPhase = 'running';
      this._refreshLoad();
    });

    on('tool_end', () => {
      this._toolLabel = '';
      this._toolPhase = 'done';
      this._refreshLoad();
    });

    // 知识库引用来源：后端检索完立刻发（早于正文），挂在 assistant 气泡底部。
    // 只取标题并去重——chunk_id / score 是内部定位信息，对用户没有意义
    on('sources', (data) => {
      const docs = (data && data.docs) || [];
      const titles = [];
      for (let i = 0; i < docs.length; i++) {
        const t = docs[i] && docs[i].title ? String(docs[i].title).trim() : '';
        if (t && titles.indexOf(t) < 0) titles.push(t);
      }
      if (!titles.length) return;
      this._roundSources = titles.slice(0, 5);
      this._applySources();
    });

    // 错误：不再逐字往外吐，已收到的正文一次性显示完再收尾；
    // 已经暂存的卡仍然放出来（数据是好的），别让用户白等一轮。
    // 载荷统一为 {code, message}；data.msg 是旧版网关字段，留一版兜底
    on('error', (data) => {
      this._failSend((data && (data.message || data.msg)) || '服务异常');
    });

    // 本轮回答结束：先记下「该收尾了」，等逐字打完再真正结束（_endTurn）。
    // sending 也在那时才解锁——解锁早了，用户能在正文还在打的时候发新问题，
    // 新一轮 token 会续进上一条气泡里
    on('done', () => {
      this._clearSendWatchdog();
      this._streamClosing = true;
      this._endLoad();
      if (this._streamIndex < 0 || this._streamShown >= this._streamText.length) {
        this._endTurn();
        return;
      }
      this._startTyping();   // 等新 token 期间定时器会停，这里兜底续上
    });

    agentSocket.connect();
  },

  // 把增量文本积进缓冲；首个 token 到达时确定目标消息（没有 streaming 消息
  // 就新建一条）。token 本身不直接落视图，交给逐字定时器按帧揭示
  _appendToken(delta) {
    // 已经有内容在回来，说明这一轮还活着：把空闲看门狗往后推，而不是撤掉——
    // 中途静默挂死（没有 error/done）同样要把 sending 放出来
    this._touchSendWatchdog();
    // 首个 token 到达即收起加载块，交给流式正文；
    // 之后每来一段都会清掉「停顿说明」，文字一直在动就不需要那句补充
    this._endLoad();
    if (this._streamIndex < 0) {
      const messages = this.data.messages;
      const last = messages[messages.length - 1];
      // 解析器随流重建：已定型的段落从空开始，首帧把已有文本整段补进 blocks
      this._mdStream = createMarkdownStream();
      this._mdTailLen = 0;
      if (last && last.role === 'assistant' && last.status === 'streaming') {
        this._streamIndex = messages.length - 1;
        this._streamText = (last.content[0] && last.content[0].data) || '';
        this._streamShown = this._streamText.length;
      } else {
        const idx = messages.length;
        this._streamIndex = idx;
        this._streamText = '';
        this._streamShown = 0;
        // 只写新追加的那一项：整表回传会把历史消息（含已解析的 blocks）重传一遍
        this.setData({
          ['messages[' + idx + ']']: {
            role: 'assistant',
            status: 'streaming',
            content: [{ type: 'markdown', data: '' }],
            // 流式消息同样带 blocks：每帧只补正在写的那一段，版式随文字一起长
            blocks: [],
            // sources 早于首个 token 到达，建消息时一次性落位（见 on('sources')）
            sources: this._roundSources.slice()
          }
        });
        // 正文气泡刚建出来：把它顶进视口（之后逐字增长不再跟随，见本文件顶部说明）
        this.scrollToBottom();
      }
    }
    this._streamText += delta;
    this._roundTextStarted = true;
    this._startTyping();
  },

  // 逐字推进的驱动：只在没有定时器时挂下一帧，token 再密也不会越排越多
  _startTyping() {
    if (this._typeTimer || this._unloaded) return;
    this._typeTimer = setTimeout(() => {
      this._typeTimer = 0;
      this._typeTick();
    }, TYPE_TICK_MS);
  },

  // 一帧：按积压量决定这一帧揭示几个字，重绘后继续下一帧。
  // 积压归零即停帧（等下一个 token 拉起）：done 已经来过就收尾整轮
  _typeTick() {
    if (this._streamIndex < 0) return;
    const total = this._streamText.length;
    const backlog = total - this._streamShown;
    if (backlog <= 0) {
      if (this._streamClosing) this._endTurn();
      else {
        // 字已经追平、下一段还没来：从这里开始计停顿，真停了才补一句说明
        this._watchStreamStall();
      }
      return;
    }
    const step = Math.min(TYPE_MAX_STEP, Math.max(1, Math.ceil(backlog / TYPE_CATCHUP)));
    this._streamShown = Math.min(total, this._streamShown + step);
    this._renderStream();
    this._startTyping();
  },

  // 把当前应显示的文本落到视图。
  // 版式与文字同帧推进：已读完的段落由 mdStream 定格（每段只解析一次），
  // 每帧只重解析正在写的尾段，且只 patch 尾部的下标。
  // 反过来做（每帧整篇重解析 + 整份 blocks 回传）视图层每帧都要重建整条气泡，
  // 长答下就是肉眼可见的「一段一段地跳」。
  // 刻意不跟随滚动：正文变长只让它向下溢出，视口停在用户正在读的位置。
  _renderStream(force) {
    const idx = this._streamIndex;
    if (idx < 0 || !this._mdStream) return;
    const text = this._streamText.slice(0, this._streamShown);
    const r = this._mdStream.update(text);
    const tailLen = r.tail.length;
    const patch = {};
    if (force || r.committed || tailLen !== this._mdTailLen) {
      // 有段落定型 / 尾段块数变了：下标整体后移，只能整段重写
      patch['messages[' + idx + '].blocks'] = r.blocks.concat(r.tail);
      // 全文只在段落定型时补一次：视图层不读 content，它是「重进本页恢复在途流」的底稿
      patch['messages[' + idx + '].content[0].data'] = text;
    } else {
      const base = r.blocks.length;
      for (let j = 0; j < tailLen; j++) {
        patch['messages[' + idx + '].blocks[' + (base + j) + ']'] = r.tail[j];
      }
    }
    this._mdTailLen = tailLen;
    this.setData(patch);
  },

  // 立刻把剩下的字全部显示：出错、切会话、新一轮发送这类不能等打字机的场合。
  // 即使字已经追平也要再落一次：尾段要定型、content 要留全文（见 _renderStream）
  _snapStreamEnd() {
    if (this._typeTimer) {
      clearTimeout(this._typeTimer);
      this._typeTimer = 0;
    }
    if (this._streamIndex < 0) return;
    this._streamShown = this._streamText.length;
    this._renderStream(true);
  },

  // 流式消息收尾：status 转 complete，复位流式追踪状态。
  // 不碰 sending：解锁发送只在 _endTurn 里做（正文打完才放开）。
  // _streamClosing 必须在这里复位：error / 切会话 / 看门狗三条路径都不走 _endTurn
  _finishStreaming() {
    const idx = this._streamIndex;
    this._streamIndex = -1;
    this._streamText = '';
    this._streamShown = 0;
    this._mdStream = null;
    this._mdTailLen = 0;
    this._streamClosing = false;
    if (idx < 0) return;
    const patch = {};
    patch['messages[' + idx + '].status'] = 'complete';
    // 来源可能晚于最后一段正文到达，收尾时再补一次，免得只有正文没有出处
    if (this._roundSources.length) {
      patch['messages[' + idx + '].sources'] = this._roundSources;
    }
    this.setData(patch);
  },

  // 把已收集的引用来源挂到当前流式气泡。正文还没开始（_streamIndex < 0）时不动：
  // 那条消息建出来时会带上 this._roundSources（见 _appendToken）
  _applySources() {
    const idx = this._streamIndex;
    if (idx < 0 || !this._roundSources.length) return;
    this.setData({ ['messages[' + idx + '].sources']: this._roundSources });
  },

  // ===== 卡片落位：收到即追加到回复末尾 =====

  // 同类卡片一轮只留一张：后端已按问题意图只挑一张，这里是客户端兜底——
  // 同一类卡重复到达（重连重放 / 双路触达）时不能再渲染第二张
  _queueCard(msg) {
    const type = (msg && msg.card) || '';
    if (type && this._cardTypes[type]) return;
    if (type) this._cardTypes[type] = true;
    this._placeCard(msg);
  },

  // 落位在回复末尾。后端在正文流式结束后才下发卡片，正常时序下正文气泡已经在列表里，
  // 直接追加到它后面即可；正文为空（只有卡）时同样追加在最后。
  // 追加而不是整表回传：整份 messages 会连带历史消息（含已解析的 blocks）重传一遍。
  // 不跟底：与正文流式同一口径（回复期间视口保持不动），卡片是补充证据，
  // 用户读完正文自然向下就能看到，不该在收尾时把视口拽走。
  _placeCard(msg) {
    this.setData({ ['messages[' + this.data.messages.length + ']']: msg });
  },

  // 收尾前清理重复段落：已经逐字打出来的内容撤不回，只能在整轮结束前把
  // 「完全重复的段落」从视图里删掉（只删后出现的那段，不重写其余文字）。
  // 与后端 dedupe_answer_text 同一口径，判定不出来的近似重复不动。
  _dedupeStreamText() {
    if (this._streamIndex < 0) return;
    const deduped = dedupeParagraphs(this._streamText);
    if (deduped === this._streamText) return;
    this._streamText = deduped;
    this._streamShown = deduped.length;
    // 解析器重建：下一帧（_snapStreamEnd 的 force 渲染）要按新全文整体重算 blocks
    this._mdStream = createMarkdownStream();
    this._mdTailLen = -1;
  },

  // 一轮真正结束：收尾流式消息，最后才解锁发送
  // （解锁早了会把新一轮 token 续进这条旧气泡）
  _endTurn() {
    // 幂等：error 与 done 会先后到达（agent 出错后仍会补一帧 done），
    // 第一次收尾已把 sending 置 false，第二次直接返回，省一次 setData
    if (!this.data.sending) return;
    this._streamClosing = false;
    this._dedupeStreamText();
    this._snapStreamEnd();
    this._finishStreaming();
    this._endLoad();
    this.setData({ sending: false });
  },

  // ===== 加载态 =====
  // 一轮回答只有一处加载提示：AI 回复该出现的左侧（.ai-load）。
  // 文案按优先级取一档：重连 > 长耗时 > 工具取数 > 整理 > 检索 > 理解
  _startLoad() {
    this._loadAt = Date.now();
    this._toolLabel = '';
    this._toolPhase = '';
    this._clearLoadTimers();
    this._refreshLoad();
    // 三档定时器只管「到点重算一次」，不做文案队列：
    // 工具事件随时会插入，队列式推进会与真实阶段打架
    this._loadTimers = [
      setTimeout(() => this._refreshLoad(), LOAD_SEARCH_MS),
      setTimeout(() => this._refreshLoad(), LOAD_LONG_MS),
      setTimeout(() => this._refreshLoad(), LOAD_FINAL_MS)
    ];
  },

  _refreshLoad() {
    if (this._unloaded || !this.data.sending) return;
    // 正文已经在出：加载块让给流式消息
    if (this._roundTextStarted) return;
    const elapsed = Date.now() - (this._loadAt || Date.now());
    let copy;
    if (elapsed >= LOAD_FINAL_MS) copy = LOAD_COPY.final;
    else if (elapsed >= LOAD_LONG_MS) copy = LOAD_COPY.long;
    else if (this._toolPhase === 'running' && this._toolLabel) copy = { text: this._toolLabel };
    else if (this._toolPhase === 'done') copy = LOAD_COPY.read;
    else if (elapsed >= LOAD_SEARCH_MS) copy = LOAD_COPY.search;
    else copy = LOAD_COPY.think;
    this.setData({
      loadVisible: true,
      loadText: copy.text,
      loadHint: copy.hint || ''
    });
  },

  _endLoad() {
    this._clearLoadTimers();
    this._clearStreamStall();
    if (!this.data.loadVisible && !this.data.streamStalled) return;
    this.setData({
      loadVisible: false,
      loadText: '',
      loadHint: '',
      streamStalled: false,
      streamStallText: ''
    });
  },

  _clearLoadTimers() {
    // 每个 token 都会走到这里，空数组时直接返回（token 频率高，别做无意义清理）
    if (!this._loadTimers || !this._loadTimers.length) return;
    this._loadTimers.forEach(clearTimeout);
    this._loadTimers = [];
  },

  // 正文停顿：字追平了但这一轮还没结束，说明在等服务端下一段。
  // 真停够 STREAM_STALL_MS 才亮一句说明，正常节奏下不会出现
  _watchStreamStall() {
    if (this._stallTimer || this._unloaded || this._streamIndex < 0) return;
    this._stallTimer = setTimeout(() => {
      this._stallTimer = 0;
      if (this._unloaded || this._streamIndex < 0) return;
      if (this._streamShown < this._streamText.length) return;
      this.setData({
        streamStalled: true,
        // 长回答的停顿更让人怀疑是不是卡住，文案点明「还在写」而不是「卡住了」
        streamStallText: this._streamText.length > 400 ? '回答较长，正在继续输出…' : '正在继续生成…'
      });
    }, STREAM_STALL_MS);
  },

  _clearStreamStall() {
    if (this._stallTimer) {
      clearTimeout(this._stallTimer);
      this._stallTimer = 0;
    }
  },

  // 每次回到本页（含从设置页返回）同步全局字号档位
  onShow() {
    this.setData(fontScalePatch());
  },

  // 进后台 / 跳到子页面：别再占着麦克风，也别在后台继续播
  // （已停录待发的终稿保留，回来还能接着发）
  onHide() {
    if (this._recording) this.onVoiceCancel();
    this.stopVoiceSpeech();
  },

  onReady() {
    // 输入栏高度按实测修正（多行输入会长高，首帧估的是两行高度）；
    // 导航栏高度确定（状态栏 + 44），无需测量
    this.measureSender();
  },

  onUnload() {
    // 置位后录音回调直接放弃结果，不再往已销毁的页面上 setData
    this._unloaded = true;
    // 摘监听放在最前：后面的音频 destroy 在开发者工具模拟器里会抛异常
    // （InnerAudioContext 从未播放过时其内部实例不存在），异常会中断 onUnload，
    // 监听清理必须在此之前完成，否则旧页会一直挂在单例上收事件
    // 必须按引用逐个摘：不传 callback 的 off() 会清空该事件的全部监听，
    // 新对话压栈场景下会把另一个对话页实例的监听一起误删
    Object.keys(this._agentHandlers || {}).forEach((ev) => {
      agentSocket.off(ev, this._agentHandlers[ev]);
    });
    this._agentHandlers = {};
    // 在途流已无人消费：本页发起的那条才断（单例可能被另一个对话页占着），
    // 否则返回上一页后仍在后台收包、白耗流量与电量
    if (this.data.sending && this._myReqId === agentSocket.currentRequestId()) {
      agentSocket.disconnect();
    }
    if (this._onResize && wx.offWindowResize) wx.offWindowResize(this._onResize);
    if (this._scrollTimer) clearTimeout(this._scrollTimer);
    if (this._measureTimer) clearTimeout(this._measureTimer);
    if (this._typeTimer) clearTimeout(this._typeTimer);
    this._clearSendWatchdog();
    this._clearLoadTimers();
    this._clearStreamStall();
    // 语音：走的时候停干净，否则插件还在后台录音
    if (this._recording) abortRecognize();
    if (this._voiceTextTimer) {
      clearTimeout(this._voiceTextTimer);
      this._voiceTextTimer = 0;
    }
    // 同传：在途译文作废、播报停掉并销毁音频上下文（否则下次进入第一次播报没声音）
    this._tr = null;
    this._voiceDstParts = [];
    this._speechQueue.length = 0;
    this._speaking = false;
    tts.release();
  },

  // sender-wrap 是绝对定位容器（自成 BFC，子元素外边距不会合并出去），
  // 它的高度 = 卡片上外边距 + 卡片 + 卡片下外边距 + 容器的下内边距。
  // 扣掉上外边距与下内边距（padBottom 在对话区高度里另算）就是卡片 + 下边距，
  // 再加上设计间距，才是对话区真正要让出的空间。
  // 注意下内边距要读实时的 senderPadBottom：键盘弹起时它是 0
  measureSender() {
    if (!wx.createSelectorQuery) return;
    wx.createSelectorQuery()
      .in(this)
      .select('.sender-wrap')
      .boundingClientRect((rect) => {
        if (!rect || !rect.height) return;
        this._senderH =
          rect.height - this.data.senderPadBottom - this._senderMarginTop + this._senderGap;
        this.applyKeyboard();
      })
      .exec();
  },

  // 输入多行时卡片会长高（textarea 开了 auto-height），键盘收放后重测一次。
  // 这里做节流：连续触发只保留最后一次，且测量本身不会再触发新任务，不会成环
  scheduleMeasureSender() {
    if (this._measureTimer) clearTimeout(this._measureTimer);
    this._measureTimer = setTimeout(() => {
      this._measureTimer = 0;
      this.measureSender();
    }, 320);
  },

  // 输入事件（textarea）
  onInput(e) {
    this.setData({ input: (e && e.detail && e.detail.value) || '' });
    // 单行 -> 两行时胶囊会变高，对话区要让出相应空间，否则最后一条消息会被压住。
    // 这里走节流测量：连续打字只在停顿时量一次
    this.scheduleMeasureSender();
  },

  // focus 由受控字段回写：切回键盘态时要靠它主动唤起键盘，
  // 用户自己点输入框时也会走到这里，保持视图与状态一致
  onInputFocus() {
    if (!this.data.inputFocus) this.setData({ inputFocus: true });
  },

  onInputBlur() {
    if (this.data.inputFocus) this.setData({ inputFocus: false });
  },

  // 键盘 / 语音互切。语音态下整块不渲染 textarea，键盘随之收起；
  // 再点一次切回键盘态，并主动聚焦把键盘唤起来
  onToggleInputMode() {
    // 聆听中 / 有停录待发的终稿时点键盘键：先丢弃这一次再切，
    // 否则终稿会落进键盘态的输入框
    if (this._recording || this.data.voiceHolding) this.onVoiceCancel();

    if (this.data.voiceMode) {
      this.setData({ voiceMode: false, inputFocus: true });
      return;
    }

    this.setData({ voiceMode: true, inputFocus: false, voiceTip: VOICE_TIP_IDLE });
    // 组件移除后键盘并不会立刻收，这里显式收一次
    if (wx.hideKeyboard) wx.hideKeyboard();
    // 键盘高度事件不一定补发 0，手动归零，防止输入栏悬在半空
    this._kbHeight = 0;
    this.applyKeyboard();
  },

  onSend() {
    // 语音态胶囊里没有文字可发：这个键的语义是「收尾并发出」，
    // 聆听中 = 停录并发送；已停录待发 = 直接发送终稿；都没有则什么都不做
    if (this.data.voiceMode) {
      if (this._recording) this.stopVoiceAndSend();
      else if (this.data.voiceHolding) this.sendHeldVoice();
      return;
    }
    this.sendText(this.data.input);
  },

  // 拉后端建议问题：失败保留本地兜底，不 toast、不打断首屏
  loadQuickAsks() {
    listQuickAsks(4, this._sessionId || '')
      .then((data) => {
        const list = (data && data.quick_asks) || [];
        if (!list.length) return;
        // 用户已经开口就不再替换，避免把建议塞进对话中段
        if (this.data.messages.length || this.data.sending) return;
        this.setData({ quickAsks: list });
      })
      .catch(() => {
        // 兜底已在 onLoad 铺好，这里静默
      });
  },

  // 首屏引导问题：展示用 display，真正发出去的是 query（带股票代码与维度，信息更完整）
  onQuickAsk(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    const q = ds.q || '';
    if (!q) return;
    // 点击上报只服务热度池，失败不影响发问，不等它的结果
    if (ds.code) reportQuickAskClick(ds.code).catch(() => {});
    this.sendText(q);
  },

  // 发送一条文本：打字与语音识别结果走同一条链路，
  // 避免「识别出来的话」和「打出来的话」在界面上表现不一致
  sendText(raw) {
    const text = (raw || '').trim();
    if (!text || this.data.sending) return;

    // 上一轮先落定（正常情况下 _endTurn 已经收过尾，这里是兜底）：
    // 剩余文字一次性铺完，卡片类型闸门复位
    this._snapStreamEnd();
    this._finishStreaming();
    this._cardTypes = {};
    // 引用来源按轮收集：sources 通常在正文之前到达（RAG 检索完先发引用）
    this._roundSources = [];

    this._roundTextStarted = false;
    // 追加用路径写法：整表回传会把历史消息（含已解析的 blocks）重传一遍，
    // 长会话下这是发送路径上最贵的一步
    const appendAt = this.data.messages.length;
    this.setData({
      ['messages[' + appendAt + ']']: userMsg(text),
      input: '',
      sending: true
    });
    // 加载块与用户消息同帧落位：先发一句「正在理解」，别让界面空着等
    this._startLoad();
    this.scrollToBottom();
    this._armSendWatchdog();

    // 通过 SSE 发送：每次提问开一条独立流；传入本页 session_id（后端据此拼 thread_id），
    // 并记下本轮 rid，回来的事件只渲染本页发起的这一轮。
    // 必须等登录：SSE 不走 utils/request 的 401 重登链路，冷启动后立刻提问
    // 会带着空 Bearer 出去，直接撞 401（旧表现：「服务响应异常」）
    this._sendSeq += 1;
    const seq = this._sendSeq;
    ensureLogin(false).then((token) => {
      // 登录回来时这一轮可能已经结束（看门狗超时 / 切了会话 / 用户又发了新问题），
      // 序号或发送态对不上就整轮丢弃，绝不把旧问题当成新问题重发
      if (this._unloaded || seq !== this._sendSeq || !this.data.sending) return;
      if (!token) {
        this._failSend('登录失败，请重试');
        return;
      }
      this._myReqId = agentSocket.sendMessage(text, this._sessionId);
    });
  },

  // 发送看门狗：sending 只在 done / error 复位；服务没起、连接没建立，或正文出到
  // 一半静默挂掉时这两个事件永远不来，sending 会一直为 true、后续发送被静默吞掉
  //（表现为「点了发送没反应」）
  _armSendWatchdog() {
    this._touchSendWatchdog();
  },

  // 按「多久没有新内容」计时：每收到一个 token 都重置一次（见 _appendToken），
  // 首包慢与中途静默卡死都能兜住
  _touchSendWatchdog() {
    if (this._sendWatchdog) clearTimeout(this._sendWatchdog);
    this._sendWatchdog = setTimeout(() => {
      this._sendWatchdog = 0;
      if (this._unloaded || !this.data.sending) return;
      this._failSend('服务暂时没有响应，请重试');
    }, SEND_TIMEOUT_MS);
  },

  _clearSendWatchdog() {
    if (this._sendWatchdog) {
      clearTimeout(this._sendWatchdog);
      this._sendWatchdog = 0;
    }
  },

  // 一轮失败/超时的统一收尾：剩余文字铺完、解锁发送、提示一次。
  // error / 看门狗 / 登录失败三条路径共用，避免某一处漏掉某一步（如 _streamClosing）
  _failSend(message) {
    this._clearSendWatchdog();
    this._snapStreamEnd();
    this._finishStreaming();
    this._endLoad();
    this.setData({ sending: false });
    if (message) this.toast(message);
  },

  // 键盘弹起后的布局：输入栏容器整体上移 kb，对话区同步收窄 kb，
  // 两者共用同一个键盘高度，保证卡片与对话区的间距始终是设计稿的 29rpx
  applyKeyboard() {
    const alreadyShifted = Math.max(0, this._baseH - this._curH);
    // 键盘弹起时窗口可能已被系统收缩（Android 会 resize，iOS 不 resize），
    // 减掉已收缩的部分，否则键盘高度会被重复计算
    const kb = Math.max(0, (this._kbHeight || 0) - alreadyShifted);
    // 输入栏的上抬量 = 键盘高度 + 固定间距：只抬键盘高度的话胶囊会贴着键盘顶边
    const lift = kb > 0 ? kb + (this._keyboardGap || 0) : 0;
    // 键盘弹起时安全区落在键盘之下，输入栏不再为它留白
    const senderPad = kb > 0 ? 0 : this.data.padBottom;
    const availH = this._curH - lift;
    // 兜底值取小值：兜得太高会让对话区反向溢出到输入栏上，把卡片盖住
    const chatHeight = Math.max(80, availH - this._navH - this._senderH - senderPad);
    if (
      chatHeight === this.data.chatHeight &&
      lift === this.data.keyboardOffset &&
      senderPad === this.data.senderPadBottom
    ) {
      return;
    }
    this.setData({ chatHeight, keyboardOffset: lift, senderPadBottom: senderPad });
  },

  onKeyboardHeightChange(e) {
    const height = (e && e.detail && e.detail.height) || 0;
    const kb = height > 0 ? height : 0;
    if (kb === this._kbHeight) return;
    this._kbHeight = kb;
    this.applyKeyboard();
    // 输入过程中卡片会随行数变高（textarea 开了 auto-height），键盘收/起稳定后再量一次
    this.scheduleMeasureSender();
  },

  /* ===== 语音输入（同声传译）=====
     点一下开始聆听（面板里原文 + 译文实时出字），再点一下把**原文**发给 agent；
     静音自动停录只把终稿停在面板上，仍要再点一次才发。 */

  // 语音键的唯一入口：未开始就开录；聆听中点停并发送；已停录待发则直接发
  onVoiceTap() {
    if (this._authorizing) return; // 授权回调还没回来，忽略连点
    if (this._recording) {
      this.stopVoiceAndSend();
      return;
    }
    if (this.data.voiceHolding) {
      this.sendHeldVoice();
      return;
    }
    if (this.data.sending) {
      this.toast('上一条还在回答，稍等一下');
      return;
    }
    this.beginVoice();
  },

  // 开录前确认麦克风权限：之前拒绝过的话 authorize 会直接失败，
  // 那时只能让用户去设置页打开，这里给个明确出口，别点了没反应
  beginVoice() {
    this._authorizing = true;
    wx.authorize({
      scope: 'scope.record',
      success: () => {
        this._authorizing = false;
        if (this._unloaded) return;
        this.startRecord();
      },
      fail: () => {
        this._authorizing = false;
        this.setData({ voiceTip: VOICE_TIP_IDLE });
        wx.showModal({
          title: '需要麦克风权限',
          content: '语音输入需要麦克风权限，是否去设置页开启？',
          confirmText: '去设置',
          success: (r) => {
            if (r.confirm) wx.openSetting();
          }
        });
      }
    });
  },

  // 再点一下：停止录音，终稿回来后由 onVoiceStop 直接发出。
  // _stopping 挡连点：等终稿的这段时间里再点会重复 stop，插件侧会抛错
  stopVoiceAndSend() {
    if (this._stopping) return;
    this._stopping = true;
    this.setData({ voiceTip: VOICE_TIP_STOP });
    stopRecognize();
  },

  // 取消：丢弃本次结果（含已停录待发的终稿、滚动中的译文与播报）
  onVoiceCancel() {
    if (this._recording) {
      // 先置 aborted 再 abort，否则插件迟到的终稿会把用户刚说的话发出去
      this._recordCanceled = true;
      this._recording = false;
      this._stopping = false;
      abortRecognize();
    }
    this._recordCanceled = false;
    this._heldVoiceText = '';
    // 置 null 后迟到的译文回调直接丢弃，不会写回面板
    this.resetVoiceTranslate();
    this.stopVoiceSpeech();
    this.setData({
      recording: false,
      voiceHolding: false,
      voiceTip: VOICE_TIP_IDLE,
      voiceDst: '',
      voiceTranslating: false,
      voiceDstFailed: false
    });
    this.clearVoiceText();
  },

  // 停录 / 到时长上限的统一收尾：手动停直接发，静音自动停只停在面板上
  onVoiceStop(res) {
    if (this._unloaded) return;
    const canceled = this._recordCanceled;
    // 用户主动点停 = 现在就要发；静音 / 到时长上限 = 只把终稿交出来
    const manual = !!this._stopping;
    const text = String((res && res.text) || '').trim();
    const ms = (res && res.duration) || Date.now() - (this._recordStartAt || 0);

    this._recording = false;
    this._stopping = false;
    this._recordCanceled = false;
    // 掐掉还没落的中间结果，否则它晚一步把终稿覆盖回「更短的旧文本」
    if (this._voiceTextTimer) {
      clearTimeout(this._voiceTextTimer);
      this._voiceTextTimer = 0;
    }

    // 终稿可能比最后一个中间结果多几个字：补翻成最后一段
    this.finalizeVoiceTranslate(text);

    if (canceled) {
      this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE });
      this.clearVoiceText();
      return;
    }

    if (!text) {
      // 空识别：静音自动停多半是没开口，不打扰；手动停才提示误触
      this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE, voiceDst: '', voiceTranslating: false });
      this.clearVoiceText();
      if (manual) this.toast(ms < REC_MIN_MS ? '说话时间太短' : '没听清，请再说一次', 1200);
      return;
    }

    if (!manual) {
      // 静音自动停：终稿留在面板上，再点一次才发（保留改口机会）
      this._heldVoiceText = text;
      this.setData({ recording: false, voiceHolding: true, voiceTip: VOICE_TIP_HOLD, voiceText: text });
      return;
    }

    this.setData({ recording: false, voiceTip: VOICE_TIP_IDLE });
    this.clearVoiceText();
    this.stopVoiceSpeech();
    this.sendVoiceText(text);
  },

  onVoiceError(message) {
    if (this._unloaded) return;
    const canceled = this._recordCanceled;

    this._recording = false;
    this._stopping = false;
    this._recordCanceled = false;
    this.resetVoiceTranslate();
    this.stopVoiceSpeech();
    this.setData({
      recording: false,
      voiceHolding: false,
      voiceTip: VOICE_TIP_IDLE,
      voiceDst: '',
      voiceTranslating: false,
      voiceDstFailed: false
    });
    this.clearVoiceText();
    if (canceled) return;
    this.toast(message || '没听清，请再说一次');
  },

  // 中间结果：翻译请求跟着识别立刻发（不节流），只有视图刷新合批到 150ms
  onVoicePartial(text) {
    if (this._unloaded) return;
    this._voicePartial = String(text || '');
    this.maybeTranslateVoice(this._voicePartial);
    if (this._voiceTextTimer) return;
    this._voiceTextTimer = setTimeout(() => {
      this._voiceTextTimer = 0;
      if (this._unloaded) return;
      this.setData({ voiceText: this._voicePartial, voiceDst: this.dstText() });
    }, VOICE_TEXT_FLUSH_MS);
  },

  clearVoiceText() {
    if (this._voiceTextTimer) {
      clearTimeout(this._voiceTextTimer);
      this._voiceTextTimer = 0;
    }
    this._voicePartial = '';
    if (this.data.voiceText) this.setData({ voiceText: '' });
  },

  startRecord() {
    this._recording = true;
    this._stopping = false;
    this._recordCanceled = false;
    this._recordStartAt = Date.now();
    this._voicePartial = '';
    this._heldVoiceText = '';
    this.beginVoiceTranslate();
    this.stopVoiceSpeech();

    this.setData({
      recording: true,
      voiceHolding: false,
      voiceTip: VOICE_TIP_LIVE,
      voiceText: '',
      voiceDst: '',
      voiceTranslating: false,
      voiceDstFailed: false
    });

    // 识别全程在插件侧完成：中间结果进预输入框，收尾后 onStop 带终稿回来。
    // silenceMs：说完 1.5s 自动停录；停录不等于发送，发不发仍由用户决定
    startRecognize(
      {
        onPartial: (text) => this.onVoicePartial(text),
        onFinal: (res) => this.onVoiceStop(res),
        onError: (message) => this.onVoiceError(message)
      },
      {
        duration: REC_MAX_MS,
        silenceMs: VOICE_SILENCE_MS,
        lang: this.voiceLang().asr
      }
    );
  },

  /* ===== 同传：稳定片段翻译 ===== */

  voiceLang() {
    return VOICE_LANGS[this.data.voiceDir] || VOICE_LANGS.zh2en;
  },

  // 新一轮录音：清空已翻译片段，_tr 存在才允许派发
  beginVoiceTranslate() {
    this._tr = { dispatched: 0, translating: false, final: false, pendingTail: '' };
    this._voiceDstParts = [];
  },

  // 作废本轮：迟到的译文回调看到 _tr 为 null 会直接丢弃
  resetVoiceTranslate() {
    this._tr = null;
    this._voiceDstParts = [];
  },

  // 稳定片段判定：不正在翻译 + 新增部分够长，两条都满足才动配额
  maybeTranslateVoice(cumulative) {
    const tr = this._tr;
    if (!tr || tr.final || tr.translating) return;
    const take = takeStableSegment(cumulative, tr.dispatched, {
      minLen: VOICE_SEG_MIN,
      maxLen: VOICE_SEG_MAX
    });
    if (!take) return;
    tr.dispatched += take.consumed;
    this.dispatchVoiceChunk(take.segment);
  },

  dispatchVoiceChunk(segment) {
    const text = String(segment || '').trim();
    if (!text) return;
    const tr = this._tr;
    if (!tr) return;
    const lang = this.voiceLang();
    tr.translating = true;
    this.setData({ voiceTranslating: true });

    // 回调带上本轮对象：上一轮迟到的译文不能写进新一轮（会把 translating 误清零）
    translator
      .translate(text, { from: lang.from, to: lang.to })
      .then((dst) => this.onVoiceChunkDone(tr, dst))
      .catch(() => this.onVoiceChunkFail(tr));
  },

  onVoiceChunkDone(tr, dst) {
    if (!tr || this._tr !== tr) return; // 已取消 / 已换一轮
    tr.translating = false;

    const out = String(dst || '').trim();
    if (out) {
      this._voiceDstParts.push(out);
      // 译文一到就预取音频：上一条还在播时，这一条的音频已经落盘
      this.enqueueVoiceSpeech(out);
    }
    if (this._unloaded) return;
    // 一旦有一条翻成功，之前的失败提示就该收掉
    this.setData({ voiceDst: this.dstText(), voiceTranslating: tr.translating, voiceDstFailed: false });

    // 终稿尾巴排在这条后面：现在补发
    this.flushVoiceTail();
    // 识别还在继续：把刚攒下的下一段立刻送出去
    this.maybeTranslateVoice(this._voicePartial || '');
  },

  onVoiceChunkFail(tr) {
    if (!tr || this._tr !== tr) return;
    tr.translating = false;
    if (this._unloaded) return;
    // 配额被限流时插件通常表现为超时 / 失败，这里给一句可解释的提示
    this.setData({ voiceDstFailed: true, voiceTranslating: false });
    this.flushVoiceTail();
    this.maybeTranslateVoice(this._voicePartial || '');
  },

  // 终稿补尾巴：终稿通常比最后一个中间结果多几个字，这几个字也得翻
  finalizeVoiceTranslate(text) {
    const tr = this._tr;
    if (!tr) return;
    tr.final = true;
    const full = String(text || '');
    if (!full) return;
    const rest = full.slice(Math.min(tr.dispatched, full.length)).trim();
    if (!rest) return;
    if (tr.translating) {
      tr.pendingTail = rest;
      return;
    }
    tr.dispatched = full.length;
    this.dispatchVoiceChunk(rest);
  },

  flushVoiceTail() {
    const tr = this._tr;
    if (!tr || !tr.final || !tr.pendingTail) return;
    const tail = tr.pendingTail;
    tr.pendingTail = '';
    tr.dispatched += tail.length;
    this.dispatchVoiceChunk(tail);
  },

  // 译文拼接：中文目标直接相连，英文目标之间补空格
  dstText() {
    const lang = this.voiceLang();
    return (this._voiceDstParts || []).join(lang.to === 'zh_CN' ? '' : ' ');
  },

  /* ===== 同传：播报 ===== */

  enqueueVoiceSpeech(text) {
    if (!this.data.voiceSpeak) return;
    const content = String(text || '').trim();
    if (!content) return;
    const lang = this.voiceLang();
    tts.prefetch(content, lang.tts);
    // 落后太多说明播放追不上识别：丢最旧的，别让耳朵追几分钟前的话
    if (this._speechQueue.length >= VOICE_SPEECH_MAX) this._speechQueue.shift();
    this._speechQueue.push({ text: content, lang: lang.tts });
    this.drainVoiceSpeech();
  },

  // 串行播：同一时刻只有一句在响，避免两句叠在一起听不清
  drainVoiceSpeech() {
    if (this._speaking || this._unloaded) return;
    const next = this._speechQueue.shift();
    if (!next) return;
    this._speaking = true;
    tts.speak(next.text, next.lang).then(() => {
      this._speaking = false;
      if (!this._unloaded) this.drainVoiceSpeech();
    });
  },

  stopVoiceSpeech() {
    if (this._speechQueue) this._speechQueue.length = 0;
    tts.stop();
  },

  onVoiceToggleSpeak() {
    const on = !this.data.voiceSpeak;
    this.setData({ voiceSpeak: on });
    // 关掉播报要立刻静音：队列里排着的也别再出声
    if (!on) this.stopVoiceSpeech();
  },

  // 方向只在没在录、也没有待发终稿时能切：切了以后识别语言和译文都对不上
  onVoiceSwapDir() {
    if (this._recording || this.data.voiceHolding) {
      this.toast('先发送或取消这一次');
      return;
    }
    const next = this.data.voiceDir === 'zh2en' ? 'en2zh' : 'zh2en';
    this.resetVoiceTranslate();
    this.stopVoiceSpeech();
    this.setData({
      voiceDir: next,
      voiceLangLabel: VOICE_LANGS[next].label,
      voiceDst: '',
      voiceTranslating: false,
      voiceDstFailed: false
    });
  },

  /* ===== 同传：发送（发原文，译文只做展示与播报）===== */

  sendVoiceText(text) {
    // 上一轮还没答完：不吞掉这句话，落到输入框并切回键盘，让用户自己决定何时发
    if (this.data.sending) {
      this.setData({ voiceMode: false, input: text, inputFocus: true });
      this.scheduleMeasureSender();
      this.toast('上一条还在回答，已填到输入框');
      return;
    }
    this.sendText(text);
  },

  // 静音停录后的终稿：再点一次才真正发出
  sendHeldVoice() {
    const text = String(this._heldVoiceText || '').trim();
    this._heldVoiceText = '';
    this.setData({ voiceHolding: false, voiceTip: VOICE_TIP_IDLE });
    this.clearVoiceText();
    this.stopVoiceSpeech();
    if (!text) return;
    this.sendVoiceText(text);
  },

  // 滚到底：用数值 scroll-top 定位，不再走 scroll-into-view
  // （旧写法每帧「清空锚点 → 重设锚点」会不停重启滚动动画，画面上下抽动）。
  // 只在显式场景调用：发送新消息、切换/恢复会话、新建会话；
  // AI 流式正文与卡片落位都不调它——回复期间视口保持不动。
  scrollToBottom() {
    // 空会话没有「底部」可看：首屏只有引导问题，滚到底只会把顶部内容顶出屏幕
    if (!this.data.messages.length) return;
    if (this._scrollTimer) return;
    this._scrollTimer = setTimeout(() => {
      this._scrollTimer = 0;
      const query = wx.createSelectorQuery().in(this);
      query.select('.chat-inner').boundingClientRect();
      query.exec((res) => {
        const rect = res && res[0];
        if (!rect || !rect.height) return;
        // 内容总高直接交给 scroll-top，视图层会自动夹到「内容高度 - 容器高度」。
        // 不做「高度没变就跳过」的去重：切换到更短的会话后旧值更大，
        // 跳过会让下一次发消息滚不到底
        this.setData({ scrollTop: Math.ceil(rect.height), scrollAnimated: true });
      });
    }, SCROLL_FOLLOW_MS);
  },

  // ===== 侧边抽屉 =====
  onMenuTap() {
    this._pendingMenu = '';
    this.setData({ drawerOpen: true });
    this.loadSessions(); // 每次打开抽屉刷新历史列表
  },

  // 点行情卡 -> 个股详情。目前只有开发版能进（卡片画廊是假数据页），
  // 体验版/正式版给一句提示而不是跳假数据：真实详情页做完后把 isDev 判断去掉即可。
  // 卡片内没有可点的子元素，bindtap 绑在组件根上即可整卡响应；
  // 没有 code（理论上不会出现）时不跳
  onQuoteCardTap(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    if (!ds.code) return;
    if (!isDev) {
      this.toast('个股详情正在开发中');
      return;
    }
    wx.navigateTo({
      url: '/pages/test/index?code=' + encodeURIComponent(ds.code) +
        (ds.name ? '&name=' + encodeURIComponent(ds.name) : '')
    });
  },

  // 点抽屉外的遮罩关闭（base-popup 的 mask 事件）——这是抽屉唯一的关闭手势。
  // 滑动只负责开：左滑不在手势里处理，避免「想滚动却把抽屉划没了」
  onDrawerMask() {
    if (this.data.drawerOpen) this.setData({ drawerOpen: false });
  },

  // 抽屉只在主页面存在，子页面没有，因此不保留「当前页」状态，
  // 每一项都是纯粹的跳转入口。
  // 这里只负责「开始收起」并记下要去哪，真正的跳转放到收起动画结束之后：
  // popup 的 transition 是靠定时器收尾的，若同一帧就 navigateTo，
  // 新页面会直接盖上来把收起动画掐断，真机上看着就是「抽屉没收起」
  onMenuSelect(e) {
    this._pendingMenu = e.currentTarget.dataset.menu || '';
    if (!this.data.drawerOpen) {
      // 理论上不会走到这里（菜单只在抽屉展开时可点），兜底直接跳
      this.flushPendingNav();
      return;
    }
    this.setData({ drawerOpen: false });
  },

  // 新对话：先收抽屉、动画结束再跳转，中间多一步「先注册、成功才压栈新对话页」。
  // 三态都反馈到按钮（wxml 按 newChatting 切换）：创建中挡连点、抽屉照旧立即收起；
  // 成功等 onDrawerLeaved 再压栈；失败（网关 502 / 网络异常 / 登录失效）toast 并留在原会话，
  // 宁可让用户再点一次，也不要跳进一条没注册的会话。
  // 兜底：注册失败不影响对话本身，首条消息发出时 Go 网关仍会 registerSession 补注册。
  onNewChat() {
    if (this.data.newChatting) return; // 连点闸门：一次点击 = 一条会话
    const sid = agentSocket.freshSessionId();
    this.setData({ newChatting: true });
    this._pendingMenu = 'new';
    this._pendingSession = sid;
    this._newChatReady = false;
    // 抽屉开着时先记「动画未完成」，等 onDrawerLeaved 才允许跳转
    this._drawerClosed = !this.data.drawerOpen;

    createSession(sid, '').then(() => {
      // 抽屉可能正在收动画、也可能压根没开过，两种情况都交给 _flushNewChat 判定
      this._newChatReady = true;
      this._flushNewChat();
    }).catch(() => {
      this._pendingMenu = '';
      this._pendingSession = '';
      this._newChatReady = false;
      this.setData({ newChatting: false });
      this.toast('新建对话失败，请重试');
    });

    if (this._drawerClosed) {
      // 抽屉本来就是关的：没有动画要等，注册回来后由 then 里的 _flushNewChat 跳
      this._flushNewChat();
      return;
    }
    this.setData({ drawerOpen: false });
  },

  // 新对话的跳转闸门：「注册成功」与「抽屉收起动画结束」两个条件都满足才压栈。
  // 只满足其一时不跳——否则要么跳进一条没注册的会话，要么掐断抽屉的收起动画
  _flushNewChat() {
    if (this._pendingMenu !== 'new' || !this._newChatReady || !this._drawerClosed) return;
    this._pendingMenu = '';
    this._newChatReady = false;
    // 压栈新对话页：旧对话原样留在栈里（可返回继续），新页是独立实例、
    // 独立会话（?session= 带上刚注册的 id，新页 onLoad 据此沿用），
    // 从它进子页返回照样回到它自己。
    // 页面栈上限 10 层，满时 navigateTo 会失败，退化为 reLaunch 重置为一个新对话页
    const sid = this._pendingSession || '';
    this._pendingSession = '';
    this.setData({ newChatting: false });
    const url = NEW_CHAT_URL + (sid ? '&session=' + encodeURIComponent(sid) : '');
    wx.navigateTo({
      url,
      fail: () => wx.reLaunch({ url })
    });
  },

  // popup 收起动画结束（transition mixin 的 leaved 事件）：此刻再跳转，动画已完整播完。
  // 新对话的跳转还要等注册回来，两个条件在 _flushNewChat 里汇合
  onDrawerLeaved() {
    this._drawerClosed = true;
    this.flushPendingNav();
  },

  flushPendingNav() {
    const menu = this._pendingMenu;
    if (menu === 'new') {
      // 新对话不在这里直接跳：注册没回来就压栈，会跳进一条没注册的会话。
      // 交给 _flushNewChat 判定，注册回来后自行补跳
      this._flushNewChat();
      return;
    }
    this._pendingMenu = '';
    const url = menu && ROUTES[menu];
    if (url) wx.navigateTo({ url });
  },

  // ===== 历史对话 =====
  // 拉会话列表并标记当前会话（抽屉里的选中效果）。
  // 失败静默：列表为空只是没历史，不该弹 toast 打断用户
  loadSessions() {
    return listSessions().then((data) => {
      const list = (data && data.sessions) || [];
      const sessions = list.map((s) => Object.assign({}, s, {
        active: s.session_id === this._sessionId
      }));
      this.setData({ sessions });
    }).catch(() => {});
  },

  // 历史列表点选：原地切到该会话并加载它的纯文本消息。
  // 抽屉保持打开——用户还停在抽屉上继续挑，不因为点了一下就被踢回对话区
  onSessionSelect(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    const sid = ds.sid;
    if (!sid) return;
    // 点的就是当前会话：什么都不做，抽屉照旧开着
    if (sid === this._sessionId) return;

    this._abortInFlight();
    this._sessionId = sid;
    this.loadSessions();
    this.loadSessionMessages(sid);
  },

  // 切会话 / 重建会话前掐断在途流：旧会话还在回来的 token、卡片，以及 abort 触发的
  // error 事件，都不该落进新会话的界面（否则上下文就串了）。
  // _myReqId 先归零再断——abort 的 error 打的正是「被掐断那一轮」的戳，
  // 归零后它会被本页的 rid 闸门过滤掉，不会误弹「连接异常」。
  // 只在本页确实有流在跑时才断：单例可能被栈里其它对话页实例共用
  _abortInFlight() {
    if (!this.data.sending) return;
    this._myReqId = 0;
    agentSocket.disconnect();
    // _failSend 负责铺完剩余文字 / 复位 _streamClosing / 解锁 sending；用户主动切走
    // 不需要再弹提示，故不传文案
    this._failSend('');
  },

  // 当前会话被删后原地换一条新会话：不跳页、不收起抽屉，
  // 用户仍停在这一页的抽屉上，只是当前项换成了新的空会话。
  // 这里只生成 id、不注册：注册会立刻在历史列表里多出一条「新对话」顶在原位，
  // 看起来就是「删了没删掉」；首条消息发出时 Go 网关会 registerSession 补注册。
  // deletedSid 用于在刷新回来的列表里再兜一次底：删除刚落库，
  // 列表接口可能还带着那条，不滤掉它就会「删了又回来」
  _resetToNewSession(deletedSid) {
    const sid = agentSocket.freshSessionId();
    this._abortInFlight();
    this._sessionId = sid;
    // 换会话等于换一份历史：分片池与加载序号一起作废，
    // 否则上一会话的旧响应还能把消息写回来
    this._historyPool = [];
    this._msgSeq += 1;
    this.setData({ messages: [], hasEarlier: false, earlierCount: 0 });
    this.scrollToBottom();
    this.loadSessions().then(() => {
      if (!deletedSid || this._unloaded) return;
      this.setData({
        sessions: (this.data.sessions || []).filter((s) => s.session_id !== deletedSid)
      });
    });
  },

  // 恢复某会话的消息（文本气泡；结构化卡片不持久化，恢复后只有文本）。
  // 长会话只渲染最近 MSG_INITIAL 条，更早的留在 _historyPool 里，点「查看更早」再补：
  // 一次性解析 + 渲染几百条会把首屏拖住（每条都要跑一遍 markdown）。
  // _msgSeq 是加载闸门：连点两条会话时，先发后到的响应不许覆盖后选的那条。
  // 失败时清空消息 + 轻提示——宁可显示空会话，也别把上一条对话的
  // 消息留在界面上冒充新会话的内容
  loadSessionMessages(sid) {
    this._msgSeq += 1;
    const seq = this._msgSeq;
    return sessionMessages(sid).then((data) => {
      if (seq !== this._msgSeq || this._unloaded) return;
      const list = (data && data.messages) || [];
      const msgs = list.map((m) =>
        (m.role === 'user' ? userMsg(m.content) : assistantMsg(m.content))
      );
      const cut = Math.max(0, msgs.length - MSG_INITIAL);
      this._historyPool = msgs.slice(0, cut);
      this.setData({
        messages: msgs.slice(cut),
        hasEarlier: cut > 0,
        earlierCount: cut
      });
      this.scrollToBottom();
    }).catch(() => {
      if (seq !== this._msgSeq || this._unloaded) return;
      this._historyPool = [];
      this.setData({ messages: [], hasEarlier: false, earlierCount: 0 });
      this.toast('会话加载失败');
    });
  },

  // 向前补一页历史。只允许在「没有在途回答」时点：插入后所有下标整体后移，
  // 正在流式刷新的那条消息会指错气泡
  onLoadEarlier() {
    if (!this._historyPool.length || this.data.sending) return;
    const take = Math.min(MSG_PAGE, this._historyPool.length);
    const chunk = this._historyPool.slice(-take);
    this._historyPool = this._historyPool.slice(0, -take);
    this.setData({
      messages: chunk.concat(this.data.messages),
      hasEarlier: this._historyPool.length > 0,
      earlierCount: this._historyPool.length
    });
  },

  // 删除会话：先弹胶囊确认框（与自选股 / 提醒页同一套 base-popup，
  // 不再用原生 wx.showModal——两端样式不一致，也不跟主题走）
  onSessionDelete(e) {
    const ds = (e && e.currentTarget && e.currentTarget.dataset) || {};
    if (!ds.sid) return;
    this._deletingSid = ds.sid;
    this.setData({ deleteVisible: true });
  },

  cancelDeleteSession() {
    if (!this.data.deleteVisible) return;
    this._deletingSid = '';
    this.setData({ deleteVisible: false });
  },

  // 确认删除：连 checkpoint 与摘要一起清，不可恢复。
  // 删的是当前会话时原地换一条新会话（不跳页）——当前页继续留着只会对着
  // 一堆已不属于任何 thread 的消息发问
  confirmDeleteSession() {
    const sid = this._deletingSid;
    this._deletingSid = '';
    this.setData({ deleteVisible: false });
    if (!sid) return;

    // 先在本地摘掉再打后端：列表接口往往比删除先返回，
    // 那条会话会「删了又冒出来」，看起来就像删不掉
    this.setData({
      sessions: (this.data.sessions || []).filter((s) => s.session_id !== sid)
    });

    const done = () => {
      if (sid === this._sessionId) {
        this._resetToNewSession(sid);
        return;
      }
      this.loadSessions();
    };

    deleteSession(sid).then(done).catch((err) => {
      // 404 = 服务端本来就没有这条（从未注册，或已被删掉过）。
      // 这种情况按「已删除」处理，否则用户永远删不掉一条本地残留
      if (err && Number(err.code) === 404) {
        done();
        return;
      }
      this.toast('删除失败');
      this.loadSessions(); // 回滚：把列表拉回服务端真实状态
    });
  },

  // 轻提示：自研 base-toast（替代 wx.showToast 默认样式）
  toast(message, duration) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message, duration);
  },

  // markdown 链接点击：小程序不能直接打开外部 web 页面，
  // 统一复制链接 + 轻提示（研报 / 事件卡的原文链接合规要求同理）
  onMdLinkTap(e) {
    const href = e.currentTarget.dataset && e.currentTarget.dataset.href;
    if (!href) return;
    wx.setClipboardData({
      data: href,
      success: () => this.toast('链接已复制')
    });
  },

  // ===== 手势开关抽屉：右滑打开；关闭只走点遮罩 =====
  // 输入栏内的手势起点标记（wxml 用 bindtouchstart 绑到 .sender-wrap）。
  // 用「标记 + 判断」取代原来的 catch：catch 会让整个输入栏的子元素点击失效，
  // 输入框点不动、模式键和发送键全都不响应。这里只记一个信号，
  // 由 onSwipeStart 决定这次手势是否让给输入（光标移动 / 按住说话）
  onSenderTouchStart() {
    this._inSender = true;
  },

  // 起手在输入栏、但这一下只是点按（没有滑动）时，标记必须跟着 touchend 清掉：
  // 只由 onSwipeStart 消费的话，输入栏上的一次点按会把「下一次」滑动锁掉，
  // 表现就是打完字/点完输入框之后，第一下右滑怎么划都开不了抽屉
  onSenderTouchEnd() {
    this._inSender = false;
  },

  // 点输入区任意位置都聚焦 textarea：原生组件的点击热区未必铺满整块，
  // 给一个明确的聚焦入口，点空白也能唤起键盘
  onTapInputField() {
    if (!this.data.inputFocus) this.setData({ inputFocus: true });
  },

  onSwipeStart(e) {
    const t = (e.touches && e.touches[0]) || null;
    if (!t) {
      this._startX = null;
      this._startY = null;
      return;
    }
    this._startX = t.clientX;
    this._startY = t.clientY;
    this._lastX = this._startX;
    this._lastY = this._startY;
    // 子元素的 bindtouchstart 先于根节点执行，这里读到的是本次手势的起点归属；
    // 读完即清，下一次手势重新判定
    const inSender = this._inSender;
    this._inSender = false;
    // 起点在输入栏（或正在录音）：这一整段手势都不用于开关抽屉。
    // 录音这条是兜底：按住说话时若滑出按钮范围，事件仍可能落到根节点
    this._swipeLocked = !!this._recording || !!inSender;
  },

  onSwipeMove(e) {
    if (this._recording || this._startX == null) return;
    const t = (e.touches && e.touches[0]) || null;
    if (!t) return;
    this._lastX = t.clientX;
    this._lastY = t.clientY;
    if (this._swipeLocked) return;
    if (this._applySwipe(this._lastX - this._startX, this._lastY - this._startY)) {
      this._swipeLocked = true;
    }
  },

  // touchend 兜底：scroll-view 与原生 textarea 会吞掉一部分 touchmove，
  // 真机上偶尔整段手势只剩 touchend。用起止点补算一次，手势才不至于整段丢失。
  // 点击与纵向滚动的位移过不了阈值，不会误触
  onSwipeEnd(e) {
    if (!this._swipeLocked && !this._recording && this._startX != null) {
      const t = (e && e.changedTouches && e.changedTouches[0]) || null;
      if (t) this._applySwipe(t.clientX - this._startX, t.clientY - this._startY);
    }
    this._swipeLocked = false;
    this._startX = null;
    this._startY = null;
  },

  // 位移判定只有这一处：横向压过纵向、且过 SWIPE_MIN 才算一次滑动。
  // 手势只负责「开」；关一律走点遮罩（onDrawerMask），避免滑错方向误关。
  // 返回 true 表示这次手势已经用掉（由调用方决定是否锁住后续帧）
  _applySwipe(dx, dy) {
    if (Math.abs(dx) < SWIPE_MIN || Math.abs(dx) <= Math.abs(dy)) return false;
    if (dx > 0 && !this.data.drawerOpen) {
      this.setData({ drawerOpen: true });
      this.loadSessions(); // 滑动打开抽屉同样刷新历史列表
      return true;
    }
    return false;
  }
});
