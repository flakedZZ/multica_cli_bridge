"""Small authenticated HTTP bridge; all Multica operations go through the CLI."""

from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List
from uuid import UUID

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time

from .dialogue import Dialogue
from .artifacts import Artifacts
from .closure import CompletionProtocol
from .uploads import MAX_REQUEST
from .validation import BridgeError, field


@dataclass
class Settings:
    cli_path: str
    workspace_id: str
    project_id: str
    assignee_id: str
    allowed_users: List[str]
    token: str
    state_dir: Path
    profile: str = ""
    cli_timeout: int = 8
    max_submissions_per_hour: int = 5
    mika_id: str = ""
    max_discussions_per_hour: int = 10
    allow_direct_submit: bool = False

    def __post_init__(self) -> None:
        # An omitted coordinator uses the explicitly configured agent, never
        # an author-specific or example account.
        self.mika_id = self.mika_id or self.assignee_id
        for value in (self.workspace_id, self.project_id, self.assignee_id, self.mika_id):
            UUID(value)
        if len(self.token) < 32 or not self.token.isascii():
            raise ValueError("桥接密钥必须是至少32位的ASCII随机字符串")
        if not isinstance(self.allowed_users, list) or any(
            not isinstance(user, str) or not user.isdecimal() for user in self.allowed_users
        ):
            raise ValueError("allowed_users 必须为 QQ 号字符串列表")
        if not 1 <= self.cli_timeout <= 10 or self.max_submissions_per_hour < 1:
            raise ValueError("CLI超时应为1~10秒，提交限额必须大于0")
        if not Path(self.cli_path).is_absolute() or not Path(self.cli_path).is_file():
            raise ValueError("cli_path 必须是实际 Multica CLI 可执行文件的绝对路径")
        if self.max_discussions_per_hour < 1 or type(self.allow_direct_submit) is not bool:
            raise ValueError("讨论限额和直接提交开关无效")


