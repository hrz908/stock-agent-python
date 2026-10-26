import { clearSession, ensureLogin } from '../../utils/request';
import {
  getProfile,
  updateProfile,
  uploadAvatar,
  absoluteUrl,
  relativeUrl,
  deleteAccount
} from '../../services/user';
import { listSessions, deleteSession } from '../../services/sessions';
import { FONT_SCALE_OPTIONS, fontScalePatch, setFontScale } from '../../utils/font-scale';

const NICKNAME_MAX = 10;

// 注销前先把会话逐条删掉：checkpoint 与摘要都在 Agent 侧，只有这个接口能清。
// 单条失败不阻断整体注销——后端硬删用户之后，残留数据已无法关联到本人
function clearAllSessions() {
  return listSessions()
    .then((data) => {
      const list = (data && data.sessions) || [];
      return list.reduce(
        (p, s) => p.then(() => deleteSession(s.session_id).catch(() => {})),
        Promise.resolve()
      );
    })
    .catch(() => {});
}

// 没完善过资料时的兜底昵称。后端建号时默认给的就是这个值，
// 前端保持一致：后端返回空昵称时也用这个显示
const DEFAULT_NICKNAME = '微信用户';

// 头像只有落在站内 avatars 目录下才算「已上传」。
// 别用 http 前缀判断：安卓 chooseAvatar 回的是 http://tmp/xxx.jpeg，
// 也长得像远程地址，跳过上传就会拿临时路径去撞后端的「头像地址不合法」，
// 连带同一次提交的昵称一起存不上
function isStoredAvatar(path) {
  return path.indexOf('/uploads/avatars/') === 0;
}

