"""Bounded incoming bytes; never interpret a user-provided local path or URL."""
import base64
import hashlib


MAX_TOTAL = 32 * 1024 * 1024
MAX_REQUEST = 46 * 1024 * 1024


def decode_attachments(items, error):
    if not isinstance(items, list) or len(items) > 5:
        raise error("一次最多上传5个附件")
    output = []
    total = 0
    names = set()
    for item in items:
        if not isinstance(item, dict):
            raise error("附件格式无效")
        name = item.get("filename")
        encoded = item.get("base64")
        if (not isinstance(name, str) or not name or len(name) > 150 or name in {".", "..", "body.md"} or
                any(c in name for c in '\\/:*?"<>|\x00') or any(ord(c) < 32 for c in name) or
                name.rstrip(" .") != name or name.casefold() in names):
            raise error("附件文件名无效或重复")
        stem = name.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
            raise error("附件文件名不可用")
        if not isinstance(encoded, str) or len(encoded) > MAX_REQUEST:
            raise error("附件大小无效")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as problem:
            raise error("附件编码无效") from problem
        total += len(data)
        if not data or total > MAX_TOTAL or len(data) != item.get("size_bytes") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise error("附件大小或校验和不匹配")
        names.add(name.casefold())
        output.append((name, data))
    return output
