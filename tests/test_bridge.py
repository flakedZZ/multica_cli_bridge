from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import json
import runpy
import warnings
import subprocess
import sys
import threading
import unittest

from bridge.server import BridgeError, Cli, Server, Service, Settings
from plugins.local_multica.client import BridgeBusyError, ClientError, parse, request


PROJECT = "00000000-0000-4000-8000-000000000007"
ISSUE = "00000000-0000-4000-8000-000000000001"


class FakeCli:
    def __init__(self):
        self.calls = []
        self.description = ""
        self.fail_create = False
        self.project = PROJECT

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "create"]:
            self.description = Path(args[args.index("--description-file") + 1]).read_text(encoding="utf-8")
            if self.fail_create:
                raise BridgeError("timeout", 502)
            return {"id": ISSUE, "identifier": "DEMO-12"}
        if args[:2] == ["issue", "get"]:
            return {"id": ISSUE, "title": "测试任务", "status": "in_review", "project_id": self.project}
        if "--roots-only" in args:
            return [{"id": ISSUE, "last_activity_at": "2026-11-17T00:00:00Z"}]
        if "--thread" in args:
            return [{"content": "实现完成，测试通过。"}]
        raise AssertionError(args)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            cli_path=sys.executable,
            workspace_id="00000000-0000-4000-8000-000000000006", project_id=PROJECT,
            assignee_id="00000000-0000-4000-8000-000000000008",
            allowed_users=["12345", "67890"], token="test-only-" + "a" * 40,
            state_dir=Path(self.temp.name),
            allow_direct_submit=True,
        )
        self.cli = FakeCli()
        self.service = Service(self.settings, self.cli)
        self.body = dict(platform="qq", user_id="12345", session_id="chat-one", message_id="message-1",
                         operation="submit", title="生成中文图案", description="第一行\n第二行 | 特殊字符 $() ` --help")

    def call(self, **kwargs):
        return self.service.handle({**self.body, **kwargs})

    def test_submit_unicode_and_restart_idempotence(self):
        result = self.call()
        self.assertIn("DEMO-12", result["text"])
        self.assertIn(self.body["description"], self.cli.description)
        self.assertIn("--assignee-id", self.cli.calls[0])
        self.assertIn("--status", self.cli.calls[0])
        self.service = Service(self.settings, self.cli)
        self.assertIn("未重复创建", self.call()["text"])
        self.assertEqual(len(self.cli.calls), 1)
        self.assertEqual(list(Path(self.temp.name).glob("request-*")), [])

    def test_same_id_different_payload_rejected(self):
        self.call()
        with self.assertRaisesRegex(BridgeError, "不同任务"):
            self.call(description="改过的需求")

    def test_uncertain_write_is_not_retried_after_restart(self):
        self.cli.fail_create = True
        with self.assertRaisesRegex(BridgeError, "未确认"):
            self.call()
        self.service = Service(self.settings, self.cli)
        with self.assertRaisesRegex(BridgeError, "禁止自动重试"):
            self.call()
        self.assertEqual(len(self.cli.calls), 1)
        self.assertIn("pending", self.call(operation="list")["text"])

    def test_access_and_operation_allowlists(self):
        for change in ({"user_id": "111"}, {"platform": "discord"}, {"operation": "shell"}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.call(**change)
        self.assertEqual(self.cli.calls, [])

    def test_scope_blocks_other_users_and_sessions(self):
        self.call()
        for change in ({"user_id": "67890"}, {"session_id": "chat-two"}, {"reference": "DEMO-11"}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.call(**{"operation": "status", "reference": "DEMO-12", **change})
        self.assertEqual(len(self.cli.calls), 1)
        self.assertIn("暂无", self.call(operation="list", user_id="67890")["text"])

    def test_project_change_denies_comments(self):
        self.call()
        self.cli.project = "another-project"
        with self.assertRaisesRegex(BridgeError, "不属于"):
            self.call(operation="result", reference="DEMO-12")
        self.assertEqual(len(self.cli.calls), 2)

    def test_result_uses_bounded_thread_reads(self):
        self.call()
        result = self.call(operation="result", reference="demo-12")
        self.assertIn("实现完成", result["text"])
        self.assertIn("--summary", self.cli.calls[-2])
        self.assertEqual(self.cli.calls[-1][-4:], [ISSUE, "--tail", "10", "--compact"])

    def test_rate_limit_and_busy(self):
        self.settings.max_submissions_per_hour = 1
        self.call()
        with self.assertRaisesRegex(BridgeError, "上限"):
            self.call(message_id="another")
        with self.service.lock, self.assertRaisesRegex(BridgeError, "正在处理"):
            self.call(operation="list")

    def test_invalid_payloads_cannot_start_cli(self):
        for change in ({"title": "x" * 121}, {"description": ""}, {"message_id": None}, {"title": "a\x00b"}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.call(**change)
        self.assertEqual(self.cli.calls, [])

    def test_cli_argv_no_shell_utf8_separate_stderr(self):
        completed = subprocess.CompletedProcess([], 0, '{"title":"中文"}', "warning")
        with patch("bridge.server.subprocess.run", return_value=completed) as run:
            self.assertEqual(Cli(self.settings).run(["issue", "get", ISSUE])["title"], "中文")
            args, kwargs = run.call_args
            self.assertEqual(args[0][0], sys.executable)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["encoding"], "utf-8")
        with patch("bridge.server.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "SECRET")):
            with self.assertRaises(BridgeError) as context:
                Cli(self.settings).run(["issue", "get", ISSUE])
            self.assertNotIn("SECRET", str(context.exception))

    def start_server(self):
        server = Server(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return "http://127.0.0.1:" + str(server.server_port)

    def test_real_http_round_trip(self):
        url = self.start_server()
        token_file = Path(self.temp.name) / "test.token"
        token_file.write_text(self.settings.token, encoding="ascii")
        self.assertIn("DEMO-12", request(url, str(token_file), self.body)["text"])
        self.assertIn("in_review", request(url, str(token_file), {**self.body, "operation": "status", "reference": "DEMO-12"})["text"])
        self.assertIn("实现完成", request(url, str(token_file), {**self.body, "operation": "result", "reference": "DEMO-12"})["text"])

    def test_http_auth_and_malformed_payload(self):
        url = self.start_server() + "/v1/request"
        headers = {"Content-Type": "application/json"}
        with self.assertRaises(HTTPError) as context:
            urlopen(Request(url, data=b"{}", headers=headers), timeout=2)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()
        headers["Authorization"] = "Bearer " + self.settings.token
        for data in (b"[]", b"bad json"):
            with self.assertRaises(HTTPError) as context:
                urlopen(Request(url, data=data, headers=headers), timeout=2)
            self.assertEqual(context.exception.code, 400)
            context.exception.close()
        self.assertEqual(self.cli.calls, [])

    def test_http_busy_retry_executes_write_once_after_lock_release(self):
        url = self.start_server()
        token = Path(self.temp.name) / "test.token"
        token.write_text(self.settings.token)
        self.service.lock.acquire()
        try:
            with patch("plugins.local_multica.client.time.sleep", side_effect=lambda _: self.service.lock.release()) as sleep:
                result = request(url, str(token), self.body)
            sleep.assert_called_once_with(0.5)
            self.assertIn("DEMO-12", result["text"])
            self.assertEqual(len(self.cli.calls), 1)
        finally:
            if self.service.lock.locked():
                self.service.lock.release()

    def test_http_busy_has_bounded_retries_and_no_side_effect(self):
        url = self.start_server()
        token = Path(self.temp.name) / "test.token"
        token.write_text(self.settings.token)
        with self.service.lock, patch("plugins.local_multica.client.time.sleep") as sleep:
            with self.assertRaises(BridgeBusyError):
                request(url, str(token), self.body)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(self.cli.calls, [])
        self.assertIsNone(self.service.active_request)

    def test_rate_limit_429_is_not_retried(self):
        self.call()
        self.settings.max_submissions_per_hour = 1
        url = self.start_server()
        token = Path(self.temp.name) / "test.token"
        token.write_text(self.settings.token)
        with patch("plugins.local_multica.client.time.sleep") as sleep:
            with self.assertRaisesRegex(ClientError, "上限"):
                request(url, str(token), {**self.body,"message_id":"another"})
        sleep.assert_not_called()
        self.assertEqual(len(self.cli.calls), 1)

    def test_ambiguous_client_failure_is_never_retried(self):
        with patch("plugins.local_multica.client._request_once", side_effect=ClientError("timeout")) as once:
            with self.assertRaises(ClientError):
                request("unused", "unused", self.body)
        once.assert_called_once()

    def test_cli_exception_releases_service_lock(self):
        self.cli.fail_create = True
        with self.assertRaises(BridgeError):
            self.call()
        self.assertFalse(self.service.lock.locked())
        self.assertIsNone(self.service.active_request)
        self.assertIn("pending", self.call(operation="list")["text"])

    def test_parser_and_remote_http_restriction(self):
        self.assertEqual(parse("提交 中文标题 | 多行\n正文")["description"], "多行\n正文")
        self.assertEqual(parse("结果 DEMO-1")["operation"], "result")
        for text in ("提交 没有分隔符", "shell whoami", "状态", "列表 extra"):
            with self.assertRaises(ClientError):
                parse(text)
        with self.assertRaisesRegex(ClientError, "HTTPS"):
            request("http://example.com", "missing.token", {})

    def test_module_entrypoint_returns_validation_error_over_http(self):
        # -m executes server under a second module name. The old lazy import
        # in Dialogue raised a different BridgeError and became HTTP 500.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            entry = runpy.run_module("bridge.server", run_name="test_entrypoint")
        service = entry["Service"](self.settings, self.cli)
        server = entry["Server"](("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            body = {**self.body, "operation": "plan", "dialogue_id": ""}
            req = Request(f"http://127.0.0.1:{server.server_port}/v1/request",
                          data=json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + self.settings.token,
                                   "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req, timeout=5)
            with caught.exception as error:
                self.assertEqual(error.code, 400)
                self.assertIn("dialogue_id", json.loads(error.read())["error"])
            self.assertFalse(service.lock.locked())
            self.assertEqual(self.cli.calls, [])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_health_checks_project_access_without_writing(self):
        with patch.object(self.cli, "run", return_value={"id": PROJECT}) as call:
            self.assertTrue(self.call(operation="health")["cli_ready"])
            call.assert_called_once_with(["project", "get", PROJECT])
        with patch.object(self.cli, "run", return_value={"id": "wrong-project"}):
            with self.assertRaisesRegex(BridgeError, "项目访问校验失败"):
                self.call(operation="health")


if __name__ == "__main__":
    unittest.main()