Page({
  data: {
    // 个人信息
    nickname: '',
    avatar: '',
    // 实际渲染用的地址：正常等于 avatar，域名证书不被信任时会换成 getImageInfo
    // 拿到的本地临时路径（见 resolveAvatarSrc）。提交给后端的始终是 avatar
    avatarSrc: '',

    // 编辑资料弹窗
    profileVisible: false,
    editNickname: '',
    editAvatar: '',
    editAvatarSrc: '',
    // 超长时的输入框提示文案。空串表示没超——WXML 里 wx:if 为空不渲染提示行
    nicknameTip: '',
    // 昵称输入框是否处于「取微信昵称」模式：只决定 type（nickname/text）。
    // 只有点下方「使用微信昵称」才为 true，平时为 false，避免一聚焦就弹候选栏。
    nicknamePicking: false,
    // 焦点状态：必须与输入框的「真实焦点」保持一致（focus 事件置 true、blur 事件置 false）。
    // 不能把它当成「收起键盘」的命令：把已聚焦输入框的 focus 下发成 false，
    // 微信会立刻让它失焦，表现就是「删除一个字符键盘就收起、没法连续编辑」。
    // 要主动收键盘请用 wx.hideKeyboard()，它会触发 blur，变量随之归位
    nicknameFocus: false,
    // 「使用微信昵称」按键刚触发过聚焦的标记：用于区分「按键唤起面板」和
    // 「用户手动点进输入框」。面板关掉后若残留 nickname 类型，用户再次点输入框
    // 不应再弹面板，只能靠按键唤起，所以这次聚焦消费掉标记后即按手动处理
    nicknameArmed: false,

    // 字体大小：三档全局字号（标准/大/超大），选择写入本地缓存（见 utils/font-scale）
    fontScaleOptions: FONT_SCALE_OPTIONS,
    fontScale: 'standard',
    fontScaleClass: '',

    // 退出登录二次确认
    logoutVisible: false,

    // 注销账号：二次确认 + 提交中（挡连点）
    deleteAccountVisible: false,
    deletingAccount: false,

    // 底部退出登录按钮距屏幕底部的距离（安全区 + 留白），与其它子页面一致
    bottomGap: 20
  },

  onLoad() {
    // 计算底部安全区，让退出登录按钮与自选股 / 提醒页的底部按钮处于同一位置
    let safeBottom = 0;
    try {
      const win = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync();
      safeBottom =
        (win.safeAreaInsets && win.safeAreaInsets.bottom) ||
        (win.screenHeight && win.safeArea ? win.screenHeight - win.safeArea.bottom : 0) ||
        0;
    } catch (e) {
      // 拿不到就用默认值
    }
    // 字号档位与安全区一起下发：根节点先挂好档位类，首屏就是用户选的字号
    this.setData(Object.assign({ bottomGap: 20 + safeBottom }, fontScalePatch()));

    // 打开页面时确保有 token（App.onLaunch 已经静默登录过，
    // 这里只是兑底：万一 token 被清，自动重新静默登录一次）。
    // ensureLogin 拿到 token 后再拉资料，失败不打断页面，用默认值顶着
    ensureLogin(false).then(() => this.loadProfile());
  },


  goBack() {
    // 立即出栈，不做任何自绘前置动画。
    // 兜底：navigateBack 失败时（无上一页，如被单独拉起 / 栈被清）
    // 用 reLaunch 回到主页，避免卡在「我的」页或空白页。
    // 注意：chat 不是 tabBar 页，switchTab 会失败，故用 reLaunch。
    wx.navigateBack({ fail: () => wx.reLaunch({ url: '/pages/chat/chat' }) });
  },

  // 轻提示：自研 base-toast（替代 TDesign toast / wx.showToast 默认样式）
  toast(message) {
    const toast = this.selectComponent('#base-toast');
    if (toast) toast.show(message);
  },

  /* ===== 个人信息卡 ===== */

  // 个人信息卡：点击打开编辑资料弹窗，头像昵称的完善由用户主动触发
  onProfileTap() {
    this.openProfile();
  },

  // 拉取后端资料覆盖本地展示
  loadProfile() {
    getProfile()
      .then((res) => {
        const avatar = absoluteUrl(res && res.avatar);
        this.setData({
          nickname: (res && res.nickname) || DEFAULT_NICKNAME,
          avatar: avatar,
          avatarSrc: avatar
        });
        this.resolveAvatarSrc('avatar', avatar);
      })
      .catch(() => {
        // 拉不到不弹错打扰用户，页面保持登录时的值
      });
  },

  // 头像显示地址的兜底解析。
  // 后端域名当前用的是自签证书：<image> 取图走原生下载通道，证书不被信任时会静默失败，
  // 画面上只剩一块底色（上传成功却「看不见头像」）。wx.getImageInfo 走的是开发者工具
  // 「不校验域名与证书」放行的通道，能拿到图，转成本地临时路径再交给 <image>。
  // 证书正常时这次调用只是重复命中缓存、把 src 换成同图，不改变最终显示
  // keepPrev：解析期间先保留当前显示地址（换头像时用），避免中间闪一下空白
  resolveAvatarSrc(field, url, keepPrev) {
    if (!url || !/^https?:\/\//i.test(url)) return;
    wx.getImageInfo({
      src: url,
      success: (r) => {
        // 期间可能又换了头像：地址已变就别拿旧图覆盖新图
        if (!r || !r.path || this.data[field] !== url) return;
        const patch = {};
        patch[field + 'Src'] = r.path;
        this.setData(patch);
      },
      fail: () => {
        // 取不到就退回原地址，让 <image> 按能显示多少显示多少
        if (!keepPrev || this.data[field] !== url) return;
        const patch = {};
        patch[field + 'Src'] = url;
        this.setData(patch);
      }
    });
  },

  /* ===== 编辑资料弹窗 ===== */

  openProfile() {
    this._nicknameBackup = '';
    this.setData({
      editNickname: this.data.nickname,
      // 弹窗沿用卡片已解析好的显示地址，避免同一张图再解析一次
      editAvatar: this.data.avatar,
      editAvatarSrc: this.data.avatarSrc,
      nicknameTip: this.tipFor(this.data.nickname),
      nicknamePicking: false,
      nicknameFocus: false,
      nicknameArmed: false,
      profileVisible: true
    });
    // 卡片的显示地址还没落到这张图上（刚保存完、解析未回）：弹窗自己再解析一次
    if (this.data.avatarSrc !== this.data.avatar) {
      this.resolveAvatarSrc('editAvatar', this.data.avatar);
    }
  },

  /* ===== 昵称输入的两条铁律（都是踩过坑才立的） =====
     1) nicknameFocus 必须镜像「真实焦点」。把它当命令用（置 false 去收键盘）会让
        一个已聚焦的输入框被下发 focus=false，微信随即让它失焦 —— 症状就是
        「删除一个字符键盘就收起」。要收键盘用 wx.hideKeyboard()，它会触发 blur，
        变量自然归位。
     2) type=nickname 只存在于「点了使用微信昵称」到「拿到值」之间。它一聚焦就弹
        微信昵称面板，残留下来会让之后的手动编辑反复被面板打断。 */

  // 手动输入 / 面板回填都走这里：只写值与提示，绝不动 focus 与 type。
  // 多下发一次 focus=false，用户正在用的键盘就会掉一次
  onEditNickname(e) {
    const value = (e && e.detail && e.detail.value) || '';
    const picking = this.data.nicknamePicking;
    this.setData({ editNickname: value, nicknameTip: this.tipFor(value) });
    if (value) this._nicknameBackup = '';
    // 取昵称模式一旦有值（面板选的或手动敲的）就切回普通 text
    if (picking && value) this.exitPicking(value);
  },

  // 退出「取微信昵称」模式：type 切回 text 并收键盘。
  // 这里置 nicknameFocus=false 是安全的：紧接着输入框确实会失焦（hideKeyboard），
  // 变量与真实状态仍然一致
  exitPicking(value) {
    this.setData({ nicknamePicking: false, nicknameFocus: false });
    if (wx.hideKeyboard) wx.hideKeyboard();
    // 防御：个别基础库在 type 从 nickname 切回 text 的瞬间会清掉显示值，
    // 仅当确实被清空时才延迟重写回去，绝不覆盖用户正在做的编辑
    if (value) {
      setTimeout(() => {
        if (!this.data.editNickname) this.setData({ editNickname: value });
      }, 60);
    }
  },

  // 微信昵称面板的「昵称审核」事件：部分基础库版本只通过这个事件回传选中的昵称，
  // 作为 change 的补充兜底，确保无论哪个事件先到都能自动填入
  onNicknameReview(e) {
    const detail = (e && e.detail) || {};
    const value = detail.value || detail.nickname || '';
    if (!value) return;
    this._nicknameBackup = '';
    this.setData({ editNickname: value, nicknameTip: this.tipFor(value) });
    if (this.data.nicknamePicking) this.exitPicking(value);
  },

  onNicknameFocus() {
    // 与真实焦点对齐：用户手动点进输入框时必须置 true。
    // 少了这一句，变量(false)与真实(已聚焦)不一致，之后每次 setData 都会把
    // false 重新下发给原生 input，键盘就被收掉了
    this.setData({ nicknameFocus: true });
    if (this.data.nicknamePicking && !this.data.nicknameArmed) {
      // 面板已关但类型还挂着 nickname（用户没选就关了）：只切回 text，
      // 绝不顺手把 focus 置 false —— 那会把用户刚点开的键盘立刻收掉
      this.setData({ nicknamePicking: false });
    }
    if (this.data.nicknameArmed) {
      // 消费按键触发的聚焦标记，之后任何聚焦都按「用户手动」处理
      this.setData({ nicknameArmed: false });
    }
  },

  // blur 是面板回填的最后一道兜底：面板选完后个别基础库只把值写进原生输入框、
  // 不派发 input，e.detail.value 里带着当前值，在这里补进 data，否则保存仍算空
  onNicknameBlur(e) {
    const value = (e && e.detail && e.detail.value) || '';
    const patch = {};
    // 与真实焦点对齐
    if (this.data.nicknameFocus) patch.nicknameFocus = false;
    if (value && value !== this.data.editNickname) {
      patch.editNickname = value;
      patch.nicknameTip = this.tipFor(value);
      this._nicknameBackup = '';
    } else if (!value && !this.data.editNickname && this._nicknameBackup) {
      // 面板关掉却没带回值：把进面板前的昵称还回去。
      // 否则用户手里只剩空输入框，点保存必然「昵称不能为空」
      patch.editNickname = this._nicknameBackup;
      patch.nicknameTip = this.tipFor(this._nicknameBackup);
      this._nicknameBackup = '';
    }
    // 按键唤起面板的过程中会人为制造一次失焦（见 fillWechatNickname），
    // 那一刻不能把 nickname 模式复位，否则面板永远弹不出来。
    // 其余失焦一律复位：这一次焦点会话结束了，armed 也该跟着作废，
    // 不然下次手动点输入框还会弹面板
    if (!this._nicknamePulsing) {
      if (this.data.nicknameArmed) patch.nicknameArmed = false;
      if (this.data.nicknamePicking) patch.nicknamePicking = false;
    }
    if (Object.keys(patch).length) this.setData(patch);
  },

  // 「使用微信昵称」：弹微信官方的昵称选择面板。
  // 平台不提供读取昵称的接口，只能把输入框切成 nickname 类型并聚焦，由用户本人点选。
  // 清空输入框是因为微信面板只在输入框为空时才弹出；原值先存下来，
  // 面板没选（或值没回来）时由 onNicknameBlur 还原
  fillWechatNickname() {
    this._nicknameBackup = this.data.editNickname;
    const patch = {
      editNickname: '',
      nicknameTip: '',
      nicknamePicking: true,
      // 标记本次聚焦由按键触发，允许唤起微信面板；消费后即视为手动聚焦
      nicknameArmed: true
    };

    if (!this.data.nicknameFocus) {
      // 没聚焦：focus false → true 会真实触发一次聚焦，微信随即弹出昵称面板
      this.setData(Object.assign({ nicknameFocus: true }, patch));
      return;
    }
    // 已经聚焦：focus 值不变，微信不会重新唤起面板（面板只在聚焦发生变化时弹）。
    // 故先收一次焦点、再重新聚焦，制造一次真实的聚焦变化。
    // 这中间那次 blur 由 _nicknamePulsing 挡住，不会把 nickname 模式复位
    this._nicknamePulsing = true;
    this.setData(Object.assign({ nicknameFocus: false }, patch));
    setTimeout(() => {
      this._nicknamePulsing = false;
      this.setData({ nicknameFocus: true });
    }, 80);
  },

  // 弹窗关闭（openProfile / cancelEdit / saveProfile）时也会统一复位 nicknamePicking 与 nicknameFocus

  // 平时不显示 x/20 计数，只有超了才给一句提示。
  // 按字符数算（中文昵称按字符更符合直觉），trim 后判断，纯空格不算超
  tipFor(value) {
    const len = String(value || '').trim().length;
    return len > NICKNAME_MAX ? '昵称不能超过 ' + NICKNAME_MAX + ' 个字符' : '';
  },

  // 用微信头像：open-type="chooseAvatar" 的原生事件，
  // 弹出的面板里就有「使用微信头像」这一项，e.detail.avatarUrl 是选完的临时路径。
  // 临时路径先存着，点「保存」时统一上传到后端换永久 URL
  onChooseWechatAvatar(e) {
    const url = e && e.detail && e.detail.avatarUrl;
    // 本地临时路径 <image> 直接能显示，不需要再过 resolveAvatarSrc
    if (url) this.setData({ editAvatar: url, editAvatarSrc: url });
  },

  cancelEdit() {
    this._nicknameBackup = '';
    this.setData({ profileVisible: false, nicknamePicking: false, nicknameFocus: false, nicknameArmed: false });
  },

  saveProfile() {
    const nickname = String(this.data.editNickname || '').trim();
    if (!nickname) {
      this.toast('昵称不能为空');
      return;
    }
    // 输入框不再硬截断，所以保存前必须自己兜一次，别把超长昵称写进库
    const tip = this.tipFor(nickname);
    if (tip) {
      this.toast(tip);
      return;
    }

    this.saveProfileRemote(nickname, this.data.editAvatar);
  },

  // 头像要么是站内已上传的（绝对地址或相对路径），要么是刚选的临时文件：
  // 只有后者要上传。判断看「是否落在 /uploads/avatars/ 下」，
  // 不能用 http 前缀——安卓 chooseAvatar 的临时路径就是 http://tmp/xxx.jpeg
  saveProfileRemote(nickname, avatarPath) {
    const isStored = !!avatarPath && isStoredAvatar(relativeUrl(avatarPath));
    const ensureAvatar =
      avatarPath && !isStored
        ? uploadAvatar(avatarPath).then((up) => absoluteUrl(up && up.avatar_url))
        : Promise.resolve(avatarPath);

    ensureAvatar
      .then((avatar) =>
        updateProfile({ nickname: nickname, avatar: avatar }).then((res) => ({
          avatar: avatar,
          nickname: (res && res.nickname) || nickname
        }))
      )
      .then((result) => {
        this._nicknameBackup = '';
        this.setData({
          nickname: result.nickname,
          avatar: result.avatar,
          profileVisible: false,
          nicknamePicking: false,
          nicknameFocus: false,
          nicknameArmed: false
        });
        // 新头像解析出来才替换显示地址，期间卡片继续显示旧图，不闪空白
        this.resolveAvatarSrc('avatar', result.avatar, true);
        this.toast('资料已保存');
      })
      .catch((err) => {
        this.toast((err && err.message) || '保存失败');
      });
  },

  // 字体大小：选中即写入缓存并立即生效；其他页面在 onShow 时同步根节点类
  onFontScaleTap(e) {
    const key = (e && e.currentTarget && e.currentTarget.dataset.key) || '';
    if (!key || key === this.data.fontScale) return;
    setFontScale(key);
    this.setData(fontScalePatch());
  },

  // 隐私说明：静态页，只做跳转
  onOpenPrivacy() {
    wx.navigateTo({ url: '/pages/privacy/privacy' });
  },

  /* ===== 退出登录 ===== */

  onLogout() {
    this.setData({ logoutVisible: true });
  },

  onLogoutCancel() {
    this.setData({ logoutVisible: false });
  },

  onLogoutConfirm() {
    // 清掉本地 token / user_id 与登录标记，下次请求会重新走静默登录。
    // 页面复位到默认状态，不跳走：底部「退出登录」按键当场变回「退出登录」
    clearSession();
    this.setData({
      logoutVisible: false,
      nickname: DEFAULT_NICKNAME,
      avatar: '',
      avatarSrc: ''
    });
    this.toast('已退出登录');
  },

  /* ===== 注销账号（合规要求：用户必须能删除自己的账号与数据） ===== */

  onDeleteAccount() {
    if (this.data.deletingAccount) return;
    this.setData({ deleteAccountVisible: true });
  },

  cancelDeleteAccount() {
    if (this.data.deletingAccount) return;
    this.setData({ deleteAccountVisible: false });
  },

  confirmDeleteAccount() {
    if (this.data.deletingAccount) return;
    this.setData({ deleteAccountVisible: false, deletingAccount: true });

    // 顺序不能反：先清会话（Agent 侧数据），再删账号（网关侧数据），
    // 账号一删 openid 就没了，会话接口也跟着登录态一起失效
    clearAllSessions()
      .then(() => deleteAccount())
      .then(() => {
        clearSession();
        this.setData({
          deletingAccount: false,
          nickname: DEFAULT_NICKNAME,
          avatar: '',
          avatarSrc: ''
        });
        this.toast('账号已注销');
        // 回首页：下次提问会自动走静默登录，建一个全新的空账号
        wx.reLaunch({ url: '/pages/chat/chat' });
      })
      .catch((err) => {
        this.setData({ deletingAccount: false });
        this.toast((err && err.message) || '注销失败，请重试');
      });
  }
});
