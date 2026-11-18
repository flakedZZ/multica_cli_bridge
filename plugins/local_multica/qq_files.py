"""SnowLuma 2.4.2 file upload+publish, through the installed adapter's public API.

No container paths or authenticated download URLs cross this boundary. A durable
claim is committed before the single upload call. An interrupted call is held
for audit, never replayed merely because a timer expired.
"""
import asyncio
import base64
import hashlib
import json
import sqlite3
import time

from .delivery import DeliveryStore

API = "maibot-team.snowluma-adapter.adapter.snowluma.action.call"
MAX_BYTES = 32 * 1024 * 1024


def decode_file(item, result):
    encoded = result.get("file_base64")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError("文件大小超限")
    data = base64.b64decode(encoded, validate=True)
    if (not 0 < len(data) <= MAX_BYTES or len(data) != item["size_bytes"] or
            any(result.get(k) != item[k] for k in ("attachment_id", "filename", "content_type", "size_bytes")) or
            hashlib.sha256(data).hexdigest() != result.get("sha256")):
        raise ValueError("文件字节或元数据校验失败")
    return data


def success(response):
    return (isinstance(response, dict) and response.get("status") == "ok" and
            type(response.get("retcode")) is int and response["retcode"] == 0)


class QQFiles:
    def __init__(self, plugin):
        self.p = plugin
        self.store = DeliveryStore(plugin.ctx.paths.data_dir / "deliveries.sqlite3")
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS qq_files (
                key TEXT PRIMARY KEY, identity TEXT NOT NULL, state TEXT NOT NULL,
                receipt TEXT, updated REAL NOT NULL, error TEXT)""")

    def get(self, key):
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM qq_files WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def claim(self, key, identity):
        with self.store.connect() as db:
            return db.execute("INSERT OR IGNORE INTO qq_files VALUES (?,?, 'pending',NULL,?,NULL)",
                              (key, identity, time.time())).rowcount == 1

    def finish(self, key, receipt=None, error=None):
        with self.store.connect() as db:
            # Duplicate callbacks may confirm a pending/uncertain attempt, but
            # cannot replace a confirmed receipt or downgrade it on a late timeout.
            row = db.execute("SELECT state,receipt FROM qq_files WHERE key=?", (key,)).fetchone()
            if not row:
                raise ValueError("未知文件发送尝试")
            if row[0] == "sent":
                if receipt and json.loads(row[1]) != receipt:
                    raise ValueError("已确认文件回执不可变更")
                return
            db.execute("UPDATE qq_files SET state=?,receipt=?,updated=?,error=? WHERE key=?",
                       ("sent" if receipt else "uncertain", json.dumps(receipt) if receipt else None,
                        time.time(), error, key))

    async def action(self, name, params):
        return await self.p.ctx.api.call(API, version="1", action_name=name, params=params)

    async def target(self, job):
        streams = await self.p.ctx.chat.get_all_streams(platform="qq")
        if isinstance(streams, dict):
            streams = streams.get("streams", [])
        if not isinstance(streams, list):
            raise ValueError("无法读取原会话")
        found = [s for s in streams if s.get("session_id") == job["session_id"] and s.get("platform") == "qq"]
        if len(found) != 1:
            raise ValueError("原QQ会话不存在或不唯一")
        stream = found[0]
        if stream.get("scope"):
            raise ValueError("临时或限定QQ会话尚未验证文件发送能力")
        kind = stream.get("chat_type")
        if kind not in {"group", "private"}:
            raise ValueError("不支持的QQ会话类型")
        target = str(stream.get("group_id" if kind == "group" else "user_id") or "")
        if not target.isdigit() or int(target) <= 0 or (kind == "private" and target != job["user_id"]):
            raise ValueError("QQ会话目标校验失败")
        version = await self.action("get_version_info", {})
        info = version.get("data", {}) if success(version) else {}
        if str(info.get("app_name", "")).lower() != "snowluma" or info.get("app_version") not in {"2.4.2", "2.4.2-node"}:
            raise ValueError("当前仅验证SnowLuma 2.4.2文件回执协议，其他客户端需先验证")
        login = await self.action("get_login_info", {})
        account = str(login.get("data", {}).get("user_id", "")) if success(login) else ""
        if not account or account != str(stream.get("account_id") or ""):
            raise ValueError("适配器账号与原会话不一致，停止文件发送")
        return kind, target

    async def send(self, key, job, item, data):
        name = item["filename"]
        if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES or len(data) != item["size_bytes"] or
                not name or len(name) > 180 or name in {".", ".."} or
                any(c in name for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in name)):
            raise ValueError("无效文件字节或文件名；禁止传入本地路径")
        sha = hashlib.sha256(data).hexdigest()
        identity = json.dumps({"session_id": job["session_id"], "user_id": job["user_id"],
                               "attachment_id": item["attachment_id"], "filename": name,
                               "size_bytes": len(data), "sha256": sha}, sort_keys=True)
        prior = await asyncio.to_thread(self.get, key)
        if prior:
            if prior["identity"] != identity:
                raise ValueError("文件发送去重键内容冲突")
            return json.loads(prior["receipt"]) if prior["state"] == "sent" else None
        # Read-only failures here can be retried. No send has been claimed yet.
        kind, target = await self.target(job)
        if not self.p.config.plugin.enabled or job["user_id"] not in self.p.config.bridge.allowed_users:
            return None
        action = "upload_group_file" if kind == "group" else "upload_private_file"
        params = {"group_id" if kind == "group" else "user_id": int(target),
                  "file": "base64://" + base64.b64encode(data).decode("ascii"), "name": name, "upload_file": True}
        if not await asyncio.to_thread(self.claim, key, identity):
            return None
        try:
            response = await self.action(action, params)
            fid = response.get("data", {}).get("file_id") if success(response) else None
            if not isinstance(fid, str) or not fid.strip() or len(fid) > 1024:
                raise ValueError("文件上传发布无有效成功回执")
            receipt = {"sent": True, "file_id": fid, "action": action, "target_type": kind,
                       "target_id": target, "session_id": job["session_id"], "filename": name, "size_bytes": len(data), "sha256": sha,
                       "status": "ok", "retcode": 0, "adapter": "SnowLuma", "adapter_version": "2.4.2",
                       "response": {"status": "ok", "retcode": 0, "data": {"file_id": fid}}}
            await asyncio.to_thread(self.finish, key, receipt)
            return receipt
        except Exception as error:
            # Adapter errors include timeouts and post-upload publish failures;
            # none prove that QQ has received nothing. Never log params/tokens.
            await asyncio.to_thread(self.finish, key, error=type(error).__name__)
            self.p.ctx.logger.warning("Multica文件发送结果待核对 [%s]，禁止自动重发", type(error).__name__)
            return None
