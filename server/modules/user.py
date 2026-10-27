"""用户资料：查询、修改、头像上传、注销。

user_id 只从鉴权依赖注入，不信任请求体里的任何身份字段。
头像这一块的安全边界是踩过坑的：
- 库里的 avatar 只能是本站 /uploads/avatars/ 下、且文件名带自己 uid 的路径，
  否则用户能把别人的头像挂到自己名下；
- 扩展名不看客户端给的，真实类型由文件头判定，防 HTML/脚本被存进公开静态目录；
- 「先写库成功，再删旧文件」：先删后写一旦写失败，用户头像就没了。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile

from server.deps import config, runtime
from server.resp import (
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    ApiError,
    ok_response,
)
from server.security.auth import CurrentUser
from server.web import read_json

logger = logging.getLogger("stocka.server.user")

router = APIRouter(prefix="/user", tags=["user"])

NICKNAME_MAX = 10
AVATAR_MAX_BYTES = (2 << 20) + 1024
AVATAR_PREFIX = "/uploads/avatars/"
_ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _trim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _trim_runes(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _is_safe_avatar(path: str) -> bool:
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
    if not _is_safe_avatar(rel):
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


async def _current_user(user_id: int) -> dict[str, Any]:
    try:
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT id, openid, nickname, avatar FROM users WHERE id = %s",
                (user_id,),
            )
            row = await cursor.fetchone()
    except Exception as exc:
        logger.exception("load user failed: user=%s", user_id)
        raise ApiError(CODE_INTERNAL, "查询失败") from exc

    if row is None:
        raise ApiError(CODE_NOT_FOUND, "用户不存在，请重新登录")
    return {
        "id": int(row[0]),
        "openid": row[1] or "",
        "nickname": row[2] or "",
        "avatar": row[3] or "",
    }


@router.get("/profile")
async def get_profile(user_id: CurrentUser):
    user = await _current_user(user_id)
    return ok_response({"nickname": user["nickname"], "avatar": user["avatar"]})


@router.put("/profile")
async def update_profile(request: Request, user_id: CurrentUser):
    user = await _current_user(user_id)
    payload = await read_json(request, 0, "参数错误")

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
        if not _is_safe_avatar(raw_avatar) or not basename.startswith(
            f"avatar_{user['id']}_"
        ):
            raise ApiError(CODE_BAD_REQUEST, "头像地址不合法")
        avatar = raw_avatar

    try:
        async with runtime().pg.pool.connection() as conn:
            await conn.execute(
                "UPDATE users SET nickname = %s, avatar = %s, updated_at = now() WHERE id = %s",
                (nickname, avatar, user["id"]),
            )
    except Exception as exc:
        logger.exception("update profile failed: user=%s", user["id"])
        raise ApiError(CODE_INTERNAL, "保存失败") from exc

    # 换头像成功后再删旧文件：先删后写一旦写失败，用户就丢了自己的头像
    if avatar != user["avatar"] and user["avatar"]:
        _remove_avatar_file(user["avatar"])

    return ok_response({"nickname": nickname, "avatar": avatar})


@router.post("/avatar")
async def upload_avatar(
    user_id: CurrentUser,
    file: UploadFile = File(default=None),  # noqa: B008 - FastAPI 的 multipart 声明惯例
):
    if file is None or not file.filename:
        raise ApiError(CODE_BAD_REQUEST, "缺少头像文件")

    content = await file.read(AVATAR_MAX_BYTES + 1)
    if len(content) > AVATAR_MAX_BYTES:
        raise ApiError(CODE_BAD_REQUEST, "头像不能超过 2MB")

    # 只保留认识的扩展名，其余一律按 png 存，避免可疑后缀写进磁盘
    filename = (file.filename or "").lower()
    ext = ""
    for candidate in _ALLOWED_EXTS:
        if filename.endswith(candidate):
            ext = candidate
            break
    if not ext:
        ext = ".png"

    # 扩展名可以随便改，真正的类型看文件头
    if not _detect_image(content[:512]):
        raise ApiError(CODE_BAD_REQUEST, "头像必须是 JPG/PNG/WebP 图片")

    name = f"avatar_{user_id}_{time.time_ns()}{ext}"
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
        async with runtime().pg.pool.connection() as conn:
            cursor = await conn.execute("SELECT avatar FROM users WHERE id = %s", (user_id,))
            row = await cursor.fetchone()
            old_avatar = (row[0] if row else "") or ""
    except Exception as exc:  # noqa: BLE001
        logger.info("load old avatar failed: user=%s err=%s", user_id, exc)

    try:
        async with runtime().pg.pool.connection() as conn:
            await conn.execute(
                "UPDATE users SET avatar = %s, updated_at = now() WHERE id = %s",
                (rel, user_id),
            )
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

    return ok_response({"avatar_url": rel})


@router.delete("/account")
async def delete_account(user_id: CurrentUser):
    user = await _current_user(user_id)

    # 硬删当前用户及其业务数据。对话会话与 checkpoint 由 agent 侧持有，
    # 小程序会在调用本接口前先逐条 DELETE /agent/sessions/:sid。
    try:
        async with runtime().pg.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM alert_rule WHERE user_id = %s", (user["id"],))
            await conn.execute(
                "DELETE FROM user_watchlist WHERE user_id = %s", (user["id"],)
            )
            await conn.execute("DELETE FROM users WHERE id = %s", (user["id"],))
    except Exception as exc:
        logger.exception("delete account failed: user=%s", user["id"])
        raise ApiError(CODE_INTERNAL, "注销失败") from exc

    # 库删成功后再删文件：先删文件、事务又回滚的话，用户头像没了但账号还在
    _remove_avatar_file(user["avatar"])

    return ok_response({"deleted": True})
