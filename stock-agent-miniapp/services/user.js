import { apiBase } from '../config';
import { ensureLogin } from '../utils/request';
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

// 头像上传两条通道：
//   1) 主通道 POST /user/avatar/base64：wx.request 发 base64。微信后台的「合法域名」
//      分 request / uploadFile 两份列表，只配了 request 时 wx.uploadFile 连请求都发不
//      出去（fail: url not in domain list），表现就是「选完图点保存，头像没变」；
//   2) 兜底 POST /user/avatar：wx.uploadFile（multipart）。开发者工具里的临时路径是
//      http://tmp/xxx，readFile 读不出来，只能走这条路。
// 返回 { avatar_url }（站内相对路径），展示前用 absoluteUrl 拼成完整地址
const AVATAR_MAX_BYTES = 2 * 1024 * 1024;
// 大图先压一道：base64 会膨胀 4/3，2MB 的图请求体接近 2.7MB，弱网下很容易超时
const COMPRESS_OVER_BYTES = 1024 * 1024;
const UPLOAD_TIMEOUT = 30000;

function friendlyUploadError(errMsg) {
  const msg = String(errMsg || '');
  if (msg.indexOf('domain list') !== -1) return '头像上传失败：域名未配置';
  if (msg.indexOf('privacy') !== -1) return '请先同意隐私协议后重试';
  if (msg.indexOf('timeout') !== -1) return '上传超时，请重试';
  return '';
}

function fileSize(filePath) {
  return new Promise((resolve) => {
    const fs = wx.getFileSystemManager && wx.getFileSystemManager();
    if (!fs || !fs.getFileInfo) {
      resolve(0);
      return;
    }
    fs.getFileInfo({
      filePath,
      success: (res) => resolve((res && res.size) || 0),
      fail: () => resolve(0)
    });
  });
}

// 超过 1MB 才压，quality 80 对头像足够；压缩失败就用原图继续（后端还有 2MB 兜底）
function compressIfNeeded(filePath, size) {
  if (!size || size <= COMPRESS_OVER_BYTES) return Promise.resolve(filePath);
  if (!wx.compressImage || !/\.(jpe?g)$/i.test(filePath)) return Promise.resolve(filePath);
  return new Promise((resolve) => {
    wx.compressImage({
      src: filePath,
      quality: 80,
      success: (res) => resolve((res && res.tempFilePath) || filePath),
      fail: () => resolve(filePath)
    });
  });
}

function readBase64(filePath) {
  return new Promise((resolve, reject) => {
    const fs = wx.getFileSystemManager && wx.getFileSystemManager();
    if (!fs || !fs.readFile) {
      reject(new Error('readFile unavailable'));
      return;
    }
    fs.readFile({
      filePath,
      encoding: 'base64',
      success: (res) => resolve((res && res.data) || ''),
      fail: (err) => reject(new Error((err && err.errMsg) || 'readFile failed'))
    });
  });
}

function pickFilename(filePath) {
  const m = /\.(jpe?g|png|webp)$/i.exec(filePath || '');
  return m ? 'avatar.' + m[1].toLowerCase() : 'avatar.jpg';
}

// transport = true 表示「请求根本没发出去 / 读不到本地文件」，只有这种情况才值得换通道重试；
// 服务端明确拒绝（体积、类型、鉴权）换通道也不会成功，直接把原始提示交给页面
function transportError(message) {
  const err = new Error(message || '头像上传失败');
  err.transport = true;
  return err;
}

function uploadBase64(filePath, token) {
  return readBase64(filePath)
    .catch(() => {
      throw transportError('头像读取失败');
    })
    .then(
      (data) =>
        new Promise((resolve, reject) => {
          wx.request({
            url: apiBase + '/api/v1/user/avatar/base64',
            method: 'POST',
            header: {
              'Content-Type': 'application/json',
              Authorization: 'Bearer ' + token
            },
            timeout: UPLOAD_TIMEOUT,
            data: { filename: pickFilename(filePath), data },
            success: (res) => {
              const body = res.data && typeof res.data === 'object' ? res.data : {};
              if (res.statusCode === 200 && body.code === 0 && body.data) {
                resolve(body.data);
                return;
              }
              reject(new Error(body.message || '头像上传失败'));
            },
            fail: (err) => {
              console.error('[avatar:base64:fail]', err && err.errMsg);
              reject(transportError(friendlyUploadError(err && err.errMsg)));
            }
          });
        })
    );
}

function uploadForm(filePath, token) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: apiBase + '/api/v1/user/avatar',
      filePath,
      name: 'file',
      header: { Authorization: 'Bearer ' + token },
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
      fail: (err) => {
        console.error('[avatar:uploadFile:fail]', err && err.errMsg);
        reject(transportError(friendlyUploadError(err && err.errMsg)));
      }
    });
  });
}

export function uploadAvatar(filePath) {
  return ensureLogin(false).then((token) => {
    // ensureLogin 拿不到 token 时 resolve 空串，别让后续请求白跑一趟 401
    if (!token) throw new Error('登录态失效，请重新登录');
    return fileSize(filePath)
      // 先压再量体积：手机直出照片常有 2~6MB，压完才判断有没有超限
      .then((size) => compressIfNeeded(filePath, size))
      .then((path) =>
        fileSize(path).then((size) => {
          if (size > AVATAR_MAX_BYTES) throw new Error('头像不能超过 2MB');
          return path;
        })
      )
      .then((path) =>
        uploadBase64(path, token).catch((err) => {
          if (!err || !err.transport) throw err;
          return uploadForm(filePath, token);
        })
      );
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
