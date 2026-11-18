"""Durable receiver acknowledgements; only 1.5 business links may auto-close.

Service.lock serializes requests. The acknowledgement commits before CLI writes.
No run completion, historic timer or manually-done sibling proves delivery.
"""
import hashlib
import json
import time

from .artifacts import category


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def delivered_content(snapshot):
    # Setting the execution to done changes its revision. All other content and
    # descendant revisions must still match the acknowledged final result.
    copy = json.loads(json.dumps(snapshot))
    for part in copy["parts"]:
        if part["issue_id"] == copy["issue_id"]:
            part.pop("revision", None)
    return copy


def children(cli, parent, error):
    tree = cli.run(["issue", "children", parent])
    if not isinstance(tree, dict) or not isinstance(tree.get("unstaged"), list) or not isinstance(tree.get("stages"), list):
        raise error("子任务列表不完整，保留父单", 502)
    rows = list(tree["unstaged"])
    for stage in tree["stages"]:
        if not isinstance(stage, dict) or not isinstance(stage.get("issues"), list):
            raise error("子任务阶段格式无效", 502)
        rows.extend(stage["issues"])
    if any(not isinstance(r, dict) or not r.get("id") for r in rows):
        raise error("子任务记录无效", 502)
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids) or ("total" in tree and tree["total"] != len(ids)):
        raise error("子任务列表截断或重复，保留父单", 502)
    return ids


