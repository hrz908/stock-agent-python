// 接口层：股票搜索（添加自选时的下拉联想）。
// 与 services/watchlist.js 同一套约定：页面只调这里，不直接碰 wx.request
import { get } from '../utils/request';

const API = '/api/v1';

// 模糊搜索股票：支持代码前缀、汉字名称、拼音缩写（"600" / "茅台" / "maotai"）
// 返回 [{ code, market, name }]，最多 limit 条。
// 结果由后端缓存 1 小时，前端无需再存一份
export function search(keyword, limit = 5) {
  return get(`${API}/stocks/search`, { q: keyword, limit });
}

// 单只标的（个股 / 指数）完整行情快照：{ name, price, change, change_percent, ... }。
// 指数需带市场前缀（如 SH000001 上证指数、SZ399006 创业板指、SH000680 科创综指）；
// 后端缓存 30 秒，抽屉每次打开刷新即可，无需前端再缓存
export function quote(code) {
  return get(`${API}/stock/quote`, { code });
}

// 单周期 K 线：period 取 m1 / m5 / m15 / day / week，limit 默认 60 根。
// 返回 { code, period, bars: [{ d, o, h, l, c, v }] }（时间升序；价量是字符串，
// 由页面转数字）。均线 / 区间统计后端不算，页面按卡片口径自己算
export function kline(code, period, limit = 60) {
  return get(`${API}/stock/kline`, { code, period, limit });
}
