"""Shared request validation, including module-entrypoint error identity."""
from typing import Any, Dict


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def field(data: Dict[str, Any], key: str, maximum: int = 200) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise BridgeError(f"参数 {key} 缺失或长度无效")
    return value.strip()