class CompletionProtocol:
    def __init__(self, service, error):
        self.s, self.error = service, error
        with service.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS execution_links (
                request_key TEXT PRIMARY KEY, parent_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS delivery_acks (
                token TEXT PRIMARY KEY, issue_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                evidence TEXT, acknowledged REAL, closed INTEGER NOT NULL DEFAULT 0)""")

    def link(self, row):
        with self.s.connect() as db:
            return db.execute("SELECT * FROM execution_links WHERE request_key=?", (row["request_key"],)).fetchone()

    def snapshot(self, issue, row):
        link = self.link(row)
        if not link or issue.get("parent_issue_id") != link["parent_id"]:
            raise self.error("缺少新版父子业务关联，禁止自动关闭", 409)
        if category(issue) not in {"in_review", "done"}:
            raise self.error("任务尚未交付", 409)
        root = self.s.artifacts.handle(issue, {"operation": "artifacts"})
        if set(children(self.s.cli, issue["id"], self.error)) != set(root["children"]):
            raise self.error("执行子任务列表变化或截断，保留任务", 409)
        sources = [(issue["id"], root)]
        for child in root["children"]:
            sources.append((child, self.s.artifacts.handle(issue, {"operation": "artifacts", "source_issue_id": child})))
        parts = []
        for source_id, result in sources:
            if result["status_category"] == "cancelled" and source_id != issue["id"]:
                parts.append({"issue_id": source_id, "cancelled": True, "revision": result["revision"]})
                continue
            if (not result.get("ready") or not result.get("delivery_complete") or result.get("truncated") or
                    not result.get("manifest_required") or not result.get("result_text") or type(result.get("revision")) is not int):
                raise self.error("完整交付清单/正文/版本尚未就绪", 409)
            parts.append({"issue_id": source_id, "revision": result["revision"],
                          "text": result["result_text"], "comment_id": result.get("result_comment_id"),
                          "images": result["images"], "files": result["files"]})
        return {"issue_id": issue["id"], "parent_id": link["parent_id"], "reference": row["identifier"],
                "user_id": row["user_id"], "session_id": row["session_id"], "parts": parts}

    def prepare(self, issue, row):
        snapshot = self.snapshot(issue, row)
        token = digest(snapshot)
        with self.s.connect() as db:
            db.execute("INSERT OR IGNORE INTO delivery_acks (token,issue_id,snapshot) VALUES (?,?,?)",
                       (token, issue["id"], json.dumps(snapshot, ensure_ascii=False)))
        return {"text": "最终交付快照已准备，等待MaiBot回执", "token": token, "snapshot": snapshot}

    def validate_evidence(self, snapshot, evidence):
        if not isinstance(evidence, dict) or evidence.get("snapshot_sha256") != digest(snapshot):
            raise self.error("交付回执与结果快照不匹配", 409)
        text = evidence.get("text_messages")
        parts = [p for p in snapshot["parts"] if not p.get("cancelled")]
        if (not isinstance(text, list) or not text or any(not isinstance(i, str) or not i.strip() or len(i) > 300 for i in text)
                or evidence.get("text_sha256") != digest([p["text"] for p in parts])):
            raise self.error("缺少完整正文发送回执", 409)
        receipts = evidence.get("attachments")
        if not isinstance(receipts, dict):
            raise self.error("缺少附件回执", 409)
        expected = {i["attachment_id"]: (i, kind) for p in parts for kind in ("images", "files") for i in p[kind]}
        if set(receipts) != set(expected):
            raise self.error("附件回执不齐全", 409)
        for aid, (item, kind) in expected.items():
            receipt = receipts[aid]
            if (not isinstance(receipt, dict) or receipt.get("received") is not True or
                    receipt.get("size_bytes") != item["size_bytes"] or
                    not isinstance(receipt.get("sha256"), str) or len(receipt["sha256"]) != 64 or
                    any(c not in "0123456789abcdef" for c in receipt["sha256"])):
                raise self.error("附件接收校验回执无效", 409)
            if kind == "images" and (not isinstance(receipt.get("message_id"), str) or not receipt["message_id"].strip()):
                raise self.error("图片尚无平台发送成功回执", 409)
            if kind == "files":
                sent = receipt.get("qq_file")
                if (not isinstance(sent, dict) or sent.get("sent") is not True or
                        sent.get("status") != "ok" or type(sent.get("retcode")) is not int or sent["retcode"] != 0 or
                        not isinstance(sent.get("file_id"), str) or not sent["file_id"].strip() or
                        sent.get("action") not in {"upload_group_file", "upload_private_file"} or
                        sent.get("target_type") != ("group" if sent["action"] == "upload_group_file" else "private") or
                        not str(sent.get("target_id", "")).isdigit() or
                        sent.get("session_id") != snapshot["session_id"] or
                        (sent["target_type"] == "private" and sent["target_id"] != snapshot["user_id"]) or
                        any(sent.get(k) != v for k, v in {"filename": item["filename"], "size_bytes": item["size_bytes"],
                                                         "sha256": receipt["sha256"]}.items())):
                    raise self.error("普通文件尚无QQ上传并发布成功回执", 409)

    def acknowledge(self, issue, row, body):
        token = body.get("token")
        if not isinstance(token, str):
            raise self.error("缺少交付令牌")
        with self.s.connect() as db:
            record = db.execute("SELECT * FROM delivery_acks WHERE token=? AND issue_id=?", (token, issue["id"])).fetchone()
        if not record:
            raise self.error("未知交付令牌", 409)
        snapshot = json.loads(record["snapshot"])
        if (snapshot["user_id"], snapshot["session_id"]) != (row["user_id"], row["session_id"]):
            raise self.error("回执归属错误", 403)
        evidence = body.get("evidence")
        self.validate_evidence(snapshot, evidence)
        if record["evidence"] and json.loads(record["evidence"]) != evidence:
            old = json.loads(record["evidence"])
            comparable = json.loads(json.dumps(evidence))
            added = False
            for part in snapshot["parts"]:
                for item in part.get("files", []):
                    aid = item["attachment_id"]
                    if not old.get("attachments", {}).get(aid, {}).get("qq_file"):
                        comparable["attachments"][aid].pop("qq_file", None)
                        added = True
            # Upgrade only an unfinished 1.5 acknowledgement by adding its exact
            # missing QQ file receipts. Text, hashes, snapshot and existing IDs
            # are immutable. Already-closed history is never migrated here.
            if (record["closed"] or not record["acknowledged"] or not added or comparable != old or
                    delivered_content(self.snapshot(issue, row)) != delivered_content(snapshot)):
                raise self.error("同一交付令牌的回执不可变更", 409)
            with self.s.connect() as db:
                db.execute("UPDATE delivery_acks SET evidence=? WHERE token=?",
                           (json.dumps(evidence, sort_keys=True), token))
        if not record["acknowledged"]:
            if digest(self.snapshot(issue, row)) != token:
                raise self.error("交付内容已变化，旧回执不能关闭新版本", 409)
            with self.s.connect() as db:
                db.execute("UPDATE delivery_acks SET evidence=?, acknowledged=? WHERE token=?",
                           (json.dumps(evidence, sort_keys=True), time.time(), token))
        if record["closed"]:
            return {"closed": True, "discussion_closed": True, "text": "该交付已收尾（幂等回执）"}
        return self.reconcile(issue, row, snapshot, token)

    def reconcile(self, issue, row, snapshot, token):
        parent_id = snapshot["parent_id"]
        if issue.get("parent_issue_id") != parent_id:
            raise self.error("执行单已改挂父单，保留状态等待核对", 409)
        current = self.snapshot(issue, row)
        if delivered_content(current) != delivered_content(snapshot):
            raise self.error("已确认回执不覆盖当前交付内容", 409)
        if category(issue) != "done":
            if digest(self.snapshot(issue, row)) != token:
                raise self.error("回执之后任务又发生变化，保留新工作", 409)
            self.s.cli.run(["issue", "status", issue["id"], "done", "--no-start"])
        parent = self.s.cli.run(["issue", "get", parent_id])
        if parent.get("project_id") != self.s.settings.project_id:
            raise self.error("父单项目已变化", 409)
        wait = {"closed": False, "execution_closed": True, "discussion_closed": False, "text": "执行单已完成，父单等待其他工作或补齐"}
        if category(parent) == "cancelled":
            return wait
        with self.s.connect() as db:
            siblings = db.execute("SELECT r.* FROM execution_links l JOIN requests r ON r.request_key=l.request_key WHERE l.parent_id=?",
                                  (parent_id,)).fetchall()
            unfinished = db.execute("SELECT 1 FROM dialogues WHERE discussion_id=? AND state NOT IN ('confirmed','cancelled') LIMIT 1",
                                    (parent_id,)).fetchone()
            acknowledgements = db.execute("SELECT issue_id,snapshot FROM delivery_acks WHERE acknowledged IS NOT NULL").fetchall()
        if unfinished or any(r["state"] != "submitted" for r in siblings):
            return wait
        ids = children(self.s.cli, parent_id, self.error)
        known = {r["issue_id"] for r in siblings}
        owned = {r["issue_id"]: r for r in siblings}
        if not known or not known.issubset(ids):
            return wait
        for child_id in ids:
            child = self.s.cli.run(["issue", "get", child_id])
            if child.get("parent_issue_id") != parent_id or child.get("project_id") != self.s.settings.project_id:
                return wait
            if category(child) == "cancelled":
                continue
            if child_id not in known or category(child) != "done":
                return wait
            current_child = delivered_content(self.snapshot(child, owned[child_id]))
            if not any(r["issue_id"] == child_id and delivered_content(json.loads(r["snapshot"])) == current_child
                       for r in acknowledgements):
                return wait
        active = self.s.cli.run(["issue", "runs", parent_id, "--active"])
        if not isinstance(active, list) or active:
            return wait
        if category(parent) != "done":
            self.s.cli.run(["issue", "status", parent_id, "done", "--no-start"])
        with self.s.connect() as db:
            db.execute("UPDATE delivery_acks SET closed=1 WHERE token=?", (token,))
        return {"closed": True, "discussion_closed": True, "text": "MaiBot已确认收到最终结果，执行单与需求父单已完成"}
