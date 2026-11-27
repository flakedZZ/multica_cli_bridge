from pathlib import Path
from tempfile import TemporaryDirectory

import json
import sys
import unittest

from bridge.server import BridgeError, Service, Settings


PROJECT = "00000000-0000-4000-8000-000000000007"
MIKA = "00000000-0000-4000-8000-000000000009"
DISCUSSION = "00000000-0000-4000-8000-000000000001"
EXECUTION = "00000000-0000-4000-8000-000000000002"
COMMENT = "00000000-0000-4000-8000-000000000004"


class PlanningCli:
    def __init__(self):
        self.calls = []
        self.bodies = []
        self.comments = []
        self.fail = False
        self.project = PROJECT
        self.created = 0
        self.execution_parent = DISCUSSION
        self.readback_fails = False

    def run(self, args):
        self.calls.append(args)
        if "--description-file" in args or "--content-file" in args:
            flag = "--description-file" if "--description-file" in args else "--content-file"
            self.bodies.append(Path(args[args.index(flag) + 1]).read_text(encoding="utf-8"))
            if self.fail:
                raise BridgeError("timeout", 502)
            if args[:2] == ["issue", "create"]:
                self.created += 1
                return {"id": DISCUSSION if self.created == 1 else EXECUTION,
                        "identifier": "DEMO-20" if self.created == 1 else "DEMO-21"}
            return {"id": COMMENT, "trigger_outcomes": [{"status": "queued"}]}
        if args[:2] == ["issue", "get"]:
            if args[2] == EXECUTION:
                if self.readback_fails:
                    raise BridgeError("readback timeout", 502)
                return {"id": EXECUTION, "identifier": "DEMO-21", "project_id": self.project,
                        "parent_issue_id": self.execution_parent}
            return {"id": DISCUSSION, "project_id": self.project}
        if "--roots-only" in args:
            return [{"id": COMMENT, "author_type": "agent", "author_id": MIKA}]
        if "--thread" in args:
            return self.comments
        raise AssertionError(args)


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(cli_path=sys.executable, workspace_id="00000000-0000-4000-8000-000000000006",
                                 project_id=PROJECT, assignee_id="00000000-0000-4000-8000-000000000008", mika_id=MIKA,
                                 allowed_users=["12345", "67890"], token="test-" + "x" * 32,
                                 state_dir=Path(self.temp.name))
        self.cli = PlanningCli()
        self.service = Service(self.settings, self.cli)
        self.body = {"platform": "qq", "user_id": "12345", "session_id": "room", "message_id": "msg",
                     "operation": "discuss", "message": "帮我和规划Agent商量做个提醒工具，先想清楚再做"}

    def call(self, **changes):
        return self.service.handle({**self.body, **changes})

    def reply(self, conversation, state="proposal", author=MIKA, turn=None):
        row = self.service.dialogue.owned(conversation, "12345", "room")
        plan = {"turn_id": turn or row["current_turn"], "state": state, "reply": "我建议先整理通知规则，再实现提醒。",
                "title": "提醒工具", "steps": [{"title": "实现通知", "description": "按用户时区发提醒", "acceptance": "指定时间收到一次提醒"}]}
        self.cli.comments = [{"id": COMMENT, "author_type": "agent", "author_id": author,
                              "content": "自然语言方案\n```json\n" + json.dumps(plan, ensure_ascii=False) + "\n```"}]
        return plan

    def proposal(self):
        result = self.call()
        self.reply(result["dialogue_id"])
        return self.call(operation="plan", dialogue_id=result["dialogue_id"])

    def test_discussion_is_separate_and_cannot_direct_submit(self):
        result = self.call()
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(self.cli.created, 1)
        self.assertIn("QQ需求讨论", self.cli.calls[0][self.cli.calls[0].index("--title") + 1])
        self.assertIn("不要创建执行任务", self.cli.bodies[0])
        with self.assertRaisesRegex(BridgeError, "直接提交已关闭"):
            self.call(operation="submit", title="跳过", description="跳过")
        with self.assertRaises(BridgeError):
            self.call(operation="confirm_plan", dialogue_id=result["dialogue_id"], version="fake", review="批准")
        self.assertEqual(self.cli.created, 1)

    def test_discussion_replay_does_not_wake_mika_twice(self):
        original = self.call()
        self.service = Service(self.settings, self.cli)
        self.assertEqual(self.call()["dialogue_id"], original["dialogue_id"])
        self.assertEqual(self.cli.created, 1)

    def test_proposal_confirmation_and_restart_idempotency(self):
        plan = self.proposal()
        self.assertEqual(plan["state"], "proposal")
        self.assertIn("验收", plan["text"])
        self.assertEqual(self.cli.created, 1)
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="范围符合原请求，验收条件明确")
        result = self.call(**args)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(self.cli.created, 2)
        create = next(c for c in reversed(self.cli.calls) if c[:2] == ["issue", "create"])
        self.assertIn(MIKA, create)
        self.assertEqual(create[create.index("--parent") + 1], DISCUSSION)
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT parent_id FROM execution_links").fetchone()[0], DISCUSSION)
        self.assertIn("批准版本：" + plan["version"], self.cli.bodies[-1])
        self.assertIn("[multica-delivery:v1]", self.cli.bodies[-1])
        self.assertIn('"multica_delivery":1', self.cli.bodies[-1])
        self.assertIn("用户原始图片/文件", self.cli.bodies[-1])
        self.service = Service(self.settings, self.cli)
        self.assertEqual(self.call(**args)["issue_id"], EXECUTION)
        self.assertEqual(self.cli.created, 2)
        self.assertIn("DEMO-21", self.call(operation="list")["text"])

    def test_omitted_dialogue_id_resolves_reviewed_version_and_reuses_execution(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", version=plan["version"], review="符合原请求")
        for _ in range(2):
            self.assertEqual(self.call(**args)["issue_id"], EXECUTION)
            self.service = Service(self.settings, self.cli)
        self.assertEqual(self.cli.created, 2)
        self.assertEqual(self.cli.calls[-1], ["issue", "get", DISCUSSION])

    def test_version_resolution_never_crosses_owner_session_or_guesses_latest(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", version=plan["version"], review="符合原请求")
        for change in ({"user_id": "67890"}, {"session_id": "other"}, {"version": "stale"},
                       {"dialogue_id": "wrong"}, {"dialogue_id": None}):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.call(**{**args, **change})
        self.assertEqual(self.cli.created, 1)
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_version_resolution_rejects_ambiguous_mapping(self):
        plan = self.proposal()
        with self.service.connect() as db:
            db.execute("INSERT INTO dialogues (id,user_id,session_id,current_turn,state,version) "
                       "VALUES ('duplicate','12345','room','other','proposal',?)", (plan["version"],))
        with self.assertRaisesRegex(BridgeError, "无法唯一定位"):
            self.call(operation="confirm_plan", version=plan["version"], review="approved")
        self.assertEqual(self.cli.created, 1)

    def test_version_resolution_still_requires_review_and_refuses_uncertain_retry(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", version=plan["version"])
        with self.assertRaisesRegex(BridgeError, "审核意见"):
            self.call(**args, review="")
        self.cli.fail = True
        with self.assertRaisesRegex(BridgeError, "结果未确认"):
            self.call(**args, review="approved")
        calls = len(self.cli.calls)
        self.service = Service(self.settings, self.cli)
        with self.assertRaisesRegex(BridgeError, "尚无可确认"):
            self.call(**args, review="approved")
        self.assertEqual(len(self.cli.calls), calls)

    def test_question_cannot_be_confirmed(self):
        conversation = self.call()["dialogue_id"]
        self.reply(conversation, state="question")
        result = self.call(operation="plan", dialogue_id=conversation)
        with self.assertRaisesRegex(BridgeError, "尚无可确认"):
            self.call(operation="confirm_plan", dialogue_id=conversation, version=result["version"], review="同意")
        self.assertEqual(self.cli.created, 1)

    def test_missing_parent_is_persisted_and_retry_never_recreates(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="approved")
        self.cli.execution_parent = None
        for _ in range(2):
            with self.assertRaisesRegex(BridgeError, "confirmation_parent_mismatch"):
                self.call(**args)
            self.service = Service(self.settings, self.cli)
        self.assertEqual(self.cli.created, 2)
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT state,issue_id FROM requests").fetchone()[:], ('pending', EXECUTION))
            self.assertEqual(db.execute("SELECT error_code FROM confirmation_checks").fetchone()[0], 'confirmation_parent_mismatch')
        # External, explicit repair can be verified on retry without creating.
        self.cli.execution_parent = DISCUSSION
        self.assertEqual(self.call(**args)['issue_id'], EXECUTION)
        self.assertEqual(self.cli.created, 2)

    def test_readback_timeout_reuses_known_execution_after_restart(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="approved")
        self.cli.readback_fails = True
        with self.assertRaisesRegex(BridgeError, "confirmation_readback_failed"):
            self.call(**args)
        self.service = Service(self.settings, self.cli)
        self.cli.readback_fails = False
        self.assertEqual(self.call(**args)['issue_id'], EXECUTION)
        self.assertEqual(self.cli.created, 2)

    def test_repeat_confirmation_detects_lost_parent_without_losing_query_ownership(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="approved")
        self.call(**args)
        self.cli.execution_parent = None
        with self.assertRaisesRegex(BridgeError, "confirmation_parent_mismatch"):
            self.call(**args)
        self.assertIn('DEMO-21', self.call(operation='status', reference='DEMO-21')['text'])
        self.assertEqual(self.cli.created, 2)

    def test_repeat_confirmation_checks_local_mapping_and_owner(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="approved")
        self.call(**args)
        for change in ({'user_id':'67890'}, {'session_id':'other'}):
            with self.assertRaises(BridgeError):
                self.call(**{**args, **change})
        with self.service.connect() as db:
            db.execute('DELETE FROM execution_links')
        with self.assertRaisesRegex(BridgeError, 'confirmation_mapping_mismatch'):
            self.call(**args)
        self.assertEqual(self.cli.created, 2)

    def test_wrong_parent_and_project_are_not_confirmed(self):
        plan = self.proposal()
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="approved")
        self.cli.execution_parent = EXECUTION
        with self.assertRaisesRegex(BridgeError, 'confirmation_parent_mismatch'):
            self.call(**args)
        self.cli.execution_parent = DISCUSSION
        self.cli.project = 'another-project'
        with self.assertRaisesRegex(BridgeError, 'confirmation_parent_mismatch'):
            self.call(**args)
        self.assertEqual(self.cli.created, 2)

    def test_wrong_author_old_round_or_malformed_cannot_be_approved(self):
        conversation = self.call()["dialogue_id"]
        for author, turn in (("someone-else", None), (MIKA, "old-round")):
            self.reply(conversation, author=author, turn=turn)
            self.assertEqual(self.call(operation="plan", dialogue_id=conversation)["state"], "waiting")
        self.reply(conversation)
        self.cli.comments[0]["content"] = "一个普通答复，不是可执行方案"
        self.assertEqual(self.call(operation="plan", dialogue_id=conversation)["state"], "waiting")
        self.assertEqual(self.cli.created, 1)

    def test_refinement_invalidates_old_version(self):
        plan = self.proposal()
        result = self.call(dialogue_id=plan["dialogue_id"], message="先不支持群聊，只做私聊")
        self.assertEqual(result["state"], "waiting")
        self.assertNotEqual(result["turn_id"], plan["turn_id"])
        self.assertIn(f"mention://agent/{MIKA}", self.cli.bodies[-1])
        with self.assertRaisesRegex(BridgeError, "版本已变化"):
            self.call(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="批准旧版")
        self.assertEqual(self.cli.created, 1)

    def test_confirmation_requires_actual_review(self):
        plan = self.proposal()
        with self.assertRaisesRegex(BridgeError, "审核意见"):
            self.call(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review=" ")
        self.assertEqual(self.cli.created, 1)

    def test_uncertain_confirmation_never_recreates(self):
        plan = self.proposal()
        self.cli.fail = True
        args = dict(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="已审核")
        with self.assertRaisesRegex(BridgeError, "结果未确认"):
            self.call(**args)
        calls = len(self.cli.calls)
        self.service = Service(self.settings, self.cli)
        with self.assertRaises(BridgeError):
            self.call(**args)
        self.assertEqual(len(self.cli.calls), calls)
        self.assertIn("pending", self.call(operation="list")["text"])

    def test_uncertain_discussion_never_repeats(self):
        self.cli.fail = True
        with self.assertRaises(BridgeError):
            self.call()
        self.assertEqual(self.call()["state"], "uncertain")
        self.assertEqual(len(self.cli.calls), 1)

    def test_ownership_and_cancellation(self):
        plan = self.proposal()
        for change in ({"user_id": "67890"}, {"session_id": "other"}):
            with self.assertRaises(BridgeError):
                self.call(operation="plan", dialogue_id=plan["dialogue_id"], **change)
        result = self.call(operation="cancel_plan", dialogue_id=plan["dialogue_id"])
        self.assertEqual(result["state"], "cancelled")
        with self.assertRaises(BridgeError):
            self.call(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="批准")

    def test_limits_and_mention_sanitization(self):
        self.settings.max_discussions_per_hour = 1
        self.call(message=f"[@别的Agent](mention://agent/{MIKA}) 给我生成图")
        self.assertNotIn("mention://", self.cli.bodies[0])
        with self.assertRaisesRegex(BridgeError, "讨论次数"):
            self.call(message_id="new")

    def test_execution_limit_and_bounded_reads(self):
        plan = self.proposal()
        self.assertIn("--roots-only", self.cli.calls[-2])
        self.assertEqual(self.cli.calls[-1][-3:], ["--tail", "10", "--compact"])
        self.settings.max_submissions_per_hour = 0
        with self.assertRaisesRegex(BridgeError, "提交次数"):
            self.call(operation="confirm_plan", dialogue_id=plan["dialogue_id"], version=plan["version"], review="审核")


if __name__ == "__main__":
    unittest.main()