class Cli:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, args: List[str]) -> Any:
        s = self.settings
        command = [s.cli_path, "--workspace-id", s.workspace_id]
        if s.profile:
            command += ["--profile", s.profile]
        command += args + ["--output", "json"]
        try:
            result = subprocess.run(
                command, cwd=s.state_dir, shell=False, capture_output=True,
                encoding="utf-8", errors="strict", timeout=120 if "--attachment" in args else s.cli_timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
            raise BridgeError("Multica CLI不可用或超时；写操作结果可能未确认，请勿重复提交", 502) from error
        if result.returncode:
            # stderr can contain local paths or sensitive diagnostics. Never send it to QQ.
            raise BridgeError(f"Multica CLI返回错误（退出码{result.returncode}），请管理员检查登录和权限", 502)
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise BridgeError("Multica CLI未返回有效JSON；写操作结果可能未确认", 502) from error

    def download(self, attachment_id: str, destination: Path) -> Path:
        UUID(attachment_id)
        s = self.settings
        command = [s.cli_path, "--workspace-id", s.workspace_id]
        if s.profile:
            command += ["--profile", s.profile]
        # attachment download has no --output flag; stdout is JSON, stderr is a confirmation.
        command += ["attachment", "download", attachment_id, "--output-dir", str(destination)]
        try:
            result = subprocess.run(command, cwd=s.state_dir, shell=False, capture_output=True,
                                    encoding="utf-8", errors="strict", timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ValueError("CLI download failed")
            return Path(json.loads(result.stdout)["path"])
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise BridgeError("附件CLI下载失败或超时", 502) from error


class Service:
    def __init__(self, settings: Settings, cli: Any = None):
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = settings.state_dir / "tasks.sqlite3"
        self.cli = cli or Cli(settings)
        # One request at a time bounds CLI concurrency and request duration.
        self.lock = threading.Lock()
        self.active_request = None
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                title TEXT NOT NULL, created REAL NOT NULL,
                issue_id TEXT, identifier TEXT, state TEXT NOT NULL)""")
        self.dialogue = Dialogue(self, BridgeError)
        self.artifacts = Artifacts(self, BridgeError)
        self.completion = CompletionProtocol(self, BridgeError)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def handle(self, body: Dict[str, Any]) -> Dict[str, Any]:
        user = field(body, "user_id")
        session = field(body, "session_id")
        if body.get("platform") != "qq" or user not in self.settings.allowed_users:
            raise BridgeError("当前QQ号未获准使用Multica", 403)
        operation = field(body, "operation", 20)
        if operation not in {"delivery_prepare", "delivery_ack", "file", "verify_file", "close", "health", "artifacts", "image", "submit", "list", "status", "result", "pending", "discuss", "plan", "confirm_plan", "cancel_plan"}:
            raise BridgeError("不支持此操作")
        if not self.lock.acquire(blocking=False):
            active = self.active_request
            print(json.dumps({"event": "bridge_busy", "operation": operation,
                              "active_operation": active[0] if active else None,
                              "active_seconds": round(time.monotonic() - active[1], 3) if active else None}), flush=True)
            raise BridgeError("桥接正在处理请求，请稍后重试原命令", 429, "bridge_busy")
        started = time.monotonic()
        self.active_request = (operation, started)
        try:
            if operation == "health":
                project = self.cli.run(["project", "get", self.settings.project_id])
                if not isinstance(project, dict) or project.get("id") != self.settings.project_id:
                    raise BridgeError("CLI项目访问校验失败", 502)
                return {"text": "桥接及持久CLI项目访问正常", "cli_ready": True}
            if operation in {"pending", "discuss", "plan", "confirm_plan", "cancel_plan"}:
                return self.dialogue.handle(body, user, session)
            if operation == "submit":
                if not self.settings.allow_direct_submit:
                    raise BridgeError("直接提交已关闭，请先与规划Agent讨论并由QQbot确认方案", 403)
                return self.submit(body, user, session)
            if operation == "list":
                with self.connect() as db:
                    rows = db.execute(
                        "SELECT identifier, title, state, request_key FROM requests "
                        "WHERE user_id=? AND session_id=? ORDER BY created DESC LIMIT 10", (user, session)
                    ).fetchall()
                lines = [f"{r['identifier'] or r['request_key'][:12]} | {r['title']} | {r['state']}" for r in rows]
                return {"text": "本会话最近提交记录（实时进度请用 状态 命令）：\n" + ("\n".join(lines) or "暂无")}
            reference = field(body, "reference")
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM requests WHERE user_id=? AND session_id=? "
                    "AND (identifier=? OR issue_id=?) AND state='submitted'",
                    (user, session, reference.upper(), reference),
                ).fetchone()
            if row is None:
                raise BridgeError("只能查询本人在当前会话通过本插件提交的任务", 403)
            issue = self.cli.run(["issue", "get", row["issue_id"]])
            if not isinstance(issue, dict) or issue.get("project_id") != self.settings.project_id:
                raise BridgeError("任务已不属于配置的项目", 403)
            if operation in {"artifacts", "image", "verify_file", "file"}:
                result = self.artifacts.handle(issue, body)
                if operation == "artifacts":
                    result["closure_protocol"] = 1 if self.completion.link(row) else 0
                return result
            if operation == "close":
                raise BridgeError("旧确认/静默关闭接口已停用；必须提供完整交付回执", 409)
            if operation == "delivery_prepare":
                return self.completion.prepare(issue, row)
            if operation == "delivery_ack":
                return self.completion.acknowledge(issue, row, body)
            header = f"{row['identifier']} | {issue.get('title', '')}\n状态：{issue.get('status_name') or issue.get('status', '')}"
            if operation == "status":
                return {"text": header}
            roots = self.cli.run(["issue", "comment", "list", row["issue_id"], "--roots-only", "--summary", "--compact"])
            if not isinstance(roots, list):
                raise BridgeError("CLI评论格式不兼容", 502)
            if not roots:
                return {"text": header + "\n尚无结果评论。", "reference": row["identifier"]}
            latest = max(roots, key=lambda r: r.get("last_activity_at") or r.get("created_at") or "")
            thread = self.cli.run(["issue", "comment", "list", row["issue_id"], "--thread", latest["id"], "--tail", "10", "--compact"])
            if not isinstance(thread, list):
                raise BridgeError("CLI评论格式不兼容", 502)
            comments = "\n\n".join(str(r.get("content", "")) for r in thread)
            truncated = len(comments) > 6500
            comments = comments[-6500:]
            return {"reference": row["identifier"], "text": header + "\n最近活动讨论串（根评论及最新10条回复，不代表已完成）：\n" +
                    ("[内容过长，仅显示末尾]\n" if truncated else "") + comments}
        finally:
            self.active_request = None
            self.lock.release()
            print(json.dumps({"event": "bridge_request_finished", "operation": operation,
                              "seconds": round(time.monotonic() - started, 3)}), flush=True)

    def submit(self, body: Dict[str, Any], user: str, session: str) -> Dict[str, Any]:
        message_id = field(body, "message_id")
        title = field(body, "title", 120)
        description = field(body, "description", 12000)
        key = hashlib.sha256(json.dumps([user, session, message_id]).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([title, description], ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            previous = db.execute("SELECT * FROM requests WHERE request_key=?", (key,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise BridgeError("相同消息ID不能提交不同任务", 409)
                if previous["state"] == "submitted":
                    return {"text": f"已提交：{previous['identifier']}（原请求，未重复创建）", "issue_id": previous["issue_id"]}
                raise BridgeError(f"提交结果待管理员核对，禁止自动重试创建。请求标识：{key[:12]}", 409)
            count = db.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND created>?", (user, time.time() - 3600)).fetchone()[0]
            if count >= self.settings.max_submissions_per_hour:
                raise BridgeError("本QQ号本小时提交次数已达上限", 429)
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'pending')",
                       (key, fingerprint, user, session, title, time.time()))
        # Commit pending BEFORE the remote write. A crash/timeout cannot automatically duplicate work.
        try:
            with tempfile.TemporaryDirectory(prefix="request-", dir=self.settings.state_dir) as folder:
                path = Path(folder) / "description.md"
                path.write_text(description + f"\n\n---\n来自 MaiBot QQ 任务桥接。请求标识：{key}\n"
                                "结果请在本任务评论中交付，QQ用户通过插件查询。\n", encoding="utf-8")
                issue = self.cli.run([
                    "issue", "create", "--title", title, "--description-file", str(path),
                    "--project", self.settings.project_id, "--assignee-id", self.settings.assignee_id,
                    "--status", "todo",
                ])
            UUID(issue["id"])
            identifier = field(issue, "identifier")
            with self.connect() as db:
                db.execute("UPDATE requests SET issue_id=?, identifier=?, state='submitted' WHERE request_key=?",
                           (issue["id"], identifier, key))
            return {"text": f"已提交：{identifier}\n查询：/multica 状态 {identifier}\n结果：/multica 结果 {identifier}", "issue_id": issue["id"]}
        except Exception as error:
            # Retain pending also on a CLI nonzero exit: the remote write may already have succeeded.
            raise BridgeError(f"提交结果未确认，请勿重新发新任务；请管理员核对请求标识 {key[:12]}", 502) from error


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple, service: Service):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Server

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Never log request paths, credentials or user prompts.

    def respond(self, status: int, data: Dict[str, Any]) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        try:
            expected = ("Bearer " + self.server.service.settings.token).encode()
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                raise BridgeError("桥接认证失败", 401)
            if self.path != "/v1/request":
                raise BridgeError("接口不存在", 404)
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise BridgeError("仅支持带Content-Length的JSON请求", 415)
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST:
                raise BridgeError("请求体大小无效", 413)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise BridgeError("请求必须是JSON对象")
            if size > 65536 and body.get("operation") != "discuss" and not (body.get("operation") == "delivery_ack" and size <= 2 * 1024 * 1024):
                raise BridgeError("仅讨论附件上传允许大请求", 413)
            self.respond(200, {"ok": True, **self.server.service.handle(body)})
        except BridgeError as error:
            self.respond(error.status, {"ok": False, "error": str(error), **({"code": error.code} if error.code else {})})
        except (ValueError, UnicodeError, TimeoutError):
            self.respond(400, {"ok": False, "error": "请求格式错误或读取超时"})
        except Exception:
            self.respond(500, {"ok": False, "error": "桥接内部错误，请管理员检查服务"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    # A task token expires with the coding run and must never become a service credential.
    if (any(os.environ.get(key) for key in ("MULTICA_TASK_ID", "MULTICA_TASK_CONFIG_ROOT", "MULTICA_AGENT_ID", "MULTICA_DAEMON_PORT"))
            or os.environ.get("MULTICA_TOKEN", "").startswith("mat_")):
        parser.error("请从普通Windows终端启动桥接，使用独立CLI登录；不能持久复用Agent任务环境")
    config_path = args.config.resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    token_path = (config_path.parent / raw.pop("token_file")).resolve()
    raw["token"] = token_path.read_text(encoding="ascii").strip()
    raw["state_dir"] = (config_path.parent / raw.get("state_dir", "state")).resolve()
    raw["cli_path"] = os.environ.get("MULTICA_CLI_PATH") or raw.get("cli_path", "")
    service = Service(Settings(**raw))
    server = Server((args.listen, args.port), service)
    print(f"MaiBot Multica bridge listening on {args.listen}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
