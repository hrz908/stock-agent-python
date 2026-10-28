import * as stockApi from '../../services/stock';
import * as alertApi from '../../services/alert';
import { fontScalePatch } from '../../utils/font-scale';

// 提醒列表数据来自后端 GET /api/v1/alerts，字段与「新增提醒」弹窗一一对应
// （股票 / 触发条件 / 阈值 / 基准周期 / 检查频率）；
// 卡片上的大号阈值、描述、状态标签等文案由 decorate 在拉取后算好，WXML 直接取

// 下拉最多展示几条联想结果（与自选股页保持一致）
const SEARCH_LIMIT = 5;

// 条件 / 基准周期 / 频率 / 阈值规则都定义在 services/alert.js：
// 列表文案与弹窗表单共用一份，新增条件类型不需要改两处
const { CONDITION_OPTIONS, VOLUME_BASE_OPTIONS, FREQUENCY_OPTIONS, THRESHOLD_CONFIG } = alertApi;

// 列表文案：把弹窗里的字段拆成两段——条件（左）与检查频率（右），
// 卡片里左右分栏显示，不再拼成一句用「·」连接
function describeAlert(a) {
  const condOpt = CONDITION_OPTIONS.find(o => o.value === a.conditionType);
  const freqOpt = FREQUENCY_OPTIONS.find(o => o.value === a.checkFrequency);

  let condText;
  if (a.conditionType === 'volume') {
    condText = `成交量放大到 ${a.volumeBase || 20}日均量的 ${a.threshold} 倍`;
  } else {
    const unit = THRESHOLD_CONFIG[a.conditionType] ? THRESHOLD_CONFIG[a.conditionType].unit : '';
    condText = `${condOpt ? condOpt.label : ''} ${a.threshold}${unit}`;
  }
  return { condText, freqText: freqOpt ? freqOpt.label : '' };
}

// 某条触发时间是否落在今天
function isToday(iso) {
  if (!iso) return false;
  const d = parseDate(iso);
  if (!d) return false;
  const today = new Date();
  return (
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate()
  );
}

// 解析后端 RFC3339 时间；解析不出来返回 null，交给调用方兜底
function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

// 概览宫格数据：只保留「全部提醒」和「今日触发」两格，
// 今日触发按 last_triggered_at 是否今天来数
function makeStats(alerts) {
  const todayCount = alerts.filter((a) => isToday(a.lastTriggeredAt)).length;
  return [
    { key: 'total', num: alerts.length, label: '全部提醒' },
    { key: 'today', num: todayCount, label: '今日触发' }
  ];
}

// 大号阈值文案 + 趋势图标：按条件类型决定展示形式
//   价格类：¥1,700.00（涨跌用上下箭头）
//   涨跌幅：+5.00% / -5.00%
//   成交量：1.5 倍
function priceTextOf(a) {
  if (a.conditionType === 'priceUp' || a.conditionType === 'priceDown') {
    return `¥${a.threshold}`;
  }
  if (a.conditionType === 'pctUp') return `${a.threshold}%`;
  if (a.conditionType === 'pctDown') return `${a.threshold}%`;
  return `${a.threshold} 倍`;
}

// 趋势图标：直接给 base-icon 的 type（字体图标集，见 components/base/icon）
function trendIconOf(a) {
  if (a.conditionType === 'priceUp' || a.conditionType === 'pctUp') return 'UpOutline';
  if (a.conditionType === 'priceDown' || a.conditionType === 'pctDown') return 'DownOutline';
  return 'HistogramOutline';
}

// 趋势颜色跟随涨跌：突破 / 涨幅用红，跌破 / 跌幅用绿，成交量保持红。
// 颜色名走新 token（--up/--down），内联到 base-icon 的 style 里由页面解析
function trendColorOf(a) {
  if (a.conditionType === 'priceDown' || a.conditionType === 'pctDown') return 'var(--down)';
  return 'var(--up)';
}

