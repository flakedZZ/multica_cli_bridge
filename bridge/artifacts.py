"""Task-bound image retrieval. Never fetch URLs provided by users or models."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict
from uuid import UUID
from urllib.parse import urlparse
from .manifest import inspect_delivery

import base64
import hashlib
import warnings

from PIL import Image


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 25_000_000
FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
READY = {"in_review", "done"}
TERMINAL = READY | {"cancelled"}


def category(issue: Dict[str, Any]) -> str:
    return issue.get("status_category") or issue.get("status", "")


class Artifacts:
    def __init__(self, service: Any, error: type):
        self.service = service
        self.error = error

    def source(self, root: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        source_id = body.get("source_issue_id") or root["id"]
        try:
            UUID(source_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise self.error("图片来源任务ID无效") from error
        source = root if source_id == root["id"] else self.service.cli.run(["issue", "get", source_id])
        if (not isinstance(source, dict) or source.get("id") != source_id or
                source.get("project_id") != self.service.settings.project_id or
                (source_id != root["id"] and source.get("parent_issue_id") != root["id"])):
            raise self.error("图片仅限原执行任务及其直接子任务", 403)
        return source

    def candidates(self, source: Dict[str, Any], images_only: bool = True) -> list:
        result = []
        for item in source.get("attachments") or []:
            if not isinstance(item, dict) or item.get("issue_id") != source["id"] or not item.get("comment_id"):
                continue
            name = item.get("filename", "")
            size = item.get("size_bytes")
            mime = str(item.get("content_type", "")).split(";", 1)[0].lower()
            if (not isinstance(name, str) or not name or len(name) > 180 or
                    any(c in name for c in '\\/:*?"<>|\x00') or name in {".", ".."} or
                    type(size) is not int or not 0 < size <= MAX_FILE_BYTES):
                continue
            is_image = Path(name).suffix.lower() in SUFFIXES and mime in FORMATS and size <= MAX_IMAGE_BYTES
            if images_only and not is_image:
                continue
            if not images_only and (is_image or mime.startswith("image/") or Path(name).suffix.lower() in SUFFIXES):
                continue
            try:
                UUID(item["id"])
            except (ValueError, TypeError, KeyError):
                continue
            row = {"attachment_id": item["id"], "source_issue_id": source["id"],
                   "filename": name, "size_bytes": size, "content_type": mime}
            url = item.get("markdown_url", "")
            if isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).hostname and not urlparse(url).username:
                row["download_url"] = url
            result.append(row)
        return result

    def handle(self, root: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        source = self.source(root, body)
        ready = category(root) in READY and category(source) in READY
        images = self.candidates(source) if ready else []
        files = self.candidates(source, images_only=False) if ready else []
        if body["operation"] == "artifacts":
            manifest = inspect_delivery(self.service, root, source, images, files, self.error) if ready else {
                "delivery_complete": category(source) == "cancelled", "manifest_required": False, "images": [], "files": []}
            images = manifest.pop("images")
            files = manifest.pop("files")
            children = []
            truncated = False
            if source["id"] == root["id"] and category(root) in READY:
                tree = self.service.cli.run(["issue", "children", root["id"]])
                if not isinstance(tree, dict):
                    raise self.error("CLI子任务格式不兼容", 502)
                rows = list(tree.get("unstaged") or [])
                for stage in tree.get("stages") or []:
                    if not isinstance(stage, dict) or not isinstance(stage.get("issues"), list):
                        raise self.error("CLI阶段子任务格式不兼容，请将图片汇总到主任务", 502)
                    rows.extend(stage["issues"])
                children = list(dict.fromkeys(r["id"] for r in rows if isinstance(r, dict) and r.get("id")))
                truncated = len(children) > 16
                children = children[:16]
            return {"text": f"任务状态：{source.get('status', '')}；可回传图片：{len(images)}",
                    "ready": ready, "terminal": category(source) in TERMINAL, "status": source.get("status"),
                    "revision": source.get("revision"), "status_category": category(source),
                    **manifest,
                    "images": images[:16], "files": files[:16], "children": children,
                    "truncated": truncated or len(images) + len(files) > 16}
        if not ready:
            raise self.error("原任务及图片来源任务需先交付待验收或完成", 409)
        candidates = files if body["operation"] in {"verify_file", "file"} else images
        item = next((i for i in candidates if i["attachment_id"] == body.get("attachment_id")), None)
        if item is None:
            raise self.error("未找到属于本任务的有效图片评论附件", 403)
        with TemporaryDirectory(prefix="image-", dir=self.service.settings.state_dir) as folder:
            destination = Path(folder).resolve()
            downloaded = self.service.cli.download(item["attachment_id"], destination)
            path = Path(downloaded)
            if path.is_symlink() or path.resolve().parent != destination or not path.is_file():
                raise self.error("CLI下载路径不在本次隔离目录内", 502)
            if path.stat().st_size != item["size_bytes"] or path.stat().st_size > MAX_FILE_BYTES:
                raise self.error("图片实际大小与附件记录不一致", 502)
            data = path.read_bytes()
            if body["operation"] in {"verify_file", "file"}:
                return {"text": "附件下载校验完成", **item, "verified": True, "sha256": hashlib.sha256(data).hexdigest(),
                        **({"file_base64": base64.b64encode(data).decode("ascii")} if body["operation"] == "file" else {})}
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as picture:
                        if (picture.format != FORMATS[item["content_type"]] or
                                picture.width * picture.height > MAX_PIXELS or
                                getattr(picture, "n_frames", 1) != 1):
                            raise ValueError("unsupported image or dimensions")
                        width, height = picture.size
                        picture.verify()
                    with Image.open(BytesIO(data)) as picture:
                        picture.load()
            except Exception as error:
                raise self.error("图片无法安全解码，仅支持尺寸受限的静态PNG/JPEG/WebP", 422) from error
        return {"text": "图片下载校验完成", **item, "width": width, "height": height,
                "sha256": hashlib.sha256(data).hexdigest(), "image_base64": base64.b64encode(data).decode("ascii")}
