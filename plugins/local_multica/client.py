from pathlib import Path
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import json
import time


HELP = """Multica 任务助手
直接告诉QQ助手需求，例如：帮我画一张猫咪头像。
复杂任务由QQ助手自动安排执行、审核方案，完成后图片会回到原会话，无需提到规划Agent或Multica。
也可用以下命令查看任务：
/multica 列表
/multica 状态 DEMO-123
/multica 结果 DEMO-123
旧的直接提交命令默认关闭，执行结果也支持自然语言查询。
仅允许配置的QQ号使用，仅可查询本人在当前会话提交的任务。群聊中的回复对群成员可见。"""


class ClientError(Exception):
    pass


class BridgeBusyError(ClientError):
    """The bridge explicitly rejected this request before executing it."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise ClientError("桥接地址发生重定向，已拒绝发送密钥")


def parse(body: str) -> Dict[str, str]:
    parts = body.strip().split(maxsplit=1)
    action = parts[0].lower() if parts else "帮助"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if action in {"帮助", "help"}:
        return {"operation": "help"}
    if action in {"提交", "submit"}:
        title, separator, description = rest.partition("|")
        if not title.strip() or not separator or not description.strip():
            raise ClientError("用法：/multica 提交 标题 | 详细需求")
        return {"operation": "submit", "title": title.strip(), "description": description.strip()}
    if action in {"列表", "list"} and not rest:
        return {"operation": "list"}
    if action in {"状态", "status", "结果", "result"} and rest and len(rest.split()) == 1:
        return {"operation": "status" if action in {"状态", "status"} else "result", "reference": rest}
    raise ClientError("命令格式无效，发送 /multica 帮助 查看用法")


def request(url: str, token_file: str, body: Dict[str, Any]) -> Dict[str, Any]:
    for attempt in range(4):
        try:
            return _request_once(url, token_file, body)
        except BridgeBusyError:
            if attempt == 3:
                raise
            # Retry only the typed pre-execution rejection, never an ambiguous
            # timeout, CLI error, rate limit, send failure, or unknown response.
            time.sleep(0.5 * (2 ** attempt))


def _request_once(url: str, token_file: str, body: Dict[str, Any]) -> Dict[str, Any]:
    address = urlsplit(url)
    if address.scheme not in {"http", "https"} or address.username or address.password or address.query or address.fragment:
        raise ClientError("桥接URL无效")
    if address.scheme == "http" and address.hostname not in {"host.docker.internal", "127.0.0.1", "localhost"}:
        raise ClientError("HTTP仅允许本机桥接地址，远程连接必须使用HTTPS")
    try:
        token = Path(token_file).read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise ClientError("桥接密钥长度无效")
        req = Request(url.rstrip("/") + "/v1/request", data=json.dumps(body, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            timeout = 150 if body.get("attachments") else 600 if body.get("operation") in {"delivery_prepare", "delivery_ack"} else 60 if body.get("operation") in {"image", "file", "verify_file", "close"} else 35
            response = opener.open(req, timeout=timeout)
        except HTTPError as error:
            response = error
        maximum = (44 * 1024 * 1024 if body.get("operation") == "file" else
                   12 * 1024 * 1024 if body.get("operation") == "image" else
                   2 * 1024 * 1024 if body.get("operation") == "delivery_prepare" else 65536)
        with response:
            status = response.status
            payload = response.read(maximum + 1)
        if len(payload) > maximum:
            raise ClientError("桥接响应过大")
        result = json.loads(payload)
        if status == 429 and isinstance(result, dict) and result.get("code") == "bridge_busy" and result.get("ok") is False:
            raise BridgeBusyError("桥接忙，已延后重试；本次请求未执行")
        if not isinstance(result, dict) or not result.get("ok"):
            raise ClientError(result.get("error", "桥接返回错误") if isinstance(result, dict) else "桥接响应格式错误")
        if not isinstance(result.get("text"), str):
            raise ClientError("桥接响应缺少正文")
        return result
    except (OSError, URLError, ValueError) as error:
        raise ClientError("桥接连接或配置错误；提交请求可能已接收，请先查 列表，勿重新提交") from error
