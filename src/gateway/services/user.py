"""用户资料业务：查询、修改、头像、注销。

user_id 只从鉴权依赖注入，不信任请求体里的任何身份字段。
头像这一块的安全边界是踩过坑的：
- 库里的 avatar 只能是本站 /uploads/avatars/ 下、且文件名带自己 uid 的路径，
  否则用户能把别人的头像挂到自己名下；
- 扩展名不看客户端给的，真实类型由文件头判定，防 HTML/脚本被存进公开静态目录；
- 「先写库成功，再删旧文件」：先删后写一旦写失败，用户头像就没了。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import time
from typing import Any

from gateway.infra.deps import config
from gateway.infra.resp import CODE_BAD_REQUEST, CODE_INTERNAL, CODE_NOT_FOUND, ApiError
from gateway.repositories import users as repo

logger = logging.getLogger("stocka.gateway.user")

NICKNAME_MAX = 10
AVATAR_MAX_BYTES = (2 << 20) + 1024
AVATAR_PREFIX = "/uploads/avatars/"
_ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# base64 膨胀 4/3 再留一点头尾（filename / JSON 括号），供读请求体时先粗挡一次
AVATAR_BASE64_MAX_BYTES = (AVATAR_MAX_BYTES + 2) // 3 * 4 + 2048
# base64 里的空白（换行）不算数据，解码前先抹掉；其余的非法字符交给 validate 拒绝
_WS_RE = re.compile(r"\s+")


def _trim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _trim_runes(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def is_safe_avatar(path: str) -> bool:
    if not path.startswith(AVATAR_PREFIX):
        return False
    if ".." in path or "\\" in path:
        return False
    name = path[len(AVATAR_PREFIX) :]
    return bool(name) and "/" not in name


def _avatar_file(name: str):
    return config().upload_path / "avatars" / name


def _remove_avatar_file(rel: str) -> None:
    """尽力而为：失败只记日志，不影响主流程。"""
    if not is_safe_avatar(rel):
        return
    name = rel[len(AVATAR_PREFIX) :]
    try:
        _avatar_file(name).unlink(missing_ok=True)
    except OSError as exc:
        logger.info("remove avatar file failed: %s err=%s", rel, exc)


def _detect_image(head: bytes) -> bool:
    """按文件头判断真实类型，只放行三种位图。"""
    if head.startswith(b"\xff\xd8\xff"):
        return True
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"


async def current_user(user_id: int) -> dict[str, Any]:
    try:
        user = await repo.find_by_id(user_id)
    except Exception as exc:
        logger.exception("load user failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "查询失败") from exc

    if user is None:
        raise ApiError(CODE_NOT_FOUND, "用户不存在，请重新登录")
    return user


async def get_profile(user_id: int) -> dict[str, Any]:
    user = await current_user(user_id)
    return {"nickname": user["nickname"], "avatar": user["avatar"]}


async def update_profile(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    user = await current_user(user_id)

    nickname = _trim(payload.get("nickname"))
    if not nickname:
        raise ApiError(CODE_BAD_REQUEST, "昵称不能为空")
    # 超长是「截断」不是报错：按字符数算，中文一个字算一个
    nickname = _trim_runes(nickname, NICKNAME_MAX)

    avatar = user["avatar"]
    raw_avatar = _trim(payload.get("avatar"))
    if raw_avatar:
        # 只能引用自己上传的文件：文件名带 user_id
        basename = raw_avatar[len(AVATAR_PREFIX) :] if raw_avatar.startswith(AVATAR_PREFIX) else ""
        if not is_safe_avatar(raw_avatar) or not basename.startswith(f"avatar_{user['id']}_"):
            raise ApiError(CODE_BAD_REQUEST, "头像地址不合法")
        avatar = raw_avatar

    try:
        await repo.update_profile(user["id"], nickname, avatar)
    except Exception as exc:
        logger.exception("update profile failed: user=%s", user["id"])
        raise ApiError(CODE_INTERNAL, "保存失败") from exc

    # 换头像成功后再删旧文件：先删后写一旦写失败，用户就丢了自己的头像
    if avatar != user["avatar"] and user["avatar"]:
        _remove_avatar_file(user["avatar"])

    return {"nickname": nickname, "avatar": avatar}


def decode_avatar_payload(payload: dict[str, Any]) -> tuple[str, bytes]:
    """解析 `{filename?, data}` 的 base64 头像载荷，返回 (文件名, 字节)。

    这条路是给「wx.uploadFile 发不出去」的环境兜底用的：uploadFile 在微信后台有
    独立的合法域名列表（和 request 分开配），没配时小程序连请求都发不出来，
    用户只能看到「头像上传失败」；而 request 那条域名通常是通的。

    校验口径与 multipart 通道完全一致：体积上限 + 文件头判类型（在 save_avatar 里）。
    """
    raw = payload.get("data")
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(CODE_BAD_REQUEST, "缺少头像文件")
    # 允许 data URL 前缀（data:image/png;base64,xxxx）与其中的换行
    text = _WS_RE.sub("", raw)
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    if not text:
        raise ApiError(CODE_BAD_REQUEST, "缺少头像文件")
    try:
        content = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(CODE_BAD_REQUEST, "头像数据格式不正确") from exc
    if not content:
        raise ApiError(CODE_BAD_REQUEST, "缺少头像文件")
    if len(content) > AVATAR_MAX_BYTES:
        raise ApiError(CODE_BAD_REQUEST, "头像不能超过 2MB")
    filename = payload.get("filename")
    return (filename if isinstance(filename, str) else ""), content


def _pick_ext(filename: str) -> str:
    """只保留认识的扩展名，其余一律按 png 存，避免可疑后缀写进磁盘。"""
    lower = (filename or "").lower()
    for candidate in _ALLOWED_EXTS:
        if lower.endswith(candidate):
            return candidate
    return ".png"


async def save_avatar(user_id: int, filename: str, content: bytes) -> dict[str, Any]:
    # 扩展名可以随便改，真正的类型看文件头
    if not _detect_image(content[:512]):
        raise ApiError(CODE_BAD_REQUEST, "头像必须是 JPG/PNG/WebP 图片")

    name = f"avatar_{user_id}_{time.time_ns()}{_pick_ext(filename)}"
    target = _avatar_file(name)
    try:
        target.write_bytes(content)
    except OSError as exc:
        logger.exception("save avatar failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "头像保存失败") from exc

    rel = AVATAR_PREFIX + name

    # 先记下旧头像：写库成功后再删，避免删了旧文件、新路径又没写进去
    old_avatar = ""
    try:
        old_avatar = await repo.load_avatar(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("load old avatar failed: user=%s err=%s", user_id, exc)

    try:
        await repo.update_avatar(user_id, rel)
    except Exception as exc:
        logger.exception("update avatar failed: user=%s", user_id)
        # 回滚刚落盘的文件，别在磁盘上留一个没人引用的孤儿
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.info("rollback avatar file failed: %s", name)
        raise ApiError(CODE_INTERNAL, "头像保存失败") from exc

    if old_avatar and old_avatar != rel:
        _remove_avatar_file(old_avatar)

    return {"avatar_url": rel}


async def delete_account(user_id: int) -> dict[str, Any]:
    user = await current_user(user_id)

    # 硬删当前用户及其业务数据。对话会话与 checkpoint 由 agent 侧持有，
    # 小程序会在调用本接口前先逐条 DELETE /agent/sessions/:sid。
    try:
        await repo.purge_account(user["id"])
    except Exception as exc:
        logger.exception("delete account failed: user=%s", user["id"])
        raise ApiError(CODE_INTERNAL, "注销失败") from exc

    # 库删成功后再删文件：先删文件、事务又回滚的话，用户头像没了但账号还在
    _remove_avatar_file(user["avatar"])

    return {"deleted": True}
