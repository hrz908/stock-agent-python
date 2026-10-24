import * as api from '../../services/watchlist';
import * as stockApi from '../../services/stock';
import { isDev } from '../../config';

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'up', label: '涨幅榜' },
  { key: 'down', label: '跌幅榜' }
];

// 只输入代码即可：交易所由代码前缀反推，不需要再单独选择所属市场
// label 只用作弹窗里的提示文案，prefix 参与展示代码拼接
const EXCHANGES = [
  { label: '沪市', prefix: 'SH', codes: ['600', '601', '603', '605', '688', '689'] },
  { label: '深市', prefix: 'SZ', codes: ['000', '001', '002', '003', '300', '301', '302'] },
  { label: '北交所', prefix: 'BJ', codes: ['43', '83', '87', '88', '920'] }
];

// 下拉最多展示几条联想结果：5 条刚好占满手机一屏，再多就要滚动了
const SEARCH_LIMIT = 5;

function marketOf(code) {
  const hit = EXCHANGES.find((e) => e.codes.some((p) => code.indexOf(p) === 0));
  return hit ? hit.prefix : '';
}

// 后端返回的列表项 → 页面渲染字段。
// 价格与涨跌幅直接用后端格式化好的 *_text（千分位 / 正负号），
// 前端不再自行拼装，避免两套格式对不上
function toViewItem(item) {
  return {
    id: item.id,
    stockCode: item.stock_code,
    name: item.name,
    code: item.code,
    price: item.price_text,
    pct: item.pct_text,
    // 排序专用数值：pct 是带正负号和 % 的展示串，不能直接比大小
    pctValue: Number(item.change_percent) || 0,
    up: !!item.up,
    cost: item.cost_price,
    qty: item.quantity,
    profit: item.profit_amount,
    profitRate: item.profit_rate,
    pending: !!item.pending,
    stale: !!item.stale
  };
}

// 盈亏为正显示红色、为负显示绿色（A 股习惯：红涨绿跌）；
// 为 0 时用中性色：空仓用户的「¥ 0.00」标成红色会看着像在亏
function profitClass(totalProfit) {
  const n = Number(totalProfit);
  if (!isFinite(n) || n === 0) return 'is-flat';
  return n < 0 ? 'is-down' : 'is-up';
}

