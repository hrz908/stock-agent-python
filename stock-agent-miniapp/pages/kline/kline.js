// 自选 K 线：顶部模糊搜索选股，选中后下方五张固定周期卡片（1/5/15 分钟、日、周）各自拉数。
//
// 两条结构约定：
// 1) 五张卡片进页面就存在（空值），不是拉完数据再动态创建——切股票时卡片位置不跳、
//    图区高度不变，用户看到的是「同一张卡换了数据」而不是整页重建
// 2) 均线、区间统计都在这里按卡片口径算：后端只返回 OHLCV，
//    避免每个周期都在服务端重复算一遍同样的派生指标
import { search, kline } from '../../services/stock';

// 下拉最多展示几条联想结果（与自选股 / 提醒页一致）
const SEARCH_LIMIT = 5;

// 固定周期：key 与服务端 /stock/kline 的 period 一一对应
const PERIODS = [
  { key: 'm1', label: '1分钟K线' },
  { key: 'm5', label: '5分钟K线' },
  { key: 'm15', label: '15分钟K线' },
  { key: 'day', label: '日K线' },
  { key: 'week', label: '周K线' }
];

// 空卡片：klines 为空时组件走空态占位，不画图也不显示假数字
function emptyCard(period) {
  return {
    key: period.key,
    label: period.label,
    klines: [],
    ma: null,
    periodPct: null,
    amplitude: null,
    volumeRatio: null,
    support: null,
    resistance: null
  };
}

// 服务端 decimal 序列化成字符串，这里统一转成数字给图表用；
// 空串（停牌 / 缺数据）会被 Number 变成 NaN，整根丢掉
function toBars(bars) {
  const out = [];
  (bars || []).forEach((b) => {
    const o = Number(b.o);
    const h = Number(b.h);
    const l = Number(b.l);
    const c = Number(b.c);
    if (isNaN(o) || isNaN(h) || isNaN(l) || isNaN(c)) return;
    out.push({ d: b.d, o: o, h: h, l: l, c: c, v: Number(b.v) || 0 });
  });
  return out;
}

// 简单均线：不足 n 根的位置给 null，组件按 null 断开折线
function maOf(bars, n) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].c;
    if (i >= n) sum -= bars[i - n].c;
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}

// 卡片的四个指标口径（与 K 线走势卡一致）：
// 区间涨幅 = (末收盘 - 首开盘) / 首开盘；振幅 = (区间最高 - 区间最低) / 首开盘；
// 量比 = 末根成交量 / 其余各根均量；支撑 / 压力取区间最低 / 最高
function statsOf(bars) {
  if (!bars.length) {
    return { pct: null, amplitude: null, volumeRatio: null, support: null, resistance: null };
  }
  const first = bars[0];
  const last = bars[bars.length - 1];
  const base = first.o || first.c;
  let maxH = -Infinity;
  let minL = Infinity;
  bars.forEach((b) => {
    if (b.h > maxH) maxH = b.h;
    if (b.l < minL) minL = b.l;
  });

  const prior = bars.slice(0, -1);
  let avgV = 0;
  if (prior.length) {
    let sum = 0;
    prior.forEach((b) => {
      sum += b.v;
    });
    avgV = sum / prior.length;
  }

  return {
    pct: base ? ((last.c - base) / base) * 100 : null,
    amplitude: base ? ((maxH - minL) / base) * 100 : null,
    volumeRatio: avgV > 0 ? last.v / avgV : null,
    support: minL,
    resistance: maxH
  };
}

Page({
  data: {
    keyword: '',
    results: [],
    // 当前选中的股票 { name, code, market }
    stock: null,
    // 卡片副标题：名称 + 代码，一次算好，五张卡共用
    stockLabel: '',
    loading: false,
    // 五张预设空卡
    cards: PERIODS.map(emptyCard)
  },

  onUnload() {
    if (this._searchTimer) clearTimeout(this._searchTimer);
    // 页面销毁后回来的响应不再写 data
    this._seq = (this._seq || 0) + 1;
  },

  goBack() {
    wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  },

  toast(message) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message);
  },

  /* ===== 搜索选股 ===== */

  onSearchInput(e) {
    const keyword = (e && e.detail && e.detail.value) || '';
    this.setData({ keyword: keyword });
    this.scheduleSearch(keyword);
  },

  // 防抖 300ms：打字过程中不发请求，与自选股 / 提醒页同一套节奏
  scheduleSearch(value) {
    if (this._searchTimer) clearTimeout(this._searchTimer);
    const kw = String(value || '').trim();
    if (!kw) {
      this.setData({ results: [] });
      return;
    }
    this._searchTimer = setTimeout(() => this.doSearch(kw), 300);
  },

  doSearch(kw) {
    search(kw, SEARCH_LIMIT)
      .then((list) => this.setData({ results: list || [] }))
      .catch(() => this.setData({ results: [] }));
  },

  onSearchClear() {
    this.setData({ keyword: '', results: [] });
  },

  onSelectStock(e) {
    const ds = e.currentTarget.dataset;
    const stock = { name: ds.name, code: ds.code, market: ds.market || '' };
    this.setData({
      stock: stock,
      stockLabel: stock.name + ' ' + stock.market + stock.code,
      keyword: '',
      results: [],
      // 换股票时先把五张卡清空，避免旧图挂在新标题下
      cards: PERIODS.map(emptyCard)
    });
    this.loadAll();
  },

  /* ===== 五个周期各自拉数 ===== */

  // 手动刷新：卡片保留旧图直到各周期新数据回来，不闪空态
  onRefresh() {
    if (!this.data.stock || this.data.loading) return;
    this.loadAll(true);
  },

  // manual：手动刷新时成功给一次轻提示，首次选股不打扰
  loadAll(manual) {
    const stock = this.data.stock;
    if (!stock) return;

    const code = stock.market + stock.code;
    // 请求序号：换股票时旧请求回来得晚，不允许覆盖新股票的数据
    this._seq = (this._seq || 0) + 1;
    const seq = this._seq;
    this.setData({ loading: true });

    const tasks = this.data.cards.map((card, index) =>
      kline(code, card.key)
        .then((res) => {
          if (seq !== this._seq) return;
          this.fillCard(index, toBars(res && res.bars));
        })
        .catch(() => {
          if (seq !== this._seq) return;
          this.fillCard(index, []);
        })
    );

    Promise.all(tasks).then(() => {
      if (seq !== this._seq) return;
      this.setData({ loading: false });
      const empty = this.data.cards.every((c) => !c.klines.length);
      if (empty) this.toast('K线获取失败，请稍后重试');
      else if (manual) this.toast('K线已刷新');
    });
  },

  fillCard(index, bars) {
    const card = this.data.cards[index];
    if (!card) return;
    const stats = statsOf(bars);
    const patch = {};
    patch['cards[' + index + ']'] = {
      key: card.key,
      label: card.label,
      klines: bars,
      ma: bars.length ? { ma5: maOf(bars, 5), ma10: maOf(bars, 10), ma20: maOf(bars, 20) } : null,
      periodPct: stats.pct,
      amplitude: stats.amplitude,
      volumeRatio: stats.volumeRatio,
      support: stats.support,
      resistance: stats.resistance
    };
    this.setData(patch);
  }
});
