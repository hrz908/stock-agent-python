// 全局字号档位：设置页写入本地缓存，各页把对应 class 挂到根节点 .page 上，
// token.wxss 中的字号变量在该 class 下整体平移（custom property 会继承进子组件）。
//
// 只有三档（2026-10-26 用户决策：超小 / 小两档连同 caption 档一起删除）：
//   标准 / 大 / 超大，相邻档位差 4rpx，正文 30 / 34 / 38，最小字是小号 26。
// token.wxss 里已没有 xsmall / small 两个档位类；
// 旧缓存落在已删除的档位时按「非法值」回退标准。
// 相对字级跟着档位走：小号 --fs-sm = 正文 -4rpx、大号 --fs-lg = 正文 +4rpx、
// 超大号 --fs-xl = 正文 +8rpx。
const STORAGE_KEY = 'font_scale';

export const FONT_SCALE_OPTIONS = [
  { key: 'standard', label: '标准', className: '' },
  { key: 'large', label: '大', className: 'font-scale--large' },
  { key: 'xlarge', label: '超大', className: 'font-scale--xlarge' }
];

const DEFAULT_KEY = 'standard';

// 非法值（旧缓存 / 手改缓存）一律退回标准，避免根节点挂上不存在的类
function findOption(key) {
  return (
    FONT_SCALE_OPTIONS.find((item) => item.key === key) ||
    FONT_SCALE_OPTIONS.find((item) => item.key === DEFAULT_KEY)
  );
}

export function getFontScale() {
  try {
    return findOption(wx.getStorageSync(STORAGE_KEY)).key;
  } catch (e) {
    return DEFAULT_KEY;
  }
}

export function setFontScale(key) {
  const value = findOption(key).key;
  try {
    wx.setStorageSync(STORAGE_KEY, value);
  } catch (e) {
    // 存储失败时本次选择仍生效，下次启动回退标准
  }
  return value;
}

// 页面 onShow 直接 setData 的补丁：fontScale 供设置页回显，fontScaleClass 供根节点
export function fontScalePatch() {
  const option = findOption(getFontScale());
  return { fontScale: option.key, fontScaleClass: option.className };
}