// 现价与目标价的差距：只有价格类条件有意义（涨幅/倍量比的是无量纲阈值）。
// 后端没给现价（停牌 / 行情源抖动）时返回空串，卡片不显示这一行
function gapTextOf(a) {
  const cur = parseFloat(a.currentPrice);
  const target = parseFloat(a.threshold);
  if (!isFinite(cur) || !isFinite(target) || cur <= 0 || target <= 0) return '';
  if (a.conditionType !== 'priceUp' && a.conditionType !== 'priceDown') return '';
  const gap = ((target - cur) / cur) * 100;
  if (Math.abs(gap) < 0.01) return '已到目标价';
  return `距目标 ${gap > 0 ? '+' : ''}${gap.toFixed(2)}%`;
}

// 接口返回的提醒 -> 卡片渲染结构，各段文案在这里算好，WXML 直接取。
// 触发状态标签与触发时间已按需求移除，不再派生 triggerText/time 字段
function decorate(a) {
  const text = describeAlert(a);
  return Object.assign({}, a, {
    condText: text.condText,
    freqText: text.freqText,
    priceText: priceTextOf(a),
    currentText: a.currentPrice ? `现价 ¥${a.currentPrice}` : '',
    gapText: gapTextOf(a),
    trendIcon: trendIconOf(a),
    trendColor: trendColorOf(a)
  });
}

