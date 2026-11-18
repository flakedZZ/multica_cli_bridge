from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import base64
import json
import subprocess
import sys
import threading
import unittest

from PIL import Image

from bridge.artifacts import MAX_IMAGE_BYTES
from bridge.server import BridgeError, Cli, Server, Service, Settings
from plugins.local_multica.client import request
from plugins.local_multica.delivery import DeliveryStore, verify_payload
from plugins.local_multica.routing import inject, instruction


ROOT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
ATTACHMENT = "00000000-0000-4000-8000-000000000003"
PROJECT = "00000000-0000-4000-8000-000000000007"


def png(large=False):
    picture = Image.effect_noise((400, 400), 100).convert("RGB") if large else Image.new("RGB", (8, 8), "blue")
    buffer = BytesIO()
    picture.save(buffer, "PNG")
    return buffer.getvalue()


class ImageCli:
    def __init__(self):
        self.data = png()
        self.calls = []
        self.downloads = []
        self.comments = []
        self.root = {"id": ROOT, "project_id": PROJECT, "status": "in_review", "attachments": [], "last_activity_at": "2020-01-01T00:00:00Z"}
        self.child = {"id": CHILD, "project_id": PROJECT, "parent_issue_id": ROOT, "status": "done", "attachments": [], "last_activity_at": "2020-01-01T00:00:00Z"}
        self.tree = {"unstaged": [], "stages": [], "total": 0}
        self.attachment = {"id": ATTACHMENT, "issue_id": ROOT, "comment_id": "comment", "filename": "生成的图.png",
                           "size_bytes": len(self.data), "content_type": "image/png"}
        self.root["attachments"].append(self.attachment)

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "get"]:
            return self.root if args[2] == ROOT else self.child
        if args[:2] == ["issue", "children"]:
            return self.tree
        if args[:3] == ["issue", "comment", "list"]:
            return self.comments
        raise AssertionError(args)

    def download(self, attachment, destination):
        self.downloads.append(attachment)
        path = destination / self.attachment["filename"]
        path.write_bytes(self.data)
        return path


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(cli_path=sys.executable, workspace_id="00000000-0000-4000-8000-000000000006",
                                 project_id=PROJECT, assignee_id=CHILD, allowed_users=["12345", "67890"],
                                 token="test-only-" + "x" * 32, state_dir=Path(self.temp.name))
        self.cli = ImageCli()
        self.service = Service(self.settings, self.cli)
        with self.service.connect() as db:
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,'submitted')", ("r", "f", "12345", "room", "画图", 0, ROOT, "DEMO-12"))
        self.body = {"platform": "qq", "user_id": "12345", "session_id": "room", "reference": "DEMO-12",
                     "operation": "image", "attachment_id": ATTACHMENT}

    def call(self, **changes):
        return self.service.handle({**self.body, **changes})

    def test_download_decode_hash_and_cleanup(self):
        result = self.call()
        self.assertEqual(base64.b64decode(verify_payload(result)), self.cli.data)
        self.assertEqual(result["width"], 8)
        self.assertEqual(list(Path(self.temp.name).glob("image-*")), [])

    def test_user_session_project_and_attachment_scope(self):
        for change in ({"user_id": "67890"}, {"session_id": "other"}, {"attachment_id": CHILD}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.call(**change)
        self.cli.root["project_id"] = "other"
        with self.assertRaises(BridgeError):
            self.call()
        self.assertEqual(self.cli.downloads, [])

    def test_direct_child_and_stage_listing(self):
        self.cli.tree = {"unstaged": [], "stages": [{"stage": 1, "issues": [{"id": CHILD}]}], "total": 1}
        self.assertEqual(self.call(operation="artifacts")["children"], [CHILD])
        self.cli.attachment["issue_id"] = CHILD
        self.cli.child["attachments"] = [self.cli.attachment]
        self.assertIn("image_base64", self.call(source_issue_id=CHILD))
        self.cli.child["parent_issue_id"] = "another-root"
        with self.assertRaises(BridgeError):
            self.call(source_issue_id=CHILD)

    def test_only_comment_outputs_ready_for_delivery(self):
        self.cli.root["status"] = "in_progress"
        self.assertFalse(self.call(operation="artifacts")["ready"])
        with self.assertRaises(BridgeError):
            self.call()
        self.cli.root["status_category"] = "in_review"
        self.cli.root["status"] = "custom-review"
        self.assertTrue(self.call(operation="artifacts")["ready"])
        self.cli.attachment["comment_id"] = None
        with self.assertRaises(BridgeError):
            self.call()

    def test_invalid_metadata_never_downloaded(self):
        original = dict(self.cli.attachment)
        for changes in ({"filename": "../secret.png"}, {"filename": "C:\\secret.png"},
                        {"size_bytes": MAX_IMAGE_BYTES + 1}, {"content_type": "text/html"},
                        {"filename": "payload.svg"}, {"issue_id": CHILD}):
            self.cli.attachment.clear()
            self.cli.attachment.update({**original, **changes})
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                self.call()
        self.assertEqual(self.cli.downloads, [])

    def test_corrupt_or_spoofed_images_rejected(self):
        self.cli.data = b"<html>not an image</html>"
        self.cli.attachment["size_bytes"] = len(self.cli.data)
        with self.assertRaisesRegex(BridgeError, "解码"):
            self.call()
        self.cli.data = png()
        self.cli.attachment["size_bytes"] = len(self.cli.data)
        self.cli.attachment["content_type"] = "image/jpeg"
        with self.assertRaises(BridgeError):
            self.call()

    def test_actual_size_and_escape_checked(self):
        self.cli.attachment["size_bytes"] += 1
        with self.assertRaisesRegex(BridgeError, "实际大小"):
            self.call()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(self.cli.data)
        with patch.object(self.cli, "download", return_value=outside):
            with self.assertRaisesRegex(BridgeError, "隔离目录"):
                self.call()

    def test_http_transfers_image_larger_than_old_json_limit(self):
        self.cli.data = png(large=True)
        self.cli.attachment["size_bytes"] = len(self.cli.data)
        self.assertGreater(len(self.cli.data), 65536)
        server = Server(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        token = Path(self.temp.name) / "test.token"
        token.write_text(self.settings.token, encoding="ascii")
        try:
            result = request(f"http://127.0.0.1:{server.server_port}", str(token), self.body)
            self.assertEqual(base64.b64decode(verify_payload(result)), self.cli.data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_cli_download_has_no_unsupported_output_flag(self):
        path = Path(self.temp.name) / "生成的图.png"
        completed = subprocess.CompletedProcess([], 0, json.dumps({"path": str(path)}), "Downloaded")
        with patch("bridge.server.subprocess.run", return_value=completed) as run:
            self.assertEqual(Cli(self.settings).download(ATTACHMENT, Path(self.temp.name)), path)
            argv = run.call_args.args[0]
            self.assertNotIn("--output", argv)
            self.assertIn("--output-dir", argv)
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_receipts_survive_restart_and_uncertainty(self):
        path = Path(self.temp.name) / "receipts.db"
        store = DeliveryStore(path)
        self.assertTrue(store.begin("sent"))
        store.finish("sent", {"sent": True, "message_id": "qq-id"})
        self.assertTrue(store.begin("uncertain"))
        store.finish("uncertain", None)
        self.assertTrue(store.begin("crash"))
        store = DeliveryStore(path)
        for key in ("sent", "uncertain", "crash"):
            self.assertFalse(store.begin(key))

    def test_explicit_send_failure_retries_at_most_three_times(self):
        store = DeliveryStore(Path(self.temp.name) / "receipts.db")
        for attempt in range(3):
            self.assertTrue(store.begin("failed"))
            store.finish("failed", {"sent": False})
            with store.connect() as db:
                db.execute("UPDATE deliveries SET updated=0")
        self.assertEqual(store.status("failed"), "failed")
        self.assertFalse(store.begin("failed"))

    def test_routing_modes_and_prompt_does_not_break_tool_chain(self):
        self.assertIn("生图", instruction("complex"))
        self.assertIn("所有用户明确交办", instruction("all_tasks"))
        self.assertIn("不需要提到规划Agent", instruction("complex"))
        rows = [{"item_type": "SystemMessageItem", "meta": {"item_id": "host"}},
                {"item_type": "FunctionCallItem"}, {"item_type": "FunctionResultItem"}]
        result = inject(inject(rows, "complex"), "all_tasks")
        self.assertEqual(len(result), 4)
        self.assertEqual(result[-2:], rows[-2:])
