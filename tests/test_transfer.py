import asyncio
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import unittest
import threading

import test_artifacts as artifact_fixtures
import test_dialogue as dialogue_fixtures
from bridge.server import BridgeError
from bridge.server import Server
from plugins.local_multica.client import request
from bridge.manifest import MARKER
from bridge.uploads import decode_attachments
from plugins.local_multica.client import ClientError
from plugins.local_multica.inputs import collect, safe_name
from plugins.local_multica.polling import PollingSettings, validate_changes


def upload(data=b"file contents", filename="01-中文文档.txt"):
    return {"filename": filename, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "base64": base64.b64encode(data).decode("ascii")}


def manifest(fixture, names):
    fixture.cli.root["description"] = MARKER
    fixture.cli.comments = [{"id": "final", "created_at": "2026-01-01T00:00:00Z", "author_type": "agent",
                             "author_id": fixture.settings.mika_id,
                             "content": "交付附件列表\n```json\n" + json.dumps({"multica_delivery": 1, "attachments": names}) + "\n```"}]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.f = artifact_fixtures.ArtifactTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def read(self):
        return self.f.call(operation="artifacts")

    def test_partial_manifest_waits_until_all_attachments_arrive(self):
        name = self.f.cli.attachment["filename"]
        manifest(self.f, [name, "second.png"])
        value = self.read()
        self.assertFalse(value["delivery_complete"])
        self.assertEqual(value["missing"], ["second.png"])
        self.f.cli.root["attachments"].append({**self.f.cli.attachment, "filename": "second.png", "id": artifact_fixtures.CHILD})
        self.assertTrue(self.read()["delivery_complete"])

    def test_missing_malformed_or_wrong_author_manifest_not_accepted(self):
        self.f.cli.root["description"] = MARKER
        self.assertFalse(self.read()["delivery_complete"])
        manifest(self.f, [])
        self.f.cli.comments[0]["author_type"] = "member"
        self.assertFalse(self.read()["delivery_complete"])
        manifest(self.f, ["../private.txt"])
        self.assertFalse(self.read()["delivery_complete"])
        manifest(self.f, ["x.png", "x.png"])
        self.assertFalse(self.read()["delivery_complete"])

    def test_new_text_task_requires_explicit_empty_list(self):
        self.f.cli.root["attachments"] = []
        manifest(self.f, [])
        self.assertTrue(self.read()["delivery_complete"])
        self.f.cli.comments[0]["content"] = "做完了"
        self.assertFalse(self.read()["delivery_complete"])

    def test_only_manifest_files_are_selected_and_no_quiet_wait(self):
        from datetime import datetime, timezone
        manifest(self.f, [])
        self.f.cli.root["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        result = self.read()
        self.assertTrue(result["delivery_complete"])
        self.assertEqual(result["images"], [])
        self.assertNotIn("settled", result)

    def test_generic_document_download_verified_without_execution(self):
        self.f.cli.data = b"%PDF-test-data"
        self.f.cli.attachment.update(filename="report.pdf", content_type="application/pdf", size_bytes=len(self.f.cli.data))
        manifest(self.f, ["report.pdf"])
        result = self.read()
        self.assertTrue(result["delivery_complete"])
        self.assertEqual(result["images"], [])
        self.assertEqual(result["files"][0]["filename"], "report.pdf")
        checked = self.f.call(operation="verify_file")
        self.assertTrue(checked["verified"])
        self.assertNotIn("image_base64", checked)


class PollingTests(unittest.TestCase):
    def test_defaults_overrides_scope_and_restart(self):
        with TemporaryDirectory() as folder:
            store = PollingSettings(Path(folder))
            job = dict(user_id="u", session_id="s", task_type="image")
            self.assertEqual(store.effective(job), dict(poll_seconds=20, max_failures=5))
            store.update("u", "s", "", dict(poll_seconds=30))
            store.update("u", "s", "image", dict(max_failures=8))
            store = PollingSettings(Path(folder))
            self.assertEqual(store.effective(job), dict(poll_seconds=30, max_failures=8))
            self.assertEqual(store.effective({**job, "session_id": "other"}), dict(poll_seconds=20, max_failures=5))
            self.assertEqual(store.effective({**job, "polling_override": {"poll_seconds": 10}})["poll_seconds"], 10)

    def test_bounds(self):
        for args in ((0, 0), (1, 0), (601, 0), (0, 51), (True, 0), (0, -1)):
            with self.assertRaises(ValueError):
                validate_changes(*args)
        self.assertEqual(validate_changes(10, 8), {"poll_seconds": 10, "max_failures": 8})


class UploadTests(unittest.TestCase):
    def test_checksums_names_and_size(self):
        item = upload()
        self.assertEqual(decode_attachments([item], BridgeError), [(item["filename"], b"file contents")])
        for changed in ({"filename": "../secret"}, {"filename": "CON.txt"}, {"filename": "body.md"},
                        {"filename": "con."}, {"sha256": "wrong"}, {"size_bytes": 1}, {"base64": "!"}):
            with self.subTest(changed=changed), self.assertRaises(BridgeError):
                decode_attachments([{**item, **changed}], BridgeError)
        with self.assertRaises(BridgeError):
            decode_attachments([item, item], BridgeError)

    def test_cli_uploads_original_bytes_and_retry_is_idempotent(self):
        f = dialogue_fixtures.DialogueTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = f.cli.run
        captured = []
        paths = []
        def run(args):
            for index, flag in enumerate(args):
                if flag == "--attachment":
                    path = Path(args[index + 1])
                    captured.append(path.read_bytes())
                    paths.append(path)
            return original(args)
        f.cli.run = run
        result = f.call(attachments=[upload()])
        again = f.call(attachments=[upload()])
        self.assertEqual(result["dialogue_id"], again["dialogue_id"])
        self.assertEqual(captured, [b"file contents"])
        self.assertEqual(f.cli.created, 1)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertIn("01-中文文档.txt", f.cli.bodies[0])

    def test_large_attachment_crosses_real_http_without_exposing_bytes_in_result(self):
        f = dialogue_fixtures.DialogueTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        server = Server(("127.0.0.1", 0), f.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        token = Path(f.temp.name) / "http.token"
        token.write_text(f.settings.token)
        try:
            response = request(f"http://127.0.0.1:{server.server_port}", str(token),
                               {**f.body, "attachments": [upload(b"large file" * 12000)]})
            self.assertEqual(response["state"], "waiting")
            self.assertNotIn("base64", response)
            self.assertEqual(f.cli.created, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class IncomingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.row = {"message_id": "msg", "platform": "qq", "session_id": "room", "account_id": "bot",
                    "message_info": {"user_info": {"user_id": "u"}}, "raw_message": []}
        self.plugin = NS(origin=AsyncMock(), ctx=NS(paths=NS(data_dir=Path(temp.name)),
                      message=NS(get_by_id=AsyncMock(return_value=self.row)), files=NS(download=AsyncMock())))
        self.payload = dict(user_id="u", session_id="room", message_id="msg")

    async def test_image_bytes_from_host_not_model_url(self):
        data = artifact_fixtures.png()
        self.row["raw_message"] = [{"type": "image", "binary_data_base64": base64.b64encode(data).decode(), "data": "untrusted-url"}]
        result = await collect(self.plugin, self.payload, [])
        self.assertEqual(base64.b64decode(result[0]["base64"]), data)
        self.assertTrue(result[0]["filename"].endswith(".png"))

    async def test_file_uses_scoped_sdk_download_and_cleans_temp(self):
        self.row["raw_message"] = [{"type": "file", "data": {"file_ref": "host-ref", "name": "../合同.pdf", "size": 4}}]
        async def download(ref, path):
            self.assertEqual(ref, "host-ref")
            path.write_bytes(b"data")
        self.plugin.ctx.files.download.side_effect = download
        result = await collect(self.plugin, self.payload, [])
        self.assertNotIn("/", result[0]["filename"])
        self.assertEqual(base64.b64decode(result[0]["base64"]), b"data")
        self.assertEqual(list(self.plugin.ctx.paths.data_dir.iterdir()), [])

    async def test_cross_user_and_missing_file_ref_rejected(self):
        self.row["message_info"]["user_info"]["user_id"] = "other"
        with self.assertRaises(ClientError):
            await collect(self.plugin, self.payload, [])
        self.row["message_info"]["user_info"]["user_id"] = "u"
        self.row["raw_message"] = [{"type": "file", "data": {"url": "http://localhost/private", "name": "x"}}]
        with self.assertRaises(ClientError):
            await collect(self.plugin, self.payload, [])
        self.plugin.ctx.files.download.assert_not_called()