Page({
  data: {
    bottomGap: 20,
    alerts: [],          // 由 GET /api/v1/alerts 填充
    stats: makeStats([]),

    // 删除确认弹窗（base-popup，替代 wx.showModal）。
    // 待删 id 记在 this._pendingDeleteId，不进 data（不参与视图 diff）
    deleteVisible: false,
    deleteDesc: '',

    // ===== 新增/编辑提醒弹窗状态 =====
    // editingId > 0 表示编辑模式：复用同一张表单回填，股票不可换
    popupVisible: false,
    editingId: 0,
    searchKeyword: '',
    searchResults: [],
    selectedStock: null,        // { name, code }
    conditionType: 'priceUp',   // priceUp | priceDown | pctUp | pctDown | volume
    conditionOptions: CONDITION_OPTIONS,
    conditionText: CONDITION_OPTIONS[0].label, // 选择器右侧显示的当前项
    // 原生 picker 的 value 是「数组索引」不是业务值（执行指南 4.3 坑 1），
    // 回显时由业务值反查下标，见 _closePopup
    conditionIndex: 0,
    threshold: '',              // 阈值（字符串，提交前转 number）
    thresholdPlaceholder: THRESHOLD_CONFIG.priceUp.placeholder,
    thresholdUnit: THRESHOLD_CONFIG.priceUp.unit,
    volumeBase: 20,             // 成交量基准（5 / 10 / 20 日均量）
    volumeBaseOptions: VOLUME_BASE_OPTIONS,
    volumeBaseText: '20日均量',  // 选择器右侧显示的当前项
    volumeBaseIndex: 2,         // VOLUME_BASE_OPTIONS 里 20 的下标
    checkFrequency: 'realtime', // realtime(盘中即时) | close(日线收盘确认)
    frequencyOptions: FREQUENCY_OPTIONS,
    frequencyText: FREQUENCY_OPTIONS[0].label,
    frequencyIndex: 0
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

  // 进入页面时加载一次。与自选股页同一套节奏：
  // 只在首次进入时拉数据，从子页面返回时不重复请求
  onShow() {
    this.setData(fontScalePatch());
    if (!this._loaded) {
      this._loaded = true;
      this.loadList();
    }
  },

  // 拉取提醒列表。失败不弹 Toast：页面保持空态，错误留控制台
  loadList() {
    return alertApi
      .fetchList()
      .then((list) => {
        const alerts = (list || []).map(decorate);
        this.setData({ alerts, stats: makeStats(alerts) });
      })
      .catch((err) => {
        console.error('[提醒列表] 加载失败：', err && err.message);
      });
  },

  /* ================= 新增提醒弹窗 ================= */

  // 打开「新增」弹窗（表单不再滚动，高度由 dialog 自己撑）。
  // 表单在此重置为空，而不是在关闭时重置——关闭时若清零，淡出动画期间
  // 输入框 / 已选股票会瞬间变空，出现「闪一下」
  onAdd() {
    this.setData({
      popupVisible: true,
      editingId: 0,
      searchKeyword: '',
      searchResults: [],
      selectedStock: null,
      conditionType: 'priceUp',
      conditionText: CONDITION_OPTIONS[0].label,
      conditionIndex: CONDITION_OPTIONS.findIndex((o) => o.value === 'priceUp'),
      threshold: '',
      thresholdPlaceholder: THRESHOLD_CONFIG.priceUp.placeholder,
      thresholdUnit: THRESHOLD_CONFIG.priceUp.unit,
      volumeBase: 20,
      volumeBaseText: '20日均量',
      volumeBaseIndex: VOLUME_BASE_OPTIONS.findIndex((o) => o.value === 20),
      checkFrequency: 'realtime',
      frequencyText: FREQUENCY_OPTIONS[0].label,
      frequencyIndex: FREQUENCY_OPTIONS.findIndex((o) => o.value === 'realtime')
    });
  },

  // 卡片右上「编辑」：复用新增表单回填当前值，股票只读不可换
  // （后端 PUT /api/v1/alerts/:id 只支持改条件/阈值/基准周期/频率）
  onEdit(e) {
    const id = Number((e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.id) || 0);
    if (!id) return;
    const target = this.data.alerts.filter((a) => a.id === id)[0];
    if (!target) return;

    const cfg = THRESHOLD_CONFIG[target.conditionType] || THRESHOLD_CONFIG.priceUp;
    const condOpt = CONDITION_OPTIONS.filter((o) => o.value === target.conditionType)[0];
    const condIdx = CONDITION_OPTIONS.findIndex((o) => o.value === target.conditionType);
    const vb = target.volumeBase || 20;
    const vbOpt = VOLUME_BASE_OPTIONS.filter((o) => o.value === vb)[0];
    const vbIdx = VOLUME_BASE_OPTIONS.findIndex((o) => o.value === vb);
    const freq = target.checkFrequency || 'realtime';
    const freqOpt = FREQUENCY_OPTIONS.filter((o) => o.value === freq)[0];
    const freqIdx = FREQUENCY_OPTIONS.findIndex((o) => o.value === freq);

    this.setData({
      popupVisible: true,
      editingId: id,
      selectedStock: { name: target.name, code: target.code },
      conditionType: target.conditionType,
      conditionText: condOpt ? condOpt.label : CONDITION_OPTIONS[0].label,
      conditionIndex: condIdx >= 0 ? condIdx : 0,
      threshold: String(target.threshold),
      thresholdPlaceholder: cfg.placeholder,
      thresholdUnit: cfg.unit,
      volumeBase: vb,
      volumeBaseText: vbOpt ? vbOpt.label : '20日均量',
      volumeBaseIndex: vbIdx >= 0 ? vbIdx : 2,
      checkFrequency: freq,
      frequencyText: freqOpt ? freqOpt.label : FREQUENCY_OPTIONS[0].label,
      frequencyIndex: freqIdx >= 0 ? freqIdx : 0
    });
  },

  // 卡片右上「删除」：二次确认后直接删除该提醒（不再进编辑弹窗）。
  // 删除不可逆，故用 base-popup 确认；确认按钮走危险色
  onDelete(e) {
    const id = Number((e && e.currentTarget && e.currentTarget.dataset && e.currentTarget.dataset.id) || 0);
    if (!id) return;
    const target = this.data.alerts.filter((a) => a.id === id)[0];
    const label = target ? `${target.name}（${target.code}）` : '该提醒';
    this._pendingDeleteId = id;
    this.setData({
      deleteVisible: true,
      deleteDesc: `确定删除 ${label} 的提醒吗？此操作不可恢复。`
    });
  },

  // 关闭时保留 deleteDesc：desc 被清空会让面板高度在淡出动画中变矮，
  // 出现「缩小残影」；下次 onDelete 打开时会重新赋值
  cancelDelete() {
    this._pendingDeleteId = 0;
    this.setData({ deleteVisible: false });
  },

  confirmDelete() {
    const id = this._pendingDeleteId;
    if (!id || this._submitting) return;
    this._submitting = true;
    this.setData({ deleteVisible: false });
    alertApi
      .remove(id)
      .then(() => {
        this.toast('提醒已删除');
        this.loadList();
      })
      .catch((err) => {
        this.toast((err && err.message) || '删除提醒失败');
      })
      .then(() => {
        this._submitting = false;
        this._pendingDeleteId = 0;
      });
  },

  // 轻提示：自研 base-toast
  toast(message) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message);
  },

  // 取消按钮：关闭弹窗不保存
  onPopupCancel() {
    this._closePopup();
  },

  // 关闭弹窗：只隐藏，不重置表单字段，也不清零 editingId。
  // 表单在 onAdd 打开时重置、在 onEdit 打开时回填；editingId 同理由下次打开决定。
  // 这里若把 editingId 置 0，编辑态的只读股票行会在淡出动画期间被 wx:if 切回
  // 新增态的搜索框，内容当场互换，造成「闪一下」
  _closePopup() {
    this.setData({ popupVisible: false });
  },

  // ----- 选股搜索（复用自选股的搜索接口，带 300ms 防抖） -----
  onSearchInput(e) {
    const keyword = (e && e.detail && e.detail.value) || '';
    this.setData({ searchKeyword: keyword });
    this.scheduleSearch(keyword);
  },

  // 防抖 300ms：与自选股页同一套节奏，打字过程中不发请求
  scheduleSearch(value) {
    if (this._searchTimer) clearTimeout(this._searchTimer);

    const kw = String(value || '').trim();
    if (!kw) {
      this.setData({ searchResults: [] });
      return;
    }

    this._searchTimer = setTimeout(() => this.doSearch(kw), 300);
  },

  // 调用 services/stock.js 的搜索接口（支持代码/名称/拼音模糊匹配）
  doSearch(kw) {
    stockApi
      .search(kw, SEARCH_LIMIT)
      .then((list) => {
        this.setData({ searchResults: list || [] });
      })
      .catch(() => {
        this.setData({ searchResults: [] });
      });
  },

  onSearchClear() {
    this.setData({ searchKeyword: '', searchResults: [] });
  },

  // 选中股票：保存 name、code、market（与自选股页一致）
  onSelectStock(e) {
    const { code, name, market } = e.currentTarget.dataset;
    this.setData({
      selectedStock: { name, code, market },
      searchKeyword: '',
      searchResults: []
    });
  },

  onClearStock() {
    this.setData({ selectedStock: null });
  },

  // ----- 触发条件（原生 picker） -----
  // detail.value 是选中项的「数组索引」（Number），不是业务值（执行指南 4.3 坑 1）；
  // 先映射回业务对象再更新表单。切换条件时清空旧阈值，占位提示（含单位）跟着换
  onConditionChange(e) {
    const index = Number((e && e.detail && e.detail.value) || 0);
    const opt = CONDITION_OPTIONS[index];
    if (!opt || opt.value === this.data.conditionType) return;

    const cfg = THRESHOLD_CONFIG[opt.value];
    this.setData({
      conditionType: opt.value,
      conditionText: opt.label,
      conditionIndex: index,
      threshold: '',
      thresholdPlaceholder: cfg.placeholder,
      thresholdUnit: cfg.unit
    });
  },

  // ----- 阈值输入 -----
  onThresholdInput(e) {
    const val = (e && e.detail && e.detail.value) || '';
    this.setData({ threshold: val });
  },

  // 阈值失焦：兜底把最终值同步进 data（输入过程 onThresholdInput 已实时同步）
  onThresholdBlur(e) {
    const val = (e && e.detail && e.detail.value) || '';
    this.setData({ threshold: val });
  },

  // ----- 基准周期（原生 picker） -----
  onVolumeBaseChange(e) {
    const index = Number((e && e.detail && e.detail.value) || 0);
    const opt = VOLUME_BASE_OPTIONS[index];
    if (!opt || index === this.data.volumeBaseIndex) return;
    this.setData({ volumeBase: opt.value, volumeBaseText: opt.label, volumeBaseIndex: index });
  },

  // ----- 检查频率（原生 picker） -----
  onFrequencyChange(e) {
    const index = Number((e && e.detail && e.detail.value) || 0);
    const opt = FREQUENCY_OPTIONS[index];
    if (!opt || opt.value === this.data.checkFrequency) return;
    this.setData({ checkFrequency: opt.value, frequencyText: opt.label, frequencyIndex: index });
  },

  // ----- 表单校验 -----
  _validateForm() {
    const { selectedStock, threshold, conditionType, editingId } = this.data;
    // 编辑模式股票固定，无需校验
    if (!editingId && !selectedStock) {
      return '请先选择一只股票';
    }
    const numVal = parseFloat(threshold);
    if (!threshold || isNaN(numVal)) {
      return '请输入阈值';
    }
    const cfg = THRESHOLD_CONFIG[conditionType];
    if (numVal <= cfg.min) {
      return `阈值必须大于 ${cfg.min}`;
    }
    if (numVal > cfg.max) {
      return `阈值不能超过 ${cfg.maxText || cfg.max}`;
    }
    // 价格类保留两位小数校验
    if ((conditionType === 'priceUp' || conditionType === 'priceDown') && !/^\d+(\.\d{1,2})?$/.test(threshold)) {
      return '价格最多保留两位小数';
    }
    return null;
  },

  // ----- 确认按钮（新增 / 编辑）-----
  onPopupConfirm() {
    // 校验
    const errMsg = this._validateForm();
    if (errMsg) {
      this.toast(errMsg);
      return;
    }

    // 连点保护：请求回来之前不再发第二次
    if (this._submitting) return;
    this._submitting = true;

    // 编辑模式：只提交可改字段（条件/阈值/基准周期/频率），股票不可换
    let req;
    if (this.data.editingId) {
      req = alertApi
        .update(this.data.editingId, {
          conditionType: this.data.conditionType,
          threshold: parseFloat(this.data.threshold),
          volumeBase: this.data.conditionType === 'volume' ? this.data.volumeBase : 0,
          checkFrequency: this.data.checkFrequency
        })
        .then(() => {
          this.toast('提醒已更新');
          this._closePopup();
          this.loadList();
        });
    } else {
      req = alertApi.create(this._payload()).then(() => {
        this.toast('提醒添加成功');
        this._closePopup();
        this.loadList();
      });
    }

    req
      .catch((err) => {
        // 409「该股票的该条件提醒已存在」这类文案后端已写好，直接用它的
        this.toast((err && err.message) || '保存失败');
      })
      .then(() => {
        this._submitting = false;
      });
  },

  // 提交载荷：与后端 /api/v1/alerts 的字段一一对应。
  // 股票代码跟添加自选一样带交易所前缀（搜索结果里已拼好）
  _payload() {
    const { selectedStock, conditionType, threshold, volumeBase, checkFrequency } = this.data;
    return {
      stockCode: (selectedStock.market || '') + selectedStock.code,
      stockName: selectedStock.name,
      conditionType: conditionType,
      threshold: parseFloat(threshold),
      // 只有成交量异动需要基准周期，其余传 0（后端会强制归 0）
      volumeBase: conditionType === 'volume' ? volumeBase : 0,
      checkFrequency: checkFrequency
    };
  },

  goBack() {
    // 立即出栈，不做任何自绘前置动画：
    // 任何「先动画后出栈」的写法都会把等待时间直接变成点击响应延迟。
    // 兜底：navigateBack 失败时（无上一页）用 reLaunch 回到主页。
    // 注意：chat 不是 tabBar 页，switchTab 会失败，故用 reLaunch。
    wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  }
});
