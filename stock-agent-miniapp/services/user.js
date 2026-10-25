import { apiBase } from '../config';
import { getToken, ensureLogin } from '../utils/request';
import { get, put, del } from '../utils/request';

// 登录只有一条实现：utils/request.js 的 ensureLogin（带 Promise 复用与 401 重登）。
// 这里曾经有一份独立的 login()，与它逻辑重复且没有调用方，已删除。

export function getProfile() {
  return get('/api/v1/user/profile');
}

// 回写资料：后端只认站内相对路径（/uploads/avatars/xxx.png），
// 页面手里的是 absoluteUrl 拼好的完整地址，这里统一还原，免得收到 400「头像地址不合法」
export function updateProfile(data) {
  const body = Object.assign({}, data);
  if (body.avatar) body.avatar = relativeUrl(body.avatar);
  return put('/api/v1/user/profile', body);
}

// 头像上传必须用 wx.uploadFile（multipart），wx.request 发不了本地文件。
// 返回 { avatar_url }，是站内相对路径，展示前用 absoluteUrl 拼成完整地址
export function uploadAvatar(filePath) {
  return new Promise((resolve, reject) => {
    // 上传走的是受保护接口，先确保手上有 token
    ensureLogin(false)
      .then(() => {
        wx.uploadFile({
          url: apiBase + '/api/v1/user/avatar',
          filePath: filePath,
          name: 'file',
          header: { Authorization: 'Bearer ' + getToken() },
          success: (res) => {
            // uploadFile 的响应体是字符串，得自己解析
            let body = {};
            try {
              body = JSON.parse(res.data);
            } catch (e) {
              body = {};
            }
            if (res.statusCode === 200 && body.code === 0 && body.data) {
              resolve(body.data);
              return;
            }
            reject(new Error(body.message || '头像上传失败'));
          },
          fail: () => reject(new Error('头像上传失败'))
        });
      })
      .catch(() => reject(new Error('登录态失效，请重新登录')));
  });
}

// 后端存的是相对路径（/uploads/avatars/xxx.png），
// 图片 src 必须是完整地址，这里统一拼上后端基地址
export function absoluteUrl(path) {
  if (!path) return '';
  if (/^https?:\/\//i.test(path)) return path;
  return apiBase + path;
}

// absoluteUrl 的逆操作：站内地址去掉基地址前缀，其它（外链/本地临时路径）原样返回
export function relativeUrl(url) {
  if (!url) return '';
  if (url.indexOf(apiBase) === 0) return url.slice(apiBase.length);
  return url;
}

// 注销账号：后端硬删用户与自选/提醒数据（含头像文件），不可恢复。
// 对话会话与 checkpoint 在 Agent 侧，必须由页面在调用本接口前逐条删除
export function deleteAccount() {
  return del('/api/v1/user/account');
}
