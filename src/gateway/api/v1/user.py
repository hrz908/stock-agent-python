"""用户资料 HTTP 接口：查询 / 修改 / 头像上传 / 注销。"""

from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile

from gateway.infra.resp import CODE_BAD_REQUEST, ApiError, ok_response
from gateway.infra.web import read_json
from gateway.security.auth import CurrentUser
from gateway.services import user as service

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/profile")
async def get_profile(user_id: CurrentUser):
    return ok_response(await service.get_profile(user_id))


@router.put("/profile")
async def update_profile(request: Request, user_id: CurrentUser):
    payload = await read_json(request, 0, "参数错误")
    return ok_response(await service.update_profile(user_id, payload))


@router.post("/avatar")
async def upload_avatar(
    user_id: CurrentUser,
    file: UploadFile = File(default=None),  # noqa: B008 - FastAPI 的 multipart 声明惯例
):
    if file is None or not file.filename:
        raise ApiError(CODE_BAD_REQUEST, "缺少头像文件")

    # 多读 1 字节用于判断是否超限，避免把超大文件整体读进内存
    content = await file.read(service.AVATAR_MAX_BYTES + 1)
    if len(content) > service.AVATAR_MAX_BYTES:
        raise ApiError(CODE_BAD_REQUEST, "头像不能超过 2MB")

    return ok_response(await service.save_avatar(user_id, file.filename, content))


@router.post("/avatar/base64")
async def upload_avatar_base64(request: Request, user_id: CurrentUser):
    """base64 通道：给 wx.uploadFile 被「合法域名」挡住的小程序兜底。

    multipart 那条（POST /user/avatar）继续保留，开发者工具与已配好 uploadFile
    域名的环境仍走它；两条通道共用同一套校验与落盘逻辑。
    """
    payload = await read_json(request, service.AVATAR_BASE64_MAX_BYTES, "头像参数错误")
    filename, content = service.decode_avatar_payload(payload)
    return ok_response(await service.save_avatar(user_id, filename, content))


@router.delete("/account")
async def delete_account(user_id: CurrentUser):
    return ok_response(await service.delete_account(user_id))
