// 接口层：页面只调这里，不直接碰 wx.request。
// 统一用 ES Module（与 utils/request.js、config/index.js 保持一致）
import { get, post, del } from '../utils/request';

const API = '/api/v1';

// filter: all | up | down；order: 可选 asc | desc（仅 filter=all 时生效）
// 返回：{ list, total_profit, total_profit_rate, up_count, down_count }
export function fetchList(filter = 'all', order = '') {
  const data = { filter };
  if (order) data.order = order;
  return get(`${API}/watchlist`, data);
}

// payload: { stock_code, cost_price?, quantity? }
// 重复添加时后端只更新成本与数量（Upsert），并返回原记录 id
export function add(payload) {
  return post(`${API}/watchlist`, payload);
}

// 手动刷新行情（后端顺带检查提醒规则，命中走企业微信推送）
// 返回结构与 fetchList 一致，便于前端直接替换列表与统计
export function refresh(filter = 'all', order = '') {
  const data = { filter };
  if (order) data.order = order;
  return post(`${API}/watchlist/refresh`, data);
}

// 移出自选：软删除（enabled=false），保留历史记录
export function remove(stockCode) {
  return del(`${API}/watchlist/${encodeURIComponent(stockCode)}`);
}
