// 首屏引导问题：候选池、加权抽样、问句模板都在网关侧完成，
// 前端只负责渲染与点击上报，不再本地随机股票。
import { get, post } from '../utils/request';
import { stockName } from '../utils/format';

const API = '/api/v1';

// 问句是后端用 stock_name 拼出来的（"万  科Ａ现在什么价？"），
// 只清洗 stock_name 不够：display / query 里的原串要一并替换成干净的名字，
// 否则展示与发送的问句里仍留着补位空格
function cleanAsk(item) {
  const raw = String((item && item.stock_name) || '');
  const clean = stockName(raw);
  const fix = (text) => {
    const src = String(text || '');
    return raw && raw !== clean ? src.split(raw).join(clean) : src;
  };
  return Object.assign({}, item, {
    stock_name: clean,
    display: fix(item && item.display),
    query: fix(item && item.query)
  });
}

// 拉一组建议问题：count 由后端收敛到 3~6，session_id 用于会话级去重
// 返回 { quick_asks: [{ id, display, query, stock_code, stock_name, tag }], generated_at, pool_size, source }
export function list(count = 4, sessionId = '') {
  return get(`${API}/quick-asks`, { count, session_id: sessionId }).then((data) => {
    const asks = (data && data.quick_asks) || [];
    if (!asks.length) return data;
    return Object.assign({}, data, { quick_asks: asks.map(cleanAsk) });
  });
}

// 点击上报：这是热度池里「站内行为」信号的来源，失败不影响发问，调用方不要等结果。
// 只上报股票代码：热度分是按标的聚合的，提问 id 后端不消费（多一个无主字段只会误导）
export function click(stockCode) {
  return post(`${API}/quick-asks/click`, { stock_code: stockCode });
}
