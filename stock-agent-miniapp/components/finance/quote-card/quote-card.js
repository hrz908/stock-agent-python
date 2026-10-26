// 行情速览卡：回答「某某现在多少钱」「今天涨了多少」
// 字段口径见设计文档第 2 节①；props 收展示数据，组件内只做
// 显示格式化（utils/format.js），不做业务判断。
// 涨跌方向由 changePct 正负自动推导（执行指南第 6 节：不手写 class）。
import { fmtPrice, fmtSign, fmtPct, fmtAmount, fmtNum, direction } from '../../../utils/format';

Component({
  externalClasses: ['card-class'],
  properties: {
    // 数值一律用 type: null（任意类型）而不是 Number：声明成 Number 时，
    // 页面传进来的 null 会被框架转成 0，缺失字段就渲染成「0.00」而不是「--」
    name: { type: String, value: '' },
    // 含交易所前缀的代码，如 SH600519
    code: { type: String, value: '' },
    // 交易状态：已收盘 / 交易中 / 停牌 / 涨停 / 跌停 / 复牌
    status: { type: String, value: '' },
    price: { type: null, value: null },
    change: { type: null, value: null },
    changePct: { type: null, value: null },
    open: { type: null, value: null },
    high: { type: null, value: null },
    low: { type: null, value: null },
    prevClose: { type: null, value: null },
    // 成交额（元）
    amount: { type: null, value: null },
    // 换手率（% 数值，如 0.42）
    turnoverRate: { type: null, value: null },
    // 总市值（元）
    totalCap: { type: null, value: null },
    // 分时点位数组（画迷你走势线）
    points: { type: Array, value: [] },
    // 悬浮标签 [{ text, theme }]，theme: '' | 'hot' | 'gold'
    tags: { type: Array, value: [] },
    // 底部说明（行情时间 / 数据日期）
    note: { type: String, value: '' }
  },
  data: {
    priceText: '--',
    changeText: '--',
    pctText: '--',
    openText: '--',
    highText: '--',
    lowText: '--',
    amountText: '--',
    turnoverText: '--',
    capText: '--',
    dir: 'flat'
  },
  lifetimes: {
    // observer 不会在 attached 时对初始值触发（微信官方行为）：
    // 卡片画廊首屏、历史消息恢复这类「初始就带值」的场景必须手动算一次
    attached() {
      this.refresh();
    },

    // ready 再兜一次：wx:if 首帧创建时 properties 可能尚未就绪，
    // attached 读到的是初始值（observer 对首值也不触发），ready 时必然已就绪
    ready() {
      this.refresh();
    }
  },
  observers: {
    'price, change, changePct, open, high, low, amount, turnoverRate, totalCap'() {
      this.refresh();
    }
  },
  methods: {
    refresh() {
      const d = this.data;
      this.setData({
        priceText: fmtPrice(d.price),
        changeText: fmtSign(d.change),
        pctText: fmtPct(d.changePct),
        openText: fmtPrice(d.open),
        highText: fmtPrice(d.high),
        lowText: fmtPrice(d.low),
        amountText: fmtAmount(d.amount),
        // 换手率是中性指标，不跟涨跌语义，不能用 fmtPct（会带正负号）
        turnoverText: d.turnoverRate == null ? '--' : fmtNum(d.turnoverRate, 2) + '%',
        capText: fmtAmount(d.totalCap),
        dir: direction(d.changePct)
      });
    }
  }
});
