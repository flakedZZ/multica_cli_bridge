"""Read only user-selected, host-authenticated QQ message attachments."""
import asyncio
import base64
import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from .client import ClientError


MAX_TOTAL = 32 * 1024 * 1024
MAX_COUNT = 5


def safe_name(name, index):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")[:120]
    # A stable numeric prefix prevents collisions and reserved Windows basenames.
    return f"{index:02d}-" + (name or "attachment.bin")


def image_suffix(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise ClientError("图片格式无法识别，请以PNG/JPEG/WebP/GIF重新发送")


async def collect(plugin, payload, message_ids):
    if not isinstance(message_ids, list) or len(message_ids) > MAX_COUNT or any(not isinstance(m, str) or not m for m in message_ids):
        raise ClientError("最多引用5条附件消息")
    result = []
    total = 0
    for message_id in dict.fromkeys([payload["message_id"], *message_ids]):
        # Never reuse raw model-provided URLs, paths or file_refs.
        await plugin.origin(payload["session_id"], message_id, False, payload["user_id"])
        response = await plugin.ctx.message.get_by_id(message_id, stream_id=payload["session_id"], include_binary_data=True)
        row = response.get("message") if isinstance(response, dict) and "message" in response else response
        if not isinstance(row, dict) or row.get("session_id") != payload["session_id"] or row.get("platform") != "qq" or str(row.get("message_info", {}).get("user_info", {}).get("user_id")) != payload["user_id"]:
            raise ClientError("附件消息归属不一致")
        segments = row.get("raw_message") or []
        if not isinstance(segments, list):
            raise ClientError("宿主附件消息格式不兼容")
        for segment in segments:
            if segment.get("type") not in {"image", "file"}:
                continue
            if len(result) >= MAX_COUNT:
                raise ClientError("一次讨论最多上传5个附件")
            if segment["type"] == "image":
                encoded = segment.get("binary_data_base64")
                if not isinstance(encoded, str) or not encoded or len(encoded) > 12 * 1024 * 1024:
                    raise ClientError("原消息图片不可读取或超过8MiB，请重新发送")
                try:
                    data = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
                except ValueError as error:
                    raise ClientError("图片数据无效") from error
                if len(data) > 8 * 1024 * 1024:
                    raise ClientError("单张输入图片不可超过8MiB")
                name = "qq-image" + image_suffix(data)
            else:
                meta = segment.get("data")
                if not isinstance(meta, dict) or not meta.get("file_ref"):
                    raise ClientError("宿主没有提供可读取的文件引用，请重新发送文件")
                try:
                    declared = int(meta.get("size") or 0)
                except (TypeError, ValueError) as error:
                    raise ClientError("文件大小无效") from error
                if declared > MAX_TOTAL:
                    raise ClientError("单文件不可超过32MiB")
                with TemporaryDirectory(prefix="input-", dir=plugin.ctx.paths.data_dir) as folder:
                    path = Path(folder) / "incoming.bin"
                    try:
                        await plugin.ctx.files.download(meta["file_ref"], path)
                    except Exception as error:
                        raise ClientError("QQ文件下载失败，请检查文件是否仍可下载后重试") from error
                    if path.stat().st_size > MAX_TOTAL:
                        raise ClientError("单文件不可超过32MiB")
                    data = await asyncio.to_thread(path.read_bytes)
                name = meta.get("name") or "qq-file.bin"
            total += len(data)
            if not data or total > MAX_TOTAL:
                raise ClientError("附件不能为空，且一次上传总大小不可超过32MiB")
            result.append({"filename": safe_name(name, len(result) + 1), "size_bytes": len(data),
                           "sha256": hashlib.sha256(data).hexdigest(), "base64": base64.b64encode(data).decode("ascii")})
    return result
