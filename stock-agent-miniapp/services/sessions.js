// 会话历史服务：小程序「历史对话」抽屉的数据源，页面只调这里，不直接碰 wx.request。
//
// 链路：本服务 -> Go 网关 /api/v1/agent/sessions* -> Python Agent /sessions*
// （注册表 chat_sessions + LangGraph checkpoints，删除会连 checkpoint 一起清），鉴权走 Bearer。
//
// 「新对话」契约：createSession(sid, '') 先注册占位行（title 传空 -> 后端落「新对话」，
// 首条真实提问到达后替换）；成功后等抽屉收起动画结束再压栈 /pages/chat/chat?new=1&session=<sid>，
// 失败 toast「新建对话失败」并留在原会话。sid 由 agentSocket.freshSessionId() 现生成，
// 只允许 [A-Za-z0-9_-]（≤64）；新页 onLoad 沿用该 session，
// 后端 thread_id = default:{user_id}:{session_id}，多轮上下文因此连续。
import { get, post, del } from '../utils/request';

const API = '/api/v1';

// 会话列表：按最近活跃倒序，返回 { sessions: [{ session_id, title, updated_at }] }
export function listSessions() {
  return get(`${API}/agent/sessions`);
}

// 注册新对话：「新对话」按钮跳转前调用，让空会话立即进历史列表
export function createSession(sessionId, title) {
  return post(`${API}/agent/sessions`, { session_id: sessionId, title: title || '' });
}

// 某会话的纯文本消息（历史列表点选后恢复上下文）；
// 返回 { messages: [{ role: 'user' | 'assistant', content }] }，结构化卡片不持久化
export function sessionMessages(sessionId) {
  return get(`${API}/agent/sessions/${encodeURIComponent(sessionId)}/messages`);
}

// 删除会话：后端连 checkpoints 与滚动摘要一起清，删完不可恢复
export function deleteSession(sessionId) {
  return del(`${API}/agent/sessions/${encodeURIComponent(sessionId)}`);
}
