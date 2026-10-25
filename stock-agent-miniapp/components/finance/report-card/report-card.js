// 财报速览卡：回答「业绩怎么样」「增长了吗」
// 字段口径见设计文档第 2 节③。营收/净利/现金流三行走「龙虎榜行」格式：
// 名称靠左、金额与同比靠右（同比颜色由数值正负推导，不手写 up/down class）。
// 文案只陈述事实（合规红线），卡内不出现投资建议措辞。
import { fmtAmount, fmtPct, fmtPctPlain, fmtNum, direction } from '../../../utils/format';

Component({
  externalClasses: ['card-class'],
  properties: {
    // 报告期标签，如「2026 三季报」（报告期日期行已按需求移除）
    periodLabel: { type: String, value: '' },
    // { value: 金额(元), yoy: 同比(% 数值) }
    revenue: { type: Object, value: null },
    netProfit: { type: Object, value: null },
    cashFlow: { type: Object, value: null },
    grossMargin: { type: Number, value: null },
    netMargin: { type: Number, value: null },
    roe: { type: Number, value: null },
    eps: { type: Number, value: null },
    // 一致预期对比结论：'超预期' | '不及预期' | ''（当前数据源没有，保留字段不出卡）
    beat: { type: String, value: '' },
    note: { type: String, value: '' }
  },
  data: {
    revenueRow: { value: '--', yoy: '--', yoyDir: 'flat' },
    profitRow: { value: '--', yoy: '--', yoyDir: 'flat' },
    cashRow: { value: '--', yoy: '--', yoyDir: 'flat' },
    grossMarginText: '--',
    netMarginText: '--',
    roeText: '--',
    epsText: '--'
  },
  lifetimes: {
    // observer 不会在 attached 时对初始值触发：首帧手动算一次
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
    'revenue, netProfit, cashFlow, grossMargin, netMargin, roe, eps'() {
      this.refresh();
    }
  },
  methods: {
    refresh() {
      const d = this.data;
      this.setData({
        revenueRow: this.rowOf(d.revenue),
        profitRow: this.rowOf(d.netProfit),
        cashRow: this.rowOf(d.cashFlow),
        // 三个比率都是水平值（不是同比），不能用带符号的 fmtPct
        grossMarginText: fmtPctPlain(d.grossMargin),
        netMarginText: fmtPctPlain(d.netMargin),
        roeText: fmtPctPlain(d.roe),
        epsText: fmtNum(d.eps)
      });
    },

    // 单项 -> 行数据：金额 + 同比（同比为空时不显示，避免刷一排 '--'）
    rowOf(src) {
      const s = src || {};
      const yoy = parseFloat(s.yoy);
      return {
        value: fmtAmount(s.value),
        yoy: isNaN(yoy) ? '' : fmtPct(yoy),
        yoyDir: direction(s.yoy)
      };
    }
  }
});
