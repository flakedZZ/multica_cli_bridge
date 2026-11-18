"""MaiBot's durable receiving inbox and acknowledgement outbox (protocol 1).

Receiving bytes is separate from QQ sends. A successful send requires both the
SDK sent flag and a platform message ID. Unknown sends remain pending for audit:
SDK 2.21 has no idempotency key or durable receipt lookup, so replay is unsafe.
"""
import asyncio
import base64
import hashlib
import json
import sqlite3

from .delivery import DeliveryStore
from .client import BridgeBusyError
from .qq_files import QQFiles, decode_file


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Completion:
    def __init__(self, plugin):
        self.p = plugin
        self.store = DeliveryStore(plugin.ctx.paths.data_dir / "deliveries.sqlite3")
        self.qq_files = QQFiles(plugin)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS received_results (
                job_key TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, job TEXT NOT NULL,
                snapshot TEXT NOT NULL, evidence TEXT, closed INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS received_files (
                token TEXT NOT NULL, attachment_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                data BLOB NOT NULL, PRIMARY KEY(token,attachment_id))""")
            # Only unfinished outboxes need the stronger file receipts. Keep
            # confirmed text/image sends and historical closed records intact.
            for key, raw in db.execute("SELECT job_key,evidence FROM received_results WHERE closed=0 AND evidence IS NOT NULL").fetchall():
                evidence = json.loads(raw)
                snapshot = json.loads(db.execute("SELECT snapshot FROM received_results WHERE job_key=?", (key,)).fetchone()[0])
                aids = [i["attachment_id"] for part in snapshot["parts"] for i in part.get("files", [])]
                if any(not evidence.get("attachments", {}).get(aid, {}).get("qq_file") for aid in aids):
                    db.execute("UPDATE received_results SET evidence=NULL WHERE job_key=?", (key,))

    def get(self, key):
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM received_results WHERE job_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def receive(self, key, job, bundle):
        snapshot, token = bundle["snapshot"], bundle["token"]
        if (digest(snapshot) != token or snapshot["user_id"] != job["user_id"] or
                snapshot["session_id"] != job["session_id"] or snapshot["reference"] != job["reference"]):
            raise ValueError("交付快照校验失败")
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO received_results (job_key,token,job,snapshot) VALUES (?,?,?,?)",
                       (key, token, json.dumps(job), json.dumps(snapshot, ensure_ascii=False)))
            if not db.execute("SELECT 1 FROM received_results WHERE job_key=?", (key,)).fetchone():
                raise ValueError("同一结果已经由其他任务引用接收，禁止重复发送")

    def file(self, token, aid):
        with self.store.connect() as db:
            row = db.execute("SELECT sha256,data FROM received_files WHERE token=? AND attachment_id=?", (token, aid)).fetchone()
        if row and hashlib.sha256(row[1]).hexdigest() != row[0]:
            raise ValueError("持久收件箱附件校验失败，停止交付")
        return row

    def receive_file(self, token, item, result, kind):
        if kind == "files":
            decode_file(item, result)
        limit = 8 * 1024 * 1024 if kind == "images" else 32 * 1024 * 1024
        encoded = result.get("image_base64" if kind == "images" else "file_base64")
        if not isinstance(encoded, str) or len(encoded) > ((limit + 2) // 3) * 4:
            raise ValueError("附件大小超限")
        data = base64.b64decode(encoded, validate=True)
        sha = hashlib.sha256(data).hexdigest()
        if (len(data) != item["size_bytes"] or not 0 < len(data) <= limit or sha != result.get("sha256") or
                result.get("attachment_id") != item["attachment_id"]):
            raise ValueError("附件接收校验失败")
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO received_files VALUES (?,?,?,?)", (token, item["attachment_id"], sha, data))

    def message_id(self, key):
        with self.store.connect() as db:
            row = db.execute("SELECT state,message_id FROM deliveries WHERE key=?", (key,)).fetchone()
        return row[1] if row and row[0] == "sent" and row[1] else None

    async def send(self, receipt, kind, content, session):
        prior = await asyncio.to_thread(self.message_id, receipt)
        if prior:
            return prior
        if not await asyncio.to_thread(self.store.begin, receipt):
            return None
        try:
            response = await getattr(self.p.ctx.send, kind)(content, session, return_details=True)
        except Exception:
            response = None
        # False is also uncertain: the host collapses transport exceptions to
        # sent=False, so it is not evidence that the platform did not receive it.
        valid = (isinstance(response, dict) and response.get("sent") is True and
                 isinstance(response.get("message_id"), str) and bool(response["message_id"].strip()))
        await asyncio.to_thread(self.store.finish, receipt, response if valid else None)
        return response["message_id"] if valid else None

    async def deliver(self, key, job):
        p = self.p
        record = await asyncio.to_thread(self.get, key)
        if not record:
            try:
                bundle = await p.rpc({**job, "operation": "delivery_prepare"})
                await asyncio.to_thread(self.receive, key, job, bundle)
            except BridgeBusyError:
                return
            except Exception:
                await p.delivery_check(key, job, True)
                return
            record = await asyncio.to_thread(self.get, key)
        if record["evidence"] or record["closed"]:
            await self.acknowledge(record)
            return
        snapshot, token = json.loads(record["snapshot"]), record["token"]
        parts = [part for part in snapshot["parts"] if not part.get("cancelled")]
        attachments = [(item, kind) for part in parts for kind in ("images", "files") for item in part[kind]]
        downloaded = 0
        for item, kind in attachments:
            if await asyncio.to_thread(self.file, token, item["attachment_id"]):
                continue
            if downloaded >= 2:
                return
            try:
                result = await p.rpc({**job, "operation": "image" if kind == "images" else "file",
                                      "attachment_id": item["attachment_id"], "source_issue_id": item["source_issue_id"]})
                await asyncio.to_thread(self.receive_file, token, item, result, kind)
                downloaded += 1
            except BridgeBusyError:
                return
            except Exception:
                await p.delivery_check(key, job, True)
                return
        await p.delivery_check(key, job, False)
        evidence = {"snapshot_sha256": token, "text_sha256": digest([part["text"] for part in parts]),
                    "attachments": {}, "text_messages": []}
        for item, kind in attachments:
            sha, data = await asyncio.to_thread(self.file, token, item["attachment_id"])
            receipt = {"received": True, "size_bytes": len(data), "sha256": sha}
            if kind == "images":
                if not p.config.routing.auto_return_images:
                    return
                if not self.enabled(job):
                    return
                mid = await self.send(key + ":v1:image:" + sha, "image", base64.b64encode(data).decode("ascii"), job["session_id"])
                if not mid:
                    return
                receipt["message_id"] = mid
            else:
                try:
                    sent = await self.qq_files.send(key + ":file:" + item["attachment_id"], job, item, data)
                except Exception:
                    await p.delivery_check(key, job, True)
                    return
                if not sent:
                    return
                receipt["qq_file"] = sent
            evidence["attachments"][item["attachment_id"]] = receipt
        text = f"任务 {job['reference']} 已返回结果。\n" + "\n\n".join(part["text"] for part in parts)
        names = [i["filename"] for i, kind in attachments if kind == "files"]
        if names:
            text += "\n文件已发送到本QQ会话：" + "、".join(names) + "。"
        # Preserve the full final result. Never acknowledge a tail-only summary.
        for index, offset in enumerate(range(0, len(text), 1000)):
            if not self.enabled(job):
                return
            mid = await self.send(token + ":text:" + str(index), "text", text[offset:offset + 1000], job["session_id"])
            if not mid:
                return
            evidence["text_messages"].append(mid)
        with self.store.connect() as db:
            db.execute("UPDATE received_results SET evidence=? WHERE job_key=? AND evidence IS NULL",
                       (json.dumps(evidence, sort_keys=True), key))
        async with p.state_lock:
            p.jobs.pop(key, None)
            await p.save_jobs()
        # Persisted outbox survives a lost HTTP response and either process restart.
        await self.acknowledge(await asyncio.to_thread(self.get, key))

    def enabled(self, job):
        return self.p.config.plugin.enabled and job["user_id"] in self.p.config.bridge.allowed_users

    async def acknowledge(self, record):
        job = json.loads(record["job"])
        if not self.enabled(job) or not self.p.config.closure.enabled or not record["evidence"]:
            return
        if not record["closed"]:
            response = await self.p.rpc({**job, "operation": "delivery_ack", "token": record["token"],
                                         "evidence": json.loads(record["evidence"])})
            if response.get("closed") is not True:
                return
            with self.store.connect() as db:
                db.execute("UPDATE received_results SET closed=1 WHERE token=?", (record["token"],))
        async with self.p.state_lock:
            self.p.jobs.pop(record["job_key"], None)
            await self.p.save_jobs()

    async def poll(self):
        with self.store.connect() as db:
            keys = [r[0] for r in db.execute("SELECT job_key FROM received_results WHERE closed=0")]
        for key in keys:
            try:
                record = await asyncio.to_thread(self.get, key)
                job = json.loads(record["job"])
                if not self.enabled(job):
                    continue
                # Share the same gate as the normal job poller.
                lock = getattr(self.p, "delivery_lock", None)
                if lock is None:
                    await self.deliver(key, job)
                else:
                    async with lock:
                        await self.deliver(key, job)
            except Exception as error:
                self.p.ctx.logger.warning("Multica送达回执收尾待重试 [%s]", type(error).__name__)
