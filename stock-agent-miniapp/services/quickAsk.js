// 首屏引导问题：候选池、加权抽样、问句模板都在网关侧完成，
// 前端只负责渲染与点击上报，不再本地随机股票。
import { get, post } from '../utils/request';

const API = '/api/v1';

// 拉一组建议问题：count 由后端收敛到 3~6，session_id 用于会话级去重
// 返回 { quick_asks: [{ id, display, query, stock_code, stock_name, tag }], generated_at, pool_size, source }
export function list(count = 4, sessionId = '') {
  return get(`${API}/quick-asks`, { count, session_id: sessionId });
}

// 点击上报：这是热度池里「站内行为」信号的来源，失败不影响发问，调用方不要等结果
export function click(stockCode, id) {
  return post(`${API}/quick-asks/click`, {
    stock_code: stockCode,
    quick_ask_id: id
  });
}