function nowHM() {
  const d = new Date();
  const pad = (n) => (n < 10 ? `0${n}` : `${n}`);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ===== 左滑删除（自绘，替代 t-swipe-cell） =====
// 操作区宽度（px）：与 .stock-row-action 的 width 保持一致。
// 触摸坐标是 px，这里必须用 px 而不是 rpx
const SWIPE_ACTION_W = 64;
// 松手时位移超过一半操作区宽度即视为「打开」
const SWIPE_OPEN_RATIO = 0.5;

Page({
  data: {
    bottomGap: 20,
    filter: 'all',
    filterIndex: 0,
    filters: FILTERS,
    stocks: [],
    filtered: [],
    upCount: 0,
    downCount: 0,
    // 总浮动盈亏：后端算好并格式化，前端只负责渲染
    totalProfitText: '0.00',
    totalRateText: '0.00%',
    profitClass: 'is-up',
    syncedAt: '--:--',
    loading: false,
    refreshing: false,
    errorMsg: '',
    // 列表是否可滚动：默认关，测量后只有内容真超出容器才打开
    scrollable: false,

    // 左滑删除状态：swipeOpen = 处于打开态的行号（-1 无）；
    // swipeIndex / swipeDx = 正在拖动的行与实时位移；swipeDragging = 拖动中（关过渡）
    swipeOpen: -1,
    swipeIndex: -1,
    swipeDx: 0,
    swipeDragging: false,

    // 删除确认弹窗（base-popup center，危险色确认）
    confirmVisible: false,
    pendingIndex: -1,
    pendingName: '',

    // 添加自选表单：股票代码（支持模糊搜索）+ 成本价（元）+ 持股数量（股）
    addVisible: false,
    addCode: '',
    addCost: '',
    addQty: '',
    codeError: '',
    // 下拉联想：searchResults 是后端给的候选，searchVisible 控制显隐，
    // addMarket 记下选中项的交易所——点确定时直接用它，
    // 不必再用代码前缀反推市场（后端返回的才是权威值）
    searchResults: [],
    searchVisible: false,
    addMarket: '',
    // 弹窗里的交易所提示：一行一个交易所，文案直接来自 EXCHANGES
    exchanges: EXCHANGES.map((e) => ({ label: e.label, codes: e.codes.join(' / ') })),
  },

  onLoad() {
    let safeBottom = 0;
    try {
      const win = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      safeBottom =
        (win.safeAreaInsets && win.safeAreaInsets.bottom) ||
        (win.screenHeight && win.safeArea ? win.screenHeight - win.safeArea.bottom : 0) ||
        0;
    } catch (e) {
      // 保持默认值
    }
    // 底部按钮距屏幕底部的距离
    const bottomGap = 20 + safeBottom;
    this.setData({ bottomGap });
  },

  // 每次进入页面（含从子页返回）都先刷新一次行情。
  // 静默执行、不弹 toast，避免首次进入与返回都打断用户
  onShow() {
    this.refreshOnEnter();
  },

  refreshOnEnter() {
    if (this.data.refreshing) return;
    this.setData({ refreshing: true, errorMsg: '' });
    api
      .refresh()
      .then((data) => {
        this.setData({ refreshing: false });
        this.applyData(data);
      })
      .catch((err) => {
        this.setData({ refreshing: false, errorMsg: (err && err.message) || '刷新失败' });
      });
  },

  /* ================= 筛选（段式单选 radio） ================= */
  // 段式单选点击：data-key 即选项 key，效果与原 tabs change 完全一致
  onFilterTap(e) {
    const key = e.currentTarget.dataset.key;
    if (!key || key === this.data.filter) return;
    // 滑块位置：按 key 在 filters 中的下标，驱动 thumb 滑动
    const idx = this.data.filters.findIndex((f) => f.key === key);
    this.setData({ filter: key, filterIndex: idx, filtered: this.filterList(key) }, () => this.updateScrollable());
  },

  // tabs 的 change 事件（旧实现保留，供兼容）
  onTabsChange(e) {
    const key = e.detail && e.detail.value;
    if (!key || key === this.data.filter) return;
    this.setData({ filter: key, filtered: this.filterList(key) }, () => this.updateScrollable());
  },

  // 筛选 + 排序。
  // 列表接口固定按 filter=all 拉全量（这样切 tab 不用重复请求），
  // 所以涨跌榜的「按幅度大小排」统一在这里做，
  // 刷新、切 tab、增删之后的顺序都由这一处保证
  filterList(key, stocks) {
    const list = (stocks || this.data.stocks).filter((s) => {
      // 尚无行情的自选只在「全部」出现，不参与涨跌榜
      if (s.pending) return key === 'all';
      if (key === 'up') return s.up;
      if (key === 'down') return !s.up;
      return true;
    });

    // 涨幅榜：涨得越多越靠前 → 降序
    // 跌幅榜：跌得越多越靠前 → 涨跌幅数值越小越靠前 → 升序
    // （e.g. -5.23% 排在 -1.02% 前面）
    if (key === 'up' || key === 'down') {
      const asc = key === 'down';
      list.sort((a, b) => (asc ? a.pctValue - b.pctValue : b.pctValue - a.pctValue));
    }
    // sep：行间分隔线类名由 JS 按行号计算（WXSS 禁兄弟选择器，见 watchlist.wxss）
    return list.map((s, i) => Object.assign({}, s, { sep: i > 0 }));
  },

  /* ================= 滚动控制 ================= */
  // 只有内容真的超出容器（会被底部按钮遮挡）时才允许滚动。
  //
  // 不能只靠 CSS：scroll-view 本身就是原生滚动容器，只要内容比容器高就能拖；
  // iOS 上还有回弹，内容不足时手指一划也会有位移。
  // 所以这里量出真实内容高度，再决定 scroll-y 是开是关。
  //
  // 必须在渲染完成后测：调用方要放在 setData 的回调里，
  // 否则量到的是上一帧的尺寸
  updateScrollable() {
    wx.createSelectorQuery()
      .in(this)
      .select('.stock-scroll')
      .boundingClientRect()
      .select('.stock-inner')
      .boundingClientRect()
      .exec((res) => {
        const box = res && res[0];
        const inner = res && res[1];
        if (!box) return;

        // 空态时 .stock-inner 里只有 t-empty，高度很小，自然不会开滚动
        const contentH = inner ? inner.height : 0;
        // 留 1px 容差：避免浮点误差让两者刚好相等时来回抖动
        const scrollable = contentH > box.height + 1;
        if (scrollable !== this.data.scrollable) this.setData({ scrollable });
      });
  },

  /* ================= 左滑删除 ================= */
  // swipe-cell 右侧按钮点击：detail 含 action 信息，行号从 data-index 取
  onDelete(e) {
    const index = Number(e.currentTarget.dataset.index);
    const item = this.data.filtered[index];
    if (!item) return;
    this.setData({
      confirmVisible: true,
      pendingIndex: index,
      pendingName: item.name
    });
  },

  // 关闭时保留 pendingName：弹窗 desc 由它拼出，关闭瞬间清空会让
  // 面板高度在淡出动画中变矮，出现「缩小残影」；下次打开时会重新赋值
  cancelDelete() {
    this.setData({ confirmVisible: false, pendingIndex: -1 });
  },

  confirmDelete() {
    const index = this.data.pendingIndex;
    if (index < 0) return;

    const item = this.data.filtered[index];
    if (!item) return;

    this.setData({ confirmVisible: false, pendingIndex: -1, loading: true });
    api
      .remove(item.stockCode)
      .then(() => this.loadList())
      .then(() => this.toast('已移出自选'))
      .catch((err) => {
        this.setData({ loading: false, errorMsg: (err && err.message) || '删除失败' });
      });
  },

  /* ================= 左滑删除（自绘，替代 t-swipe-cell） ================= */

  // 起点：记录行号、起始坐标、该行当前是否已打开。
  // locked 三态：null 未判定 / true 横向滑动（本行接管）/ false 纵向滚动（交还列表）
  // 用 bind 而不是 catch：catch 会让行内所有子元素点击失效
  onRowTouchStart(e) {
    const index = Number(e.currentTarget.dataset.index);
    const touch = e.touches[0];
    const baseOpen = this.data.swipeOpen === index;
    const baseTx = baseOpen ? -SWIPE_ACTION_W : 0;
    this._swipe = {
      index: index,
      startX: touch.clientX,
      startY: touch.clientY,
      baseOpen: baseOpen,
      locked: null,
      tx: baseTx,
      lastTx: baseTx
    };
    this.setData({ swipeDragging: true });
  },

  onRowTouchMove(e) {
    const s = this._swipe;
    if (!s) return;
    const touch = e.touches[0];
    const dx = touch.clientX - s.startX;
    const dy = touch.clientY - s.startY;
    if (s.locked === null) {
      // 超过判定阈值才定向：避免把点击抖动当成滑动
      if (Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
      s.locked = Math.abs(dx) > Math.abs(dy);
      if (!s.locked) {
        // 纵向滚动：交还手势给 scroll-view，本行状态复位
        this._swipe = null;
        this.setData({ swipeDragging: false, swipeIndex: -1, swipeDx: 0 });
        return;
      }
    }
    // 只允许左滑（负位移）；已打开的行可以右滑关闭
    let tx = s.baseOpen ? -SWIPE_ACTION_W + dx : Math.min(0, dx);
    tx = Math.max(-SWIPE_ACTION_W, Math.min(0, tx));
    s.tx = tx;
    // 1px 内的抖动不回写视图，省掉无意义 setData
    if (Math.abs(tx - s.lastTx) < 1) return;
    s.lastTx = tx;
    this.setData({ swipeIndex: s.index, swipeDx: tx });
  },

  onRowTouchEnd() {
    const s = this._swipe;
    this._swipe = null;
    if (!s) return;
    // 松手：恢复过渡动画，按最终位移决定打开还是关闭
    this.setData({ swipeDragging: false });
    const open = s.tx <= -SWIPE_ACTION_W * SWIPE_OPEN_RATIO;
    this.setData({ swipeIndex: -1, swipeDx: 0, swipeOpen: open ? s.index : -1 });
  },

  // 点主行：先把打开状态的行收回去；没收起的动作（纯点击）则进个股详情。
  // 详情页目前只有开发版可进（卡片画廊是假数据），正式版给提示，
  // 免得把「卡片样式测试」和茅台假数据当成真实行情交付出去。
  // 横向滑动判定在 onRowTouchMove 里做，滑动锁定时触摸已交还列表、tap 不触发，
  // 因此滑完抬手不会误跳
  onRowMainTap(e) {
    if (this.data.swipeOpen >= 0) {
      this.setData({ swipeOpen: -1 });
      return;
    }
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

  // 轻提示：自研 base-toast（替代 TDesign toast）
  toast(message) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message);
  },

  /* ================= 数据同步 ================= */
  // 手动刷新：后端会并发拉行情 → 写缓存 → 检查提醒并推送，
  // 返回的全量列表（filter=all）直接替换本地数据
  onRefresh() {
    if (this.data.refreshing) return;
    this.setData({ refreshing: true, errorMsg: '' });

    api
      .refresh()
      .then((data) => {
        this.setData({ refreshing: false });
        this.applyData(data);
        this.toast('行情已刷新');
      })
      .catch((err) => {
        this.setData({ refreshing: false, errorMsg: (err && err.message) || '刷新失败' });
      });
  },

  // 统一入口：列表与刷新接口返回结构一致，都走这里更新列表与统计
  applyData(data) {
    const d = data || {};
    const stocks = (d.list || []).map(toViewItem);

    this.setData({
      stocks,
      filtered: this.filterList(this.data.filter, stocks),
      upCount: d.up_count || 0,
      downCount: d.down_count || 0,
      totalProfitText: d.total_profit || '0.00',
      totalRateText: `${d.total_profit_rate || '0.00'}%`,
      profitClass: profitClass(d.total_profit),
      syncedAt: nowHM()
    }, () => this.updateScrollable());
  },

  loadList() {
    this.setData({ loading: true, errorMsg: '' });
    return api
      .fetchList()
      .then((data) => {
        this.setData({ loading: false });
        this.applyData(data);
      })
      .catch((err) => {
        this.setData({ loading: false, errorMsg: (err && err.message) || '加载失败' });
      });
  },

  /* ================= 添加自选（dialog + input） ================= */
  onAdd() {
    this.setData({
      addVisible: true,
      addCode: '',
      addCost: '',
      addQty: '',
      codeError: '',
      searchResults: [],
      searchVisible: false,
      addMarket: ''
    });
  },

  onAddCancel() {
    this.setData({ addVisible: false, searchResults: [], searchVisible: false });
  },

  onAddCode(e) {
    const value = (e.detail && e.detail.value) || '';
    // 输入时清掉旧的报错，避免边改边红
    this.setData({ addCode: value, codeError: '' });
    this.scheduleSearch(value);
  },

  // 防抖 300ms：打字过程中不发请求，停手后再发。
  // 不防抖的话每敲一个字符就打一次后端，既慢又容易被第三方限流
  scheduleSearch(value) {
    if (this._searchTimer) clearTimeout(this._searchTimer);

    const kw = String(value || '').trim();
    if (!kw) {
      this.setData({ searchResults: [], searchVisible: false });
      return;
    }

    // 切走页面时清掉待发请求，避免在已销毁的页面上 setData
    this._searchTimer = setTimeout(() => this.doSearch(kw), 300);
  },

  // 拉联想候选。失败就静默收起下拉：搜索是体验加分项，
  // 挂了也不影响手输代码添加自选
  doSearch(kw) {
    stockApi
      .search(kw, SEARCH_LIMIT)
      .then((list) => {
        const results = list || [];
        this.setData({ searchResults: results, searchVisible: results.length > 0 });
      })
      .catch(() => {
        this.setData({ searchResults: [], searchVisible: false });
      });
  },

  // 选中下拉项：填回代码，并记住它的交易所。
  // 之后就算用户再手动改动代码，也会走 onAddConfirm 里的兜底反推
  onPickSearch(e) {
    const { code, market } = e.currentTarget.dataset;
    this.setData({
      addCode: code || '',
      addMarket: market || '',
      searchResults: [],
      searchVisible: false,
      codeError: ''
    });
  },

  onAddCost(e) {
    const value = (e.detail && e.detail.value) || '';
    this.setData({ addCost: value });
  },

  onAddQty(e) {
    const value = (e.detail && e.detail.value) || '';
    this.setData({ addQty: value });
  },

  onAddConfirm() {
    const code = String(this.data.addCode || '').trim();

    let codeError = '';
    if (!code) codeError = '请输入股票代码';
    else if (!/^\d{6}$/.test(code)) codeError = '股票代码为 6 位数字';

    // 优先用下拉选中时记下的交易所（后端给的是权威值）；
    // 手输没选过的，再按代码前缀反推
    const market = codeError ? '' : this.data.addMarket || marketOf(code);
    if (!codeError && !market) codeError = '暂不支持该交易所，请核对代码';
    if (!codeError && this.data.stocks.some((s) => s.code === `${market} ${code}`)) {
      codeError = '该股票已在自选中';
    }

    if (codeError) {
      this.setData({ codeError });
      return;
    }

    // 只把填了的字段发过去：没填的成本/数量让后端保持原值，
    // 这样重复添加同一只股票时才不会把之前的建仓信息冲掉
    const payload = { stock_code: `${market}${code}` };
    const cost = String(this.data.addCost || '').trim();
    const qty = String(this.data.addQty || '').trim();
    if (cost) payload.cost_price = cost;
    if (qty) payload.quantity = Number(qty) || 0;

    this.setData({ addVisible: false, loading: true });
    api
      .add(payload)
      .then(() => this.loadList())
      .then(() => this.toast(`已添加 ${market} ${code}`))
      .catch((err) => {
        this.setData({ loading: false, errorMsg: (err && err.message) || '添加失败' });
      });
  },

  goBack() {
    // 立即出栈，不做任何自绘前置动画。
    // 兜底：navigateBack 失败时（无上一页，如被单独拉起 / 栈被清）
    // 用 reLaunch 回到主页，避免卡在子页或空白页。
    // 注意：chat 不是 tabBar 页，switchTab 会失败，故用 reLaunch。
    wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  }
});
