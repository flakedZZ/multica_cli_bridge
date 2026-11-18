import asyncio
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from plugins.local_multica.qq_files import QQFiles, decode_file, API


def configure_api(p, kind="group"):
    p.ctx.chat = NS(get_all_streams=AsyncMock(return_value=[{
        "session_id": "room", "platform": "qq", "user_id": "12345", "group_id": "67890",
        "chat_type": kind, "account_id": "90000"}]))
    async def call(name, version, action_name, params):
        assert name == API and version == "1"
        if action_name == "get_version_info":
            data = {"app_name": "SnowLuma", "app_version": "2.4.2-node"}
        elif action_name == "get_login_info":
            data = {"user_id": 90000}
        else:
            assert params["file"].startswith("base64://") and params["upload_file"] is True
            data = {"file_id": "qq-file-123"}
        return {"status": "ok", "retcode": 0, "data": data}
    p.ctx.api = NS(call=AsyncMock(side_effect=call))


class QQFileTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = NS(ctx=NS(paths=NS(data_dir=Path(self.temp.name)), logger=Mock()),
                    config=NS(plugin=NS(enabled=True), bridge=NS(allowed_users=["12345"])))
        configure_api(self.p)
        self.job = {"session_id": "room", "user_id": "12345"}
        self.item = {"attachment_id": "attachment", "filename": "游戏.html", "size_bytes": 6, "content_type": "text/html"}
        self.data = b"<html>"
        self.q = QQFiles(self.p)

    def uploads(self):
        return [c for c in self.p.ctx.api.call.call_args_list if c.kwargs["action_name"].startswith("upload_")]

    async def test_group_bytes_and_name_success_survive_restart_without_duplicate(self):
        receipt = await self.q.send("key", self.job, self.item, self.data)
        self.assertEqual(receipt["action"], "upload_group_file")
        params = self.uploads()[0].kwargs["params"]
        self.assertEqual(params["group_id"], 67890)
        self.assertEqual(params["name"], "游戏.html")
        self.assertEqual(base64.b64decode(params["file"][9:]), self.data)
        self.q = QQFiles(self.p)
        self.assertEqual(await self.q.send("key", self.job, self.item, self.data), receipt)
        self.assertEqual(len(self.uploads()), 1)
        self.q.finish("key", receipt)  # same callback is idempotent
        self.q.finish("key", error="late_timeout")
        self.assertEqual(self.q.get("key")["state"], "sent")

    async def test_private_uses_original_user(self):
        configure_api(self.p, "private")
        r = await self.q.send("key", self.job, self.item, self.data)
        self.assertEqual(r["action"], "upload_private_file")
        self.assertEqual(self.uploads()[0].kwargs["params"]["user_id"], 12345)

    async def test_unknown_and_explicit_failed_upload_never_replayed(self):
        original = self.p.ctx.api.call.side_effect
        for n, response in enumerate([{"status": "failed", "retcode": 1200}, {"status": "ok", "retcode": 0, "data": {}}, TimeoutError()]):
            async def call(name, version, action_name, params):
                if action_name.startswith("upload_"):
                    if isinstance(response, Exception):
                        raise response
                    return response
                return await original(name, version, action_name, params)
            self.p.ctx.api.call.side_effect = call
            self.assertIsNone(await self.q.send(str(n), self.job, self.item, self.data))
            self.q = QQFiles(self.p)
            self.assertIsNone(await self.q.send(str(n), self.job, self.item, self.data))
            self.assertEqual(self.q.get(str(n))["state"], "uncertain")
        self.assertEqual(len(self.uploads()), 3)

    async def test_pending_process_crash_is_not_replayed(self):
        original = self.p.ctx.api.call.side_effect
        async def call(name, version, action_name, params):
            if action_name.startswith("upload_"):
                raise asyncio.CancelledError()
            return await original(name, version, action_name, params)
        self.p.ctx.api.call.side_effect = call
        with self.assertRaises(asyncio.CancelledError):
            await self.q.send("key", self.job, self.item, self.data)
        self.assertEqual(self.q.get("key")["state"], "pending")
        self.assertIsNone(await QQFiles(self.p).send("key", self.job, self.item, self.data))
        self.assertEqual(len(self.uploads()), 1)

    async def test_invisible_local_path_and_tampered_bytes_fail_before_upload(self):
        for data in ["/MaiMBot/cache/file.html", b"short"]:
            with self.assertRaises(ValueError):
                await self.q.send("key", self.job, self.item, data)
        result = {**self.item, "sha256": hashlib.sha256(self.data).hexdigest(), "file_base64": base64.b64encode(self.data).decode()}
        self.assertEqual(decode_file(self.item, result), self.data)
        for change in [{"filename": "other.html"}, {"content_type": "image/png"}, {"sha256": "0"*64}, {"file_base64": "!"}]:
            with self.assertRaises(ValueError):
                decode_file(self.item, {**result, **change})
        self.assertEqual(self.uploads(), [])

    async def test_read_only_preflight_failure_is_recoverable_without_claim(self):
        self.p.ctx.chat.get_all_streams.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.q.send("key", self.job, self.item, self.data)
        self.assertIsNone(self.q.get("key"))
        self.p.ctx.chat.get_all_streams.side_effect = None
        self.assertTrue(await self.q.send("key", self.job, self.item, self.data))

    async def test_wrong_account_or_session_or_unsupported_protocol_blocks_send(self):
        original = self.p.ctx.chat.get_all_streams.return_value
        for change in [{"session_id": "elsewhere"}, {"account_id": "99999"}, {"chat_type": "private", "user_id": "99999"}]:
            self.p.ctx.chat.get_all_streams.return_value = [{**original[0], **change}]
            with self.assertRaises(ValueError):
                await self.q.send("key", self.job, self.item, self.data)
        self.p.ctx.chat.get_all_streams.return_value = original
        self.p.ctx.api.call.return_value = {"status": "ok", "retcode": 0, "data": {"app_name": "NapCat", "app_version": "9"}}
        self.p.ctx.api.call.side_effect = None
        with self.assertRaises(ValueError):
            await self.q.send("key", self.job, self.item, self.data)
        self.assertEqual(self.uploads(), [])
