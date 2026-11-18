"""Fault injection across the receiver, HTTP acknowledgement and two CLI writes."""
import asyncio
import copy
import hashlib
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import unittest

import test_artifacts as fixtures
from test_transfer import manifest
from bridge.server import BridgeError, Service, Server
from plugins.local_multica.client import request
from plugins.local_multica.completion import Completion

PARENT = "00000000-0000-4000-8000-000000000005"
ROOT, CHILD = fixtures.ROOT, fixtures.CHILD


class BusinessFixture:
    def __init__(self, test):
        self.f = fixtures.ArtifactTests()
        self.f.setUp()
        test.addCleanup(self.f.doCleanups)
        self.cli = self.f.cli
        self.cli.root.update(parent_issue_id=PARENT, revision=4)
        self.parent = dict(id=PARENT, project_id=fixtures.PROJECT, status="in_review", revision=2)
        self.siblings = [ROOT]
        self.active = []
        self.writes = []
        self.fail_before = None
        self.fail_after = None
        self.extra = {}
        original = self.cli.run
        def run(args):
            if args[:2] == ["issue", "get"]:
                return self.parent if args[2] == PARENT else self.extra[args[2]] if args[2] in self.extra else original(args)
            if args[:2] == ["issue", "children"] and args[2] == PARENT:
                return {"unstaged": [{"id": i} for i in self.siblings], "stages": [], "total": len(self.siblings)}
            if args[:2] == ["issue", "runs"]:
                return self.active
            if args[:2] == ["issue", "status"]:
                if self.fail_before == args[2]:
                    self.fail_before = None
                    raise BridgeError("temporary network failure", 502)
                target = self.parent if args[2] == PARENT else self.extra.get(args[2], self.cli.root)
                self.writes.append(args)
                target["status"] = args[3]
                target["revision"] += 1
                if self.fail_after == args[2]:
                    self.fail_after = None
                    raise BridgeError("lost CLI response", 502)
                return copy.deepcopy(target)
            return original(args)
        self.cli.run = run
        manifest(self.f, [self.cli.attachment["filename"]])
        with self.f.service.connect() as db:
            db.execute("INSERT INTO execution_links VALUES (?,?)", ("r", PARENT))
            db.execute("INSERT INTO dialogues (id,user_id,session_id,discussion_id,current_turn,state,execution_id) VALUES (?,?,?,?,?,?,?)",
                       ("d", "12345", "room", PARENT, "turn", "confirmed", ROOT))

    def call(self, **changes):
        return self.f.call(**changes)

    def restart(self):
        self.f.service = Service(self.f.settings, self.cli)


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b = BusinessFixture(self)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = dict(reference="DEMO-12", platform="qq", user_id="12345", session_id="room", deadline=0, notify=True)
        self.key = "room:12345:DEMO-12"
        self.p = NS(ctx=NS(paths=NS(data_dir=Path(self.temp.name)), logger=Mock(),
                           send=NS(text=AsyncMock(return_value={"sent": True, "message_id": "qq-text"}),
                                   image=AsyncMock(return_value={"sent": True, "message_id": "qq-image"}))),
                    config=NS(plugin=NS(enabled=True), closure=NS(enabled=True),
                              routing=NS(auto_return_images=True), bridge=NS(allowed_users=["12345"])),
                    state_lock=asyncio.Lock(), jobs={self.key: self.job}, save_jobs=AsyncMock(), delivery_check=AsyncMock())
        async def rpc(body):
            return self.b.f.service.handle(body)
        self.p.rpc = AsyncMock(side_effect=rpc)
        self.c = Completion(self.p)
        from test_qq_files import configure_api
        configure_api(self.p)

    async def deliver(self):
        await self.c.deliver(self.key, self.job)

    def ack(self):
        record = self.c.get(self.key)
        return {**self.job, "operation": "delivery_ack", "token": record["token"], "evidence": json.loads(record["evidence"])}

    async def test_normal_complete_parent_child_with_strong_receipts_and_no_question(self):
        await self.deliver()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT, PARENT])
        self.assertTrue(all("--no-start" in w for w in self.b.writes))
        self.assertEqual(self.b.cli.root["status"], "done")
        self.assertEqual(self.b.parent["status"], "done")
        self.assertEqual(self.b.cli.root["parent_issue_id"], PARENT)
        self.p.ctx.send.image.assert_awaited_once()
        self.p.ctx.send.text.assert_awaited_once()
        self.assertNotIn("完成了吗", self.p.ctx.send.text.call_args.args[0])
        self.assertTrue(self.c.get(self.key)["closed"])

    async def test_repeated_ack_and_restart_do_not_repeat_sends_or_writes(self):
        await self.deliver()
        for _ in range(3):
            self.b.restart()
            self.c = Completion(self.p)
            self.assertTrue(self.b.f.service.handle(self.ack())["closed"])
            await self.deliver()
        self.assertEqual(len(self.b.writes), 2)
        self.p.ctx.send.image.assert_awaited_once()
        self.p.ctx.send.text.assert_awaited_once()

    async def test_busy_prepare_and_attachment_do_not_count_failures_or_close(self):
        from plugins.local_multica.client import BridgeBusyError
        original = self.p.rpc.side_effect
        for operation in ('delivery_prepare', 'image'):
            async def busy(body):
                if body['operation'] == operation:
                    raise BridgeBusyError('busy')
                return await original(body)
            self.p.rpc.side_effect = busy
            await self.deliver()
            self.p.delivery_check.assert_not_called()
            self.p.ctx.send.text.assert_not_called()
            self.p.ctx.send.image.assert_not_called()
            self.assertEqual(self.b.writes, [])
        self.p.rpc.side_effect = original
        await self.deliver()
        self.assertEqual(len(self.b.writes), 2)

    async def test_missing_false_and_queue_receipts_never_close_or_resend(self):
        for index, result in enumerate((None, {"success": True}, {"sent": True}, {"sent": True, "message_id": None}, {"sent": False})):
            with self.subTest(result=result):
                self.p.ctx.paths.data_dir = Path(self.temp.name) / str(index)
                self.c = Completion(self.p)
                self.p.ctx.send.image.return_value = result
                await self.deliver()
                await self.deliver()
                self.assertEqual(self.b.writes, [])
                self.assertIsNone(self.c.get(self.key)["evidence"])
        self.assertEqual(self.p.ctx.send.image.await_count, 5)
        self.p.ctx.send.text.assert_not_called()

    async def test_uncertain_send_exception_survives_restart_without_replay(self):
        self.p.ctx.send.image.side_effect = TimeoutError
        await self.deliver()
        self.c = Completion(self.p)
        self.p.ctx.send.image.side_effect = None
        await self.deliver()
        self.p.ctx.send.image.assert_awaited_once()
        self.assertEqual(self.b.writes, [])

    async def test_sent_image_without_final_text_receipt_keeps_both_open(self):
        self.p.ctx.send.text.return_value = {"sent": False}
        await self.deliver()
        await self.deliver()
        self.assertEqual(self.b.writes, [])
        self.p.ctx.send.image.assert_awaited_once()
        self.p.ctx.send.text.assert_awaited_once()

    async def test_download_failure_retries_safely(self):
        download = self.b.cli.download
        self.b.cli.download = Mock(side_effect=TimeoutError)
        await self.deliver()
        self.assertEqual(self.b.writes, [])
        self.p.ctx.send.image.assert_not_called()
        self.b.cli.download = download
        await self.deliver()
        self.assertEqual(len(self.b.writes), 2)

    async def test_no_manifest_and_running_task_do_not_prepare(self):
        self.b.cli.comments = []
        await self.deliver()
        self.assertIsNone(self.c.get(self.key))
        self.b.cli.root["status"] = "in_progress"
        await self.deliver()
        self.assertEqual(self.b.writes, [])

    async def test_partial_close_and_lost_cli_response_recover(self):
        self.b.fail_after = ROOT
        with self.assertRaises(BridgeError):
            await self.deliver()
        self.assertEqual(self.b.cli.root["status"], "done")
        self.assertEqual(self.b.parent["status"], "in_review")
        self.assertIsNotNone(self.c.get(self.key)["evidence"])
        self.b.restart()
        self.c = Completion(self.p)
        self.p.jobs.clear()  # Outbox must not depend on job TTL or pending JSON.
        await self.c.poll()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT, PARENT])
        self.assertTrue(self.c.get(self.key)["closed"])
        self.p.ctx.send.image.assert_awaited_once()

    async def test_failed_parent_write_recovers(self):
        self.b.fail_before = PARENT
        with self.assertRaises(BridgeError):
            await self.deliver()
        await self.c.poll()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT, PARENT])

    async def test_lost_ack_response_retries_only_ack(self):
        original = self.p.rpc.side_effect
        async def lost(body):
            result = await original(body)
            if body["operation"] == "delivery_ack":
                raise TimeoutError
            return result
        self.p.rpc.side_effect = lost
        with self.assertRaises(TimeoutError):
            await self.deliver()
        self.p.rpc.side_effect = original
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertTrue(self.c.get(self.key)["closed"])
        self.assertEqual(len(self.b.writes), 2)
        self.p.ctx.send.text.assert_awaited_once()

    async def test_crash_after_inbox_commit_before_ack_recovers(self):
        self.c.acknowledge = AsyncMock(side_effect=RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            await self.deliver()
        self.assertEqual(self.b.writes, [])
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertEqual(len(self.b.writes), 2)
        self.p.ctx.send.text.assert_awaited_once()

    async def test_other_independent_sibling_even_manually_done_blocks_parent(self):
        self.b.siblings.append(CHILD)
        self.b.extra[CHILD] = dict(id=CHILD, project_id=fixtures.PROJECT, parent_issue_id=PARENT, status="in_review")
        await self.deliver()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT])
        self.b.extra[CHILD]["status"] = "done"
        await self.c.poll()
        self.assertEqual(self.b.parent["status"], "in_review")
        self.b.extra[CHILD]["status"] = "cancelled"
        await self.c.poll()
        self.assertEqual(self.b.parent["status"], "done")

    async def test_parent_active_run_and_pending_creation_block_closure(self):
        self.b.active = [{"status": "running"}]
        await self.deliver()
        self.assertEqual(self.b.parent["status"], "in_review")
        self.b.active = []
        with self.b.f.service.connect() as db:
            db.execute("INSERT INTO requests VALUES ('pending','f','12345','room','title',0,NULL,NULL,'pending')")
            db.execute("INSERT INTO execution_links VALUES ('pending',?)", (PARENT,))
        await self.c.poll()
        self.assertEqual(self.b.parent["status"], "in_review")

    async def test_two_independent_executions_close_parent_only_after_both_receipts(self):
        self.b.cli.root["attachments"] = []
        manifest(self.b.f, [])
        self.b.extra[CHILD] = {**self.b.cli.root, "id": CHILD}
        self.b.siblings.append(CHILD)
        with self.b.f.service.connect() as db:
            db.execute("INSERT INTO requests VALUES ('second','f','12345','room','second',0,?,'DEMO-13','submitted')", (CHILD,))
            db.execute("INSERT INTO execution_links VALUES ('second',?)", (PARENT,))
        await self.deliver()
        self.assertEqual(self.b.parent["status"], "in_review")
        first_key = self.key
        second_job = {**self.job, "reference": "DEMO-13"}
        await self.c.deliver("room:12345:DEMO-13", second_job)
        await self.c.poll()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT, CHILD, PARENT])
        self.assertTrue(self.c.get(first_key)["closed"])

    async def test_new_result_after_child_closed_is_not_covered_by_old_receipt(self):
        self.b.active = [{"status": "running"}]
        await self.deliver()
        self.b.active = []
        self.b.cli.comments[0]["content"] += "\n新的尚未送达成果"
        await self.c.poll()
        self.assertEqual(self.b.parent["status"], "in_review")
        self.assertFalse(self.c.get(self.key)["closed"])

    async def test_real_http_snapshot_file_and_ack_roundtrip(self):
        self.b.cli.data = b"document" * 20000
        self.b.cli.attachment.update(filename="report.pdf", content_type="application/pdf", size_bytes=len(self.b.cli.data))
        manifest(self.b.f, ["report.pdf"])
        token_file = Path(self.temp.name) / "bridge.token"
        token_file.write_text(self.b.f.settings.token)
        server = Server(("127.0.0.1", 0), self.b.f.service)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            async def http(body):
                return await asyncio.to_thread(request, f"http://127.0.0.1:{server.server_port}", str(token_file), body)
            self.p.rpc.side_effect = http
            await self.deliver()
            self.assertEqual([w[2] for w in self.b.writes], [ROOT, PARENT])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    async def test_old_revision_or_changed_parent_rejects_ack(self):
        self.p.config.closure.enabled = False
        await self.deliver()
        self.b.cli.root["revision"] += 1
        with self.assertRaises(BridgeError):
            self.b.f.service.handle(self.ack())
        self.b.cli.root["revision"] -= 1
        self.b.cli.root["parent_issue_id"] = CHILD
        with self.assertRaises(BridgeError):
            self.b.f.service.handle(self.ack())
        self.assertEqual(self.b.writes, [])

    async def test_owner_session_and_tampered_receipts_rejected(self):
        self.p.config.closure.enabled = False
        await self.deliver()
        for changes in ({"user_id": "67890"}, {"session_id": "elsewhere"}, {"evidence": {}}, {"token": "invented"}):
            with self.assertRaises(BridgeError):
                self.b.f.service.handle({**self.ack(), **changes})
        ack = self.ack()
        ack["evidence"]["attachments"] = {}
        with self.assertRaises(BridgeError):
            self.b.f.service.handle(ack)
        self.assertEqual(self.b.writes, [])

    async def test_legacy_database_does_not_acquire_auto_close_authority(self):
        with self.b.f.service.connect() as db:
            db.execute("DELETE FROM execution_links")
        self.assertEqual(self.b.call(operation="artifacts")["closure_protocol"], 0)
        with self.assertRaises(BridgeError):
            self.b.call(operation="close", reason="silence_timeout", expected_revision=4)
        await self.deliver()
        self.assertEqual(self.b.writes, [])

    async def test_full_long_text_received_and_sent_without_truncation(self):
        self.b.cli.root["attachments"] = []
        manifest(self.b.f, [])
        self.b.cli.comments[0]["content"] = "首段" + "长正文" * 1200 + self.b.cli.comments[0]["content"] + "尾段"
        await self.deliver()
        sent = "".join(c.args[0] for c in self.p.ctx.send.text.call_args_list)
        self.assertIn(self.b.cli.comments[0]["content"], sent)
        self.p.ctx.send.image.assert_not_called()
        self.assertEqual(len(self.b.writes), 2)

    async def test_generic_file_bytes_reach_maibot_and_are_hashed_before_ack(self):
        self.b.cli.data = b"document" * 20000
        self.b.cli.attachment.update(filename="report.pdf", content_type="application/pdf", size_bytes=len(self.b.cli.data))
        manifest(self.b.f, ["report.pdf"])
        await self.deliver()
        record = self.c.get(self.key)
        sha, data = self.c.file(record["token"], self.b.cli.attachment["id"])
        self.assertEqual(data, self.b.cli.data)
        self.assertEqual(sha, hashlib.sha256(data).hexdigest())
        self.assertIn("report.pdf", self.p.ctx.send.text.call_args.args[0])
        self.p.ctx.send.image.assert_not_called()
        self.assertEqual(len(self.b.writes), 2)

    async def test_disabled_or_revoked_receiver_never_acknowledges(self):
        self.p.config.closure.enabled = False
        await self.deliver()
        self.assertEqual(self.b.writes, [])
        self.p.config.closure.enabled = True
        self.p.config.bridge.allowed_users = []
        await self.c.poll()
        self.assertEqual(self.b.writes, [])

    async def test_old_acceptance_timer_is_not_loaded(self):
        path = self.p.ctx.paths.data_dir / "acceptance_pending.json"
        path.write_text(json.dumps({"old": {"phase": "waiting", "due": 0}}))
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertEqual(self.b.writes, [])

    def use_html(self):
        self.b.cli.data = b"<html>original</html>"
        self.b.cli.attachment.update(filename="game.html", content_type="text/html", size_bytes=len(self.b.cli.data))
        manifest(self.b.f, ["game.html"])

    async def test_file_upload_timeout_never_closes_or_repeats(self):
        self.use_html()
        original = self.p.ctx.api.call.side_effect
        async def call(name, version, action_name, params):
            if action_name.startswith('upload_'):
                raise TimeoutError
            return await original(name, version, action_name, params)
        self.p.ctx.api.call.side_effect = call
        await self.deliver()
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertEqual(self.b.writes, [])
        self.p.ctx.send.text.assert_not_called()
        self.assertEqual(sum(c.kwargs['action_name'].startswith('upload_') for c in self.p.ctx.api.call.call_args_list), 1)

    async def test_file_parent_status_failure_recovers_without_resending_file(self):
        self.use_html()
        self.b.fail_after = ROOT
        with self.assertRaises(BridgeError):
            await self.deliver()
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT, PARENT])
        self.assertEqual(sum(c.kwargs['action_name'].startswith('upload_') for c in self.p.ctx.api.call.call_args_list), 1)

    async def test_download_only_file_evidence_cannot_close(self):
        self.use_html()
        self.p.config.closure.enabled = False
        await self.deliver()
        ack = self.ack()
        aid = self.b.cli.attachment['id']
        receipt = ack['evidence']['attachments'][aid]['qq_file']
        self.assertEqual(receipt['file_id'], 'qq-file-123')
        del ack['evidence']['attachments'][aid]['qq_file']
        with self.assertRaises(BridgeError):
            self.b.f.service.handle(ack)
        self.assertEqual(self.b.writes, [])

    async def test_unfinished_old_file_evidence_upgrades_without_text_replay(self):
        self.use_html()
        self.p.config.closure.enabled = False
        await self.deliver()
        ack = self.ack()
        aid = self.b.cli.attachment['id']
        del ack['evidence']['attachments'][aid]['qq_file']
        with self.c.store.connect() as db:
            db.execute('UPDATE received_results SET evidence=?', (json.dumps(ack['evidence']),))
        self.p.config.closure.enabled = True
        self.c = Completion(self.p)
        self.assertIsNone(self.c.get(self.key)['evidence'])
        await self.c.poll()
        self.assertEqual(len(self.b.writes), 2)
        self.p.ctx.send.text.assert_awaited_once()

    async def test_partial_legacy_ack_accepts_only_added_file_confirmation(self):
        self.use_html()
        self.b.fail_before = PARENT
        with self.assertRaises(BridgeError):
            await self.deliver()
        ack = self.ack()
        old = copy.deepcopy(ack['evidence'])
        del old['attachments'][self.b.cli.attachment['id']]['qq_file']
        with self.b.f.service.connect() as db:
            db.execute('UPDATE delivery_acks SET evidence=? WHERE token=?', (json.dumps(old),ack['token']))
        with self.c.store.connect() as db:
            db.execute('UPDATE received_results SET evidence=?', (json.dumps(old),))
        altered = copy.deepcopy(ack)
        altered['evidence']['text_messages']=['replacement-not-original']
        with self.assertRaises(BridgeError):
            self.b.f.service.handle(altered)
        self.c = Completion(self.p)
        await self.c.poll()
        self.assertEqual([w[2] for w in self.b.writes], [ROOT,PARENT])
        self.p.ctx.send.text.assert_awaited_once()
