// 估值卡：回答「市盈率多少，贵不贵」「分红多少」
// 字段口径见设计文档第 2 节④。两行估值指标（各三列），全部等距居中。
// 分位类信息（PE 分位进度条 / 近5年分位）不出卡：分位必须标注历史区间口径，
// 而卡内按需求不再出现口径文案，故整块移除，只留可独立成立的估值指标。
import { fmtNum, fmtPctPlain, fmtAmount } from '../../../utils/format';

Component({
  externalClasses: ['card-class'],
  properties: {
    // 数值一律用 type: null（任意类型）而不是 Number：声明成 Number 时，
    // 页面传进来的 null 会被框架转成 0，缺失指标就渲染成「0.00%」而不是「--」
    //（线上实测：dv_ttm / peg 为 null，卡片显示成 0.00）
    // 属性名不能用全大写缩略词：微信把 wxml 的 dash-case 转 camelCase 时
    // 只把连字符后的「首字母」大写——pe-ttm 映射成 peTtm，不是 peTTM。
    // 写成 peTTM 会让 wxml 传进来的值落空（PE(TTM) 恒显示「--」），
    // 且组件读不到任何报错。同类命名一律用 peTtm / psTtm 这种首字母大写形式
    peTtm: { type: null, value: null },
    peStatic: { type: null, value: null },
    pb: { type: null, value: null },
    ps: { type: null, value: null },
    peg: { type: null, value: null },
    // 股息率（% 数值，如 3.42）
    dividendYield: { type: null, value: null },
    totalCap: { type: null, value: null },
    note: { type: String, value: '' }
  },
  data: {
    peTtmText: '--',
    peStaticText: '--',
    pbText: '--',
    psText: '--',
    pegText: '--',
    yieldText: '--',
    capText: '--'
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
    'peTtm, peStatic, pb, ps, peg, dividendYield, totalCap'() {
      this.refresh();
    }
  },
  methods: {
    refresh() {
      const d = this.data;
      this.setData({
        peTtmText: fmtNum(d.peTtm),
        peStaticText: fmtNum(d.peStatic),
        pbText: fmtNum(d.pb),
        psText: fmtNum(d.ps),
        pegText: fmtNum(d.peg),
        // 股息率是水平值，不带正负号
        yieldText: fmtPctPlain(d.dividendYield),
        capText: fmtAmount(d.totalCap)
      });
    }
  }
});
