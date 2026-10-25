// 环境配置
//
// 小程序没有 .env 机制，官方推荐用 envVersion 区分环境：
//   develop  开发版（开发者工具 / 真机调试）
//   trial    体验版
//   release  正式版
//
// 改后端地址只动这张表，其它文件一律从这里取

const ENV_CONFIG = {
  develop: {
    // 远程服务器（ubuntu@tearsinrain.top）：开发版直连 HTTPS，需在开发者工具里
    // 勾选「不校验合法域名、TLS 版本以及 HTTPS 证书」（project.config.json 的 urlCheck 已为 false）
    apiBase: 'https://tearsinrain.top'
  },
  // 体验版/正式版必须用已备案域名 + CA 证书，小程序后台的 request 合法域名
  // 也必须是同一个域名（自签 + IP 只能用于 develop）。
  trial: {
    apiBase: 'https://tearsinrain.top'
  },
  release: {
    apiBase: 'https://tearsinrain.top'
  }
};

function envVersion() {
  try {
    const info = wx.getAccountInfoSync();
    return (info && info.miniProgram && info.miniProgram.envVersion) || 'release';
  } catch (e) {
    // 老基础库或异常场景，按正式环境处理
    return 'release';
  }
}

function currentConfig() {
  return ENV_CONFIG[envVersion()] || ENV_CONFIG.release;
}

const config = currentConfig();

export const apiBase = config.apiBase;

// 开发版专属能力（卡片画廊这类假数据页、调试入口）用它开闸：
// 体验版/正式版也会走到同一份代码，靠 IDE 的 urlCheck 之类开关是关不掉入口的。
export const isDev = envVersion() === 'develop';

// AI 对话的 SSE 流式通道（server 侧 agent stream）：每次提问一个独立
// POST 请求，响应 text/event-stream 逐帧下发，走 Header Bearer 鉴权
//（见 services/agent.js 与 server/internal/agent/sse_agent.go）
export const agentSsePath = '/api/v1/agent/stream';
