"""Run in MaiBot's real Python environment; no bot traffic or live plugin load."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import importlib.util
import json
import time
import unittest


HAS_SDK = importlib.util.find_spec("maibot_sdk") is not None


@unittest.skipUnless(HAS_SDK, "requires the installed MaiBot SDK")
class SdkTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        from plugins.local_multica.plugin import Config, create_plugin
        plugin = create_plugin()
        plugin._plugin_config_instance = Config()
        plugin._plugin_config_instance.bridge.allowed_users = ["12345"]
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        plugin._ctx = SimpleNamespace(send=SimpleNamespace(text=AsyncMock(return_value={"sent": True, "message_id": "text-id"}),
                                                           image=AsyncMock(return_value={"sent": True, "message_id": "image-id"})), paths=SimpleNamespace(data_dir=Path(directory.name)),
                                      message=SimpleNamespace(get_by_id=AsyncMock(), get_recent=AsyncMock()),
                                      maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=AsyncMock(return_value={"success": True}))))
        return plugin

    async def test_component_and_config(self):
        from maibot_sdk.components import collect_components
        from maibot_sdk.config import build_plugin_default_config
        from plugins.local_multica.plugin import Config
        plugin = self.plugin()
        components = collect_components(plugin)
        self.assertEqual({c["name"] for c in components}, {"multica_command", "multica_mika", "multica_routing"})
        parameters = next(c for c in components if c["name"] == "multica_mika")["metadata"]["parameters"]
        self.assertEqual([p["name"] for p in parameters if p["required"]], ["action"])
        self.assertEqual(build_plugin_default_config(Config)["bridge"]["allowed_users"], [])

    async def test_live_config_callback_is_implemented_and_rechecks_allowlist(self):
        plugin = self.plugin()
        self.assertIn("on_config_update", type(plugin).__dict__)
        plugin._plugin_config_instance.bridge.allowed_users = []
        await plugin.on_config_update("plugin", {}, "1.1.1")
        with patch("plugins.local_multica.plugin.request") as call:
            result = await plugin.multica_command(stream_id="room", user_id="12345", platform="qq", matched_groups={"body": "列表"})
            self.assertFalse(result[0])
            call.assert_not_called()

    async def test_real_command_with_host_identity(self):
        plugin = self.plugin()
        with patch("plugins.local_multica.plugin.request", return_value={"text": "已提交：DEMO-12"}) as send:
            result = await plugin.multica_command(
                stream_id="real-session", user_id="12345", platform="qq",
                message={"message_id": "real-message"}, matched_groups={"body": "提交 中文 | 需求"},
            )
            self.assertTrue(result[0])
            self.assertEqual(send.call_args.args[2]["session_id"], "real-session")
            self.assertEqual(send.call_args.args[2]["message_id"], "real-message")
            plugin.ctx.send.text.assert_awaited_once_with("已提交：DEMO-12", "real-session")

    async def test_unauthorized_or_missing_message_cannot_call_bridge(self):
        plugin = self.plugin()
        with patch("plugins.local_multica.plugin.request") as request:
            for user, message in (("666", {"message_id": "msg"}), ("12345", {})):
                result = await plugin.multica_command(stream_id="session", user_id=user, platform="qq",
                                                      message=message, matched_groups={"body": "提交 标题 | 详情"})
                self.assertFalse(result[0])
            request.assert_not_called()

    async def test_manifest_against_host_validator(self):
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
        validator = ManifestValidator()
        path = Path(__file__).parents[1] / "plugins/local_multica/_manifest.json"
        self.assertTrue(validator.validate(json.loads(path.read_text(encoding="utf-8"))), validator.errors)

    def origin(self, **changes):
        return {"session_id": "room", "platform": "qq", "message_id": "msg", "timestamp": str(time.time()),
                "account_id": "99999", "message_info": {"user_info": {"user_id": "12345"}}, **changes}

    async def test_natural_language_discussion_uses_verified_origin(self):
        plugin = self.plugin()
        plugin.ctx.message.get_by_id.return_value = self.origin()
        response = {"dialogue_id": "discussion", "state": "waiting", "turn_id": "round1", "text": "处理中"}
        with patch("plugins.local_multica.plugin.request", return_value=response) as call:
            result = await plugin.multica_mika("discuss", request_message_id="msg", message="和规划Agent讨论如何做提醒工具", stream_id="room")
            self.assertTrue(result["success"])
            self.assertEqual(call.call_args.args[2]["user_id"], "12345")
            self.assertEqual(plugin.pending["discussion"]["turn_id"], "round1")
            self.assertTrue((plugin.ctx.paths.data_dir / "mika_pending.json").exists())
            plugin.ctx.send.text.assert_not_called()

    async def test_natural_language_rejects_cross_session_or_expired_source(self):
        plugin = self.plugin()
        for origin in (self.origin(session_id="other"), self.origin(timestamp="1"),
                       self.origin(message_info={"user_info": {"user_id": "666"}}), self.origin(account_id="12345")):
            plugin.ctx.message.get_by_id.return_value = origin
            with patch("plugins.local_multica.plugin.request") as call:
                result = await plugin.multica_mika("discuss", request_message_id="msg", message="开工", stream_id="room")
                self.assertFalse(result["success"])
                call.assert_not_called()

    async def test_mika_reply_wakes_bot_once_without_submitting(self):
        plugin = self.plugin()
        await plugin.track({"dialogue_id": "d", "state": "waiting", "turn_id": "round1"},
                           {"session_id": "room", "user_id": "12345", "message_id": "msg"})
        pending = plugin.pending["d"]
        with patch("plugins.local_multica.plugin.request", return_value={"state": "proposal", "text": "方案", "version": "v1"}) as call:
            await plugin.poll_one("d", pending)
            await plugin.poll_one("d", pending)
            self.assertEqual(call.call_args.args[2]["operation"], "plan")
        plugin.ctx.maisaka.proactive.trigger.assert_awaited_once()
        self.assertIn("由你确认", plugin.ctx.maisaka.proactive.trigger.call_args.args[1])
        self.assertEqual(plugin.pending, {})

    async def test_confirm_without_dialogue_id_tracks_delivery_and_replays_once(self):
        import test_dialogue as fixtures
        f = fixtures.DialogueTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        plan = f.proposal()
        plugin = self.plugin()
        plugin.ctx.message.get_by_id.return_value = self.origin()
        with patch("plugins.local_multica.plugin.request",
                   side_effect=lambda _url, _token, body: f.service.handle(body)):
            for _ in range(2):
                result = await plugin.multica_mika(
                    "confirm", request_message_id="msg", version=plan["version"],
                    review="符合原需求，可执行", task_type="image", stream_id="room")
                self.assertTrue(result["success"], result)
                self.assertEqual(json.loads(result["content"])["reference"], "DEMO-21")
                f.service = fixtures.Service(f.settings, f.cli)
        self.assertEqual(f.cli.created, 2)
        self.assertEqual(len(plugin.jobs), 1)
        job = next(iter(plugin.jobs.values()))
        self.assertEqual((job["reference"], job["task_type"], job["notify"]), ("DEMO-21", "image", True))
        saved = json.loads((plugin.ctx.paths.data_dir / "execution_pending.json").read_text())
        self.assertEqual(saved, plugin.jobs)
        plugin.ctx.send.image.assert_not_awaited()

    async def test_new_round_cannot_be_woken_by_old_poll(self):
        plugin = self.plugin()
        payload = {"session_id": "room", "user_id": "12345", "message_id": "msg"}
        await plugin.track({"dialogue_id": "d", "state": "waiting", "turn_id": "round1"}, payload)
        old = plugin.pending["d"]
        await plugin.track({"dialogue_id": "d", "state": "waiting", "turn_id": "round2"}, payload)
        with patch("plugins.local_multica.plugin.request", return_value={"state": "proposal", "text": "旧方案", "version": "old"}):
            await plugin.poll_one("d", old)
        plugin.ctx.maisaka.proactive.trigger.assert_not_called()
        self.assertEqual(plugin.pending["d"]["turn_id"], "round2")

    async def test_failed_wakeup_keeps_durable_pending(self):
        from plugins.local_multica.client import ClientError
        plugin = self.plugin()
        await plugin.track({"dialogue_id": "d", "state": "waiting", "turn_id": "round"},
                           {"session_id": "room", "user_id": "12345", "message_id": "msg"})
        plugin.ctx.maisaka.proactive.trigger.return_value = {"success": False}
        with patch("plugins.local_multica.plugin.request", return_value={"state": "question", "text": "你希望几点提醒？"}):
            with self.assertRaises(ClientError):
                await plugin.poll_one("d", plugin.pending["d"])
        self.assertIn("d", plugin.pending)

    async def test_reload_restores_pending_and_unload_stops_watcher(self):
        plugin = self.plugin()
        await plugin.track({"dialogue_id": "d", "state": "waiting", "turn_id": "round"},
                           {"session_id": "room", "user_id": "12345", "message_id": "msg"})
        plugin.pending.clear()
        await plugin.on_load()
        self.assertIn("d", plugin.pending)
        await plugin.on_unload()
        self.assertTrue(plugin.watcher.done())

    async def test_explicit_plan_read_resumes_expired_wait(self):
        plugin = self.plugin()
        result = {"dialogue_id": "d", "state": "waiting", "turn_id": "round"}
        payload = {"session_id": "room", "user_id": "12345", "message_id": "msg"}
        await plugin.track(result, payload)
        plugin.pending["d"]["deadline"] = time.time() - 1
        await plugin.track(result, payload)
        self.assertGreater(plugin.pending["d"]["deadline"], time.time() + 3500)

    async def test_natural_language_complete_planning_and_confirmation(self):
        from test_dialogue import PlanningCli, PROJECT, MIKA
        from bridge.server import Service, Settings
        import sys
        plugin = self.plugin()
        plugin.ctx.message.get_by_id.return_value = self.origin()
        settings = Settings(cli_path=sys.executable, workspace_id="00000000-0000-4000-8000-000000000006", project_id=PROJECT,
                            assignee_id="00000000-0000-4000-8000-000000000008", mika_id=MIKA, allowed_users=["12345"],
                            token="test-" + "z" * 32, state_dir=plugin.ctx.paths.data_dir / "bridge")
        cli = PlanningCli()
        service = Service(settings, cli)
        def route(url, token, body):
            return {"ok": True, **service.handle(body)}
        with patch("plugins.local_multica.plugin.request", side_effect=route):
            result = await plugin.multica_mika("discuss", request_message_id="msg", message="做一个提醒工具", stream_id="room")
            first = json.loads(result["content"])
            plan = {"turn_id": first["turn_id"], "state": "proposal", "reply": "建议如下", "title": "提醒工具",
                    "steps": [{"title": "提醒", "description": "按时提醒", "acceptance": "收到一次"}]}
            cli.comments = [{"id": "comment", "author_type": "agent", "author_id": settings.mika_id,
                             "content": "```json\n" + json.dumps(plan, ensure_ascii=False) + "\n```"}]
            result = await plugin.multica_mika("plan", request_message_id="msg", dialogue_id=first["dialogue_id"], stream_id="room")
            proposal = json.loads(result["content"])
            self.assertEqual(cli.created, 1)
            result = await plugin.multica_mika("confirm", request_message_id="msg", dialogue_id=first["dialogue_id"],
                                               version=proposal["version"], review="内容符合用户意图，范围及验收清楚", stream_id="room")
            self.assertTrue(result["success"])
            self.assertEqual(json.loads(result["content"])["state"], "confirmed")
            self.assertEqual(cli.created, 2)

    def delivery_fixture(self):
        from test_artifacts import ArtifactTests
        fixture = ArtifactTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plugin = self.plugin()
        async def rpc(body):
            return fixture.service.handle(body)
        plugin.rpc = rpc
        return plugin, fixture

    async def delivery_job(self, plugin):
        await plugin.track_execution({"state": "confirmed", "reference": "DEMO-12"},
                                     {"session_id": "room", "user_id": "12345"})
        return next(iter(plugin.jobs.items()))

    async def test_image_pipeline_and_receipts_prevent_repeated_send(self):
        plugin, fixture = self.delivery_fixture()
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        self.assertEqual(plugin.ctx.send.image.call_args.args[1], "room")
        self.assertTrue(plugin.ctx.send.image.call_args.kwargs["return_details"])
        plugin.ctx.send.text.assert_awaited_once()
        self.assertEqual(plugin.jobs, {})
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        plugin.ctx.send.text.assert_awaited_once()

    async def test_uncertain_image_send_is_not_retried(self):
        plugin, fixture = self.delivery_fixture()
        plugin.ctx.send.image.side_effect = TimeoutError("unknown platform result")
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        self.assertIn("未确认", plugin.ctx.send.text.call_args.args[0])

    async def test_same_image_on_parent_and_child_sent_only_once(self):
        from test_artifacts import CHILD
        plugin, fixture = self.delivery_fixture()
        fixture.cli.tree = {"stages": [], "unstaged": [{"id": CHILD}]}
        fixture.cli.child["attachments"] = [{**fixture.cli.attachment, "id": CHILD, "issue_id": CHILD}]
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        self.assertEqual(len(fixture.cli.downloads), 2)

    async def test_running_task_never_sends_draft_images(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["status"] = "in_progress"
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_not_called()
        self.assertIn(key, plugin.jobs)

    async def test_execution_jobs_restore_after_plugin_reload(self):
        plugin, fixture = self.delivery_fixture()
        key, job = await self.delivery_job(plugin)
        plugin.jobs.clear()
        await plugin.on_load()
        try:
            self.assertEqual(plugin.jobs[key]["reference"], "DEMO-12")
        finally:
            await plugin.on_unload()

    async def test_routing_hook_injects_mode_and_preserves_other_tools(self):
        from src.llm_models.payload_content.context_snapshot import deserialize_context_item_snapshot
        from src.llm_models.payload_content.context_protocol import ContextProtocolMode, validate_context_items
        plugin = self.plugin()
        def meta(identity, turn=None):
            return {"item_id": identity, "logical_turn_id": turn, "timestamp": "2026-11-17T00:00:00"}
        items = [{"item_type": "SystemMessageItem", "meta": meta("host"), "parts": [{"type": "text", "text": "Host rules"}]},
                 {"item_type": "UserMessageItem", "meta": meta("user", "turn"), "parts": [{"type": "text", "text": "Draw a cat"}]},
                 {"item_type": "FunctionCallItem", "meta": meta("call", "turn"),
                  "tool_call": {"call_id": "call1", "func_name": "requests", "args": {}, "extra_content": None}},
                 {"item_type": "FunctionCallOutputItem", "meta": meta("output", "turn"), "call_id": "call1",
                  "output": "[]", "success": True, "tool_name": "requests"}]
        plugin._plugin_config_instance.routing.mode = "all_tasks"
        result = await plugin.routing_prompt(items, tool_definitions=[{"name": "multica_mika"}])
        injected = result["modified_kwargs"]["items"]
        self.assertIn("所有用户明确交办", injected[1]["parts"][0]["text"])
        self.assertEqual(injected[-2:], items[-2:])
        validate_context_items([deserialize_context_item_snapshot(row) for row in injected], ContextProtocolMode.REQUEST_CONTEXT)
        self.assertNotIn("modified_kwargs", await plugin.routing_prompt(items, tool_definitions=[{"name": "reply"}]))

    async def test_routing_survives_host_replacement_after_items_only_hook(self):
        # Load the installed host's actual merge method without bootstrapping
        # its global config/logging services in this isolated test container.
        import ast
        import __future__
        host = Path(importlib.util.find_spec("src.plugin_runtime").origin).parent
        tree = ast.parse((host / "host/hook_dispatcher.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HookDispatcher")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_merge_blocking_result")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "host_merge", "exec",
                     flags=__future__.annotations.compiler_flag), namespace)
        plugin = self.plugin()
        state = SimpleNamespace(hook_name="maisaka.planner.before_request", custom_results=[], errors=[], kwargs={
            "items": [], "tool_definitions": [{"name": "multica_mika"}], "session_id": "test-session"})
        target = SimpleNamespace(entry=SimpleNamespace(full_name="earlier.items_only", plugin_id="earlier"))
        namespace["_merge_blocking_result"](None,
            hook_spec=SimpleNamespace(allow_kwargs_mutation=True), target=target,
            execution_result=SimpleNamespace(success=True, action="continue", custom_result=None,
                                             modified_kwargs={"items": []}), dispatch_result=state)
        self.assertNotIn("tool_definitions", state.kwargs)
        result = await plugin.routing_prompt(**state.kwargs)
        self.assertEqual(result["modified_kwargs"]["items"][0]["meta"]["item_id"], "multica-route:policy")
        self.assertNotIn("tool_definitions", result["modified_kwargs"])
        repeat = await plugin.routing_prompt(**result["modified_kwargs"])
        self.assertEqual(len(repeat["modified_kwargs"]["items"]), 1)

    async def test_routing_preserves_complete_host_payload(self):
        plugin = self.plugin()
        payload = {"items": [], "tool_definitions": [{"type": "function", "function": {"name": "multica_mika"}}],
                   "session_id": "test-session", "item_schema_version": 1, "selected_history_count": 2,
                   "built_message_count": 3, "selection_reason": "test", "future_field": {"value": 4}}
        result = (await plugin.routing_prompt(**payload))["modified_kwargs"]
        self.assertEqual({k:v for k,v in result.items() if k != "items"},
                         {k:v for k,v in payload.items() if k != "items"})
        self.assertEqual(payload["items"], [])

    async def test_routing_missing_tools_respects_plugin_and_user_controls(self):
        plugin = self.plugin()
        for definitions in ([], None, [{"name": "reply"}]):
            self.assertNotIn("modified_kwargs", await plugin.routing_prompt([], tool_definitions=definitions))
        plugin._plugin_config_instance.plugin.enabled = False
        self.assertNotIn("modified_kwargs", await plugin.routing_prompt([]))
        plugin._plugin_config_instance.plugin.enabled = True
        plugin._plugin_config_instance.bridge.allowed_users = []
        self.assertNotIn("modified_kwargs", await plugin.routing_prompt([]))

    async def test_result_tool_returns_metadata_not_image_bytes(self):
        plugin, fixture = self.delivery_fixture()
        plugin.ctx.message.get_by_id.return_value = self.origin()
        def route(url, token, payload):
            return fixture.service.handle(payload)
        with patch("plugins.local_multica.plugin.request", side_effect=route):
            result = await plugin.multica_mika("result", request_message_id="msg", reference="DEMO-12", stream_id="room")
        self.assertTrue(result["success"])
        self.assertNotIn("image_base64", result["content"])
        key, job = next(iter(plugin.jobs.items()))
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        plugin.ctx.send.text.assert_not_called()

    async def test_legacy_delivery_does_not_start_new_closure(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["revision"] = 4
        plugin._plugin_config_instance.closure.enabled = True
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertEqual(plugin.ctx.send.text.await_count, 1)
        self.assertNotIn("3分钟", plugin.ctx.send.text.call_args.args[0])
        self.assertIsNone(plugin.acceptance)
        await plugin.deliver_job(key, job)
        self.assertEqual(plugin.ctx.send.text.await_count, 1)

    async def test_failed_result_delivery_never_starts_closure(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["revision"] = 4
        plugin._plugin_config_instance.closure.enabled = True
        plugin.ctx.send.image.return_value = {"sent": False}
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertIsNone(plugin.acceptance)
        plugin.ctx.send.text.assert_not_called()

    async def test_unknown_text_delivery_never_starts_closure(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["revision"] = 4
        plugin._plugin_config_instance.closure.enabled = True
        plugin.ctx.send.text.return_value = None
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertIsNone(plugin.acceptance)

    async def test_real_sdk_preserves_send_receipt_details(self):
        from maibot_sdk.context import PluginContext
        actual = PluginContext._normalize_capability_result("send.text", {"success": True, "sent": True, "message_id": "qq-final"}, return_details=True)
        self.assertIs(actual["sent"], True)
        self.assertEqual(actual["message_id"], "qq-final")

    async def test_new_business_pipeline_completes_without_user_confirmation(self):
        from test_completion import BusinessFixture, PARENT, ROOT
        fixture = BusinessFixture(self)
        plugin = self.plugin()
        async def rpc(body):
            return fixture.f.service.handle(body)
        plugin.rpc = rpc
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertEqual([w[2] for w in fixture.writes], [ROOT, PARENT])
        self.assertEqual(plugin.jobs, {})
        plugin.ctx.send.text.assert_awaited_once()
        plugin.ctx.send.image.assert_awaited_once()

    async def test_new_business_waits_for_running_descendants_without_failure_count(self):
        from test_completion import BusinessFixture, CHILD
        fixture = BusinessFixture(self)
        fixture.cli.tree = {"unstaged": [{"id": CHILD}], "stages": [], "total": 1}
        fixture.cli.child["status"] = "in_progress"
        plugin = self.plugin()
        async def rpc(body):
            return fixture.f.service.handle(body)
        plugin.rpc = rpc
        key, job = await self.delivery_job(plugin)
        for _ in range(6):
            await plugin.deliver_job(key, job)
        self.assertEqual(plugin.delivery_failures[key], 0)
        self.assertIn(key, plugin.jobs)
        plugin.ctx.send.text.assert_not_called()

    async def test_busy_polls_do_not_exhaust_delivery_budget_or_send(self):
        from plugins.local_multica.client import BridgeBusyError
        plugin, fixture = self.delivery_fixture()
        key, job = await self.delivery_job(plugin)
        plugin.rpc = AsyncMock(side_effect=BridgeBusyError("busy"))
        for _ in range(7):
            await plugin.deliver_job(key, job)
        self.assertEqual(plugin.delivery_failures.get(key, 0), 0)
        self.assertIn(key, plugin.jobs)
        self.assertFalse(job.get("delivery_stopped", False))
        plugin.ctx.send.text.assert_not_called()
        plugin.ctx.send.image.assert_not_called()

    async def test_rpc_serializes_tool_and_poller_requests(self):
        import asyncio
        import threading
        plugin = self.plugin()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked(url, token, body):
            calls.append(body['operation'])
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test release timeout')
            return {'text':'ok'}
        with patch('plugins.local_multica.plugin.request', side_effect=blocked):
            first = asyncio.create_task(plugin.rpc({'operation':'result'}))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                second = asyncio.create_task(plugin.rpc({'operation':'artifacts'}))
                await asyncio.sleep(0.02)
                self.assertEqual(calls, ['result'])
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []))
        self.assertEqual(calls, ['result','artifacts'])

    async def test_review_before_final_image_keeps_watching(self):
        from datetime import datetime, timezone
        plugin, fixture = self.delivery_fixture()
        key, job = await self.delivery_job(plugin)
        attachment = fixture.cli.root["attachments"].pop()
        # The issue is in_review but the final comment has not been uploaded yet.
        await plugin.deliver_job(key, job)
        plugin.ctx.send.text.assert_not_called()
        self.assertIn(key, plugin.jobs)
        fixture.cli.root["attachments"].append(attachment)
        fixture.cli.root["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        self.assertNotIn(key, plugin.jobs)

    async def test_five_missing_checks_report_once_without_closing(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["attachments"] = []
        plugin._plugin_config_instance.closure.enabled = True
        key, job = await self.delivery_job(plugin)
        for _ in range(4):
            await plugin.deliver_job(key, job)
        plugin.ctx.send.text.assert_not_called()
        await plugin.deliver_job(key, job)
        self.assertIn("连续5次", plugin.ctx.send.text.call_args.args[0])
        self.assertIn("交付失败", plugin.ctx.send.text.call_args.args[0])
        self.assertIsNone(plugin.acceptance)
        self.assertNotIn(key, plugin.jobs)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.text.assert_awaited_once()

    async def test_download_failures_checked_five_times_without_send_attempts(self):
        plugin, fixture = self.delivery_fixture()
        key, job = await self.delivery_job(plugin)
        with patch.object(fixture.cli, "download", side_effect=TimeoutError) as download:
            for _ in range(5):
                await plugin.deliver_job(key, job)
        self.assertEqual(download.call_count, 5)
        plugin.ctx.send.image.assert_not_called()
        self.assertIn("连续5次", plugin.ctx.send.text.call_args.args[0])

    async def test_running_check_resets_failures_and_restart_starts_from_zero(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["attachments"] = []
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        await plugin.deliver_job(key, job)
        self.assertEqual(plugin.delivery_failures[key], 2)
        self.assertNotIn("delivery_failures", json.loads((plugin.ctx.paths.data_dir / "execution_pending.json").read_text())[key])
        await plugin.on_load()
        try:
            self.assertEqual(plugin.delivery_failures, {})
            job = plugin.jobs[key]
            await plugin.deliver_job(key, job)
            fixture.cli.root["status"] = "in_progress"
            await plugin.deliver_job(key, job)
            self.assertEqual(plugin.delivery_failures[key], 0)
        finally:
            await plugin.on_unload()

    async def test_configure_type_via_tool_and_settings_survive_reload(self):
        plugin = self.plugin()
        plugin.ctx.message.get_by_id.return_value = self.origin()
        result = await plugin.multica_mika("configure", request_message_id="msg", task_type="image",
                                           poll_seconds=10, max_failures=8, stream_id="room")
        self.assertTrue(result["success"])
        job = dict(user_id="12345", session_id="room", task_type="image")
        self.assertEqual(plugin.delivery_settings(job), dict(poll_seconds=10, max_failures=8))
        plugin.polling = None
        self.assertEqual(plugin.delivery_settings(job)["max_failures"], 8)
        self.assertEqual(plugin.delivery_settings({**job, "user_id": "other"})["max_failures"], 5)
        self.assertEqual(plugin.delivery_settings({**job, "task_type": "development"})["poll_seconds"], 20)

    async def test_task_specific_failure_limit_and_scope_validation(self):
        plugin, fixture = self.delivery_fixture()
        fixture.cli.root["attachments"] = []
        plugin.ctx.message.get_by_id.return_value = self.origin()
        key, job = await self.delivery_job(plugin)
        result = await plugin.multica_mika("configure", request_message_id="msg", reference="DEMO-12", max_failures=2, stream_id="room")
        self.assertTrue(result["success"])
        bad = await plugin.multica_mika("configure", request_message_id="msg", reference="DEMO-99", max_failures=2, stream_id="room")
        self.assertFalse(bad["success"])
        await plugin.deliver_job(key, job)
        plugin.ctx.send.text.assert_not_called()
        await plugin.deliver_job(key, job)
        self.assertIn("连续2次", plugin.ctx.send.text.call_args.args[0])

    async def test_generic_file_result_verified_then_reported(self):
        from test_transfer import manifest
        plugin, fixture = self.delivery_fixture()
        from test_qq_files import configure_api
        configure_api(plugin)
        fixture.cli.data = b"document"
        fixture.cli.attachment.update(filename="report.docx", content_type="application/octet-stream", size_bytes=8)
        manifest(fixture, ["report.docx"])
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertEqual(len(fixture.cli.downloads), 1)
        plugin.ctx.send.image.assert_not_called()
        self.assertIn("report.docx", plugin.ctx.send.text.call_args.args[0])

    async def test_complete_manifest_delivers_immediately_after_missing_poll(self):
        from test_transfer import manifest
        plugin, fixture = self.delivery_fixture()
        manifest(fixture, [fixture.cli.attachment["filename"]])
        attachment = fixture.cli.root["attachments"].pop()
        key, job = await self.delivery_job(plugin)
        await plugin.deliver_job(key, job)
        self.assertEqual(plugin.delivery_failures[key], 1)
        fixture.cli.root["attachments"].append(attachment)
        await plugin.deliver_job(key, job)
        plugin.ctx.send.image.assert_awaited_once()
        self.assertEqual(plugin.delivery_failures[key], 0)


if __name__ == "__main__":
    unittest.main()
