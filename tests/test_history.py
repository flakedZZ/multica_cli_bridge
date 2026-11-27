import ast
import os
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch
import test_sdk as sdk_fixtures
import test_dialogue as dialogue_fixtures
from bridge.server import BridgeError


def host_payload(arguments, context):
    # Execute the actual two patched host methods without booting MaiBot.
    from scripts.patch_host import patch_source
    path = Path(os.environ.get('MAIBOT_HOST_SOURCE', '/MaiMBot/src/plugin_runtime/component_query.py'))
    if not path.is_file():
        raise unittest.SkipTest('requires compatible MaiBot host source; see README')
    tree = ast.parse(patch_source(path.read_text(encoding='utf-8')))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ComponentQueryService')
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in
                ('_build_tool_context_payload', '_build_tool_invocation_payload')]
    env = dict(Any=Any, Dict=Dict, Optional=Optional, ToolInvocation=Any, ToolExecutionContext=Any)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])), '<host-patch>', 'exec'),env)
    return env['ComponentQueryService']._build_tool_invocation_payload(
        SimpleNamespace(arguments=arguments,tool_name='multica_mika'),context)


def context(user='12345', session='room'):
    return SimpleNamespace(stream_id=session,session_id=session,user_id=user,platform='qq',group_id='',metadata={})


class HostIdentityTests(unittest.TestCase):
    def test_model_cannot_override_reserved_identity(self):
        payload=host_payload({'stream_id':'victim','user_id':'victim','platform':'fake',
                              '_multica_context':{'user_id':'victim'}},context())
        self.assertEqual(payload['_multica_context'],dict(stream_id='room',user_id='12345',platform='qq'))
        self.assertEqual(payload['user_id'],'12345')

    def test_absent_context_erases_forged_identity(self):
        payload=host_payload({'user_id':'12345','stream_id':'room','_multica_context':{'user_id':'12345'}},None)
        self.assertEqual(payload['_multica_context'],dict(stream_id='',user_id='',platform=''))

    def test_group_missing_actor_cannot_be_filled_by_model(self):
        payload=host_payload({'user_id':'12345','_multica_context':{'user_id':'12345'}},context(user=''))
        self.assertEqual(payload['_multica_context']['user_id'],'')

    def test_host_anchor_identity_overrides_model(self):
        ctx=context(user='')
        ctx.metadata={'anchor_message':SimpleNamespace(message_info=SimpleNamespace(
            user_info=SimpleNamespace(user_id='67890'),group_info=None))}
        self.assertEqual(host_payload({'user_id':'12345'},ctx)['_multica_context']['user_id'],'67890')


@unittest.skipUnless(sdk_fixtures.HAS_SDK, 'requires installed MaiBot SDK')
class HistoryTests(unittest.IsolatedAsyncioTestCase):
    plugin = sdk_fixtures.SdkTests.plugin
    origin = sdk_fixtures.SdkTests.origin

    def bridge(self):
        f=dialogue_fixtures.DialogueTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        plan=f.proposal()
        f.call(operation='confirm_plan',dialogue_id=plan['dialogue_id'],version=plan['version'],review='approved')
        f.service=dialogue_fixtures.Service(f.settings,f.cli)
        return f

    async def test_pending_recovery_without_original_message_after_restart(self):
        p = self.plugin()
        f = dialogue_fixtures.DialogueTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        conversation = f.call()["dialogue_id"]
        f.reply(conversation)
        f.service = dialogue_fixtures.Service(f.settings, f.cli)
        p.ctx.message.get_by_id.return_value = None
        p.ctx.message.get_recent.return_value = []
        with patch("plugins.local_multica.plugin.request", side_effect=lambda _u, _t, b: f.service.handle(b)):
            listed = await p.multica_mika("pending", **host_payload({}, context()))
            row = json.loads(listed["content"])["dialogues"][0]
            plan_result = await p.multica_mika("plan", dialogue_id=row["dialogue_id"], **host_payload({}, context()))
            plan = json.loads(plan_result["content"])
            result = await p.multica_mika("confirm", dialogue_id=conversation,
                                          version=plan["version"], review="符合原任务", task_type="image",
                                          **host_payload({}, context()))
            self.assertTrue(result["success"], result)
        p.ctx.message.get_by_id.assert_not_awaited()
        self.assertEqual(f.cli.created, 2)
        self.assertEqual(len(p.jobs), 1)
        self.assertTrue(next(iter(p.jobs.values()))["notify"])

    async def test_pending_and_confirm_reject_untrusted_or_wrong_identity(self):
        p = self.plugin()
        with patch("plugins.local_multica.plugin.request") as rpc:
            for action in ("pending", "plan", "confirm", "cancel"):
                result = await p.multica_mika(action, stream_id="room", user_id="12345")
                self.assertFalse(result["success"])
            rpc.assert_not_called()
        p.ctx.message.get_by_id.return_value = self.origin(message_info={"user_info": {"user_id": "67890"}})
        p.config.bridge.allowed_users.append("67890")
        with patch("plugins.local_multica.plugin.request") as rpc:
            result = await p.multica_mika("confirm", request_message_id="msg", **host_payload({}, context()))
            self.assertFalse(result["success"])
            rpc.assert_not_called()

    async def test_reference_queries_survive_restart_and_empty_message_window(self):
        p=self.plugin()
        f=self.bridge()
        p.ctx.message.get_recent.return_value=[]
        p.ctx.message.get_by_id.return_value=None
        with patch('plugins.local_multica.plugin.request',side_effect=lambda _u,_t,b:f.service.handle(b)):
            for action in ('status','result','history'):
                result=await p.multica_mika(action,reference='DEMO-21',**host_payload({},context()))
                self.assertTrue(result['success'],result)
                self.assertIn('DEMO-21',result['content'])
        p.ctx.message.get_by_id.assert_not_awaited()
        p.ctx.message.get_recent.assert_not_awaited()

    async def test_other_user_and_session_are_denied_by_durable_bridge(self):
        p=self.plugin()
        p.config.bridge.allowed_users.append('67890')
        f=self.bridge()
        from plugins.local_multica.client import ClientError
        def rpc(_u,_t,body):
            try: return f.service.handle(body)
            except BridgeError as e: raise ClientError(str(e))
        with patch('plugins.local_multica.plugin.request',side_effect=rpc):
            for ctx in (context(user='67890'),context(session='other')):
                for action in ('status','result'):
                    result=await p.multica_mika(action,reference='DEMO-21',**host_payload({},ctx))
                    self.assertFalse(result['success'])
                    self.assertIn('本人在当前会话',result['content'])

    async def test_plain_user_kwargs_do_not_authenticate(self):
        p=self.plugin()
        with patch('plugins.local_multica.plugin.request') as rpc:
            result=await p.multica_mika('status',reference='DEMO-21',stream_id='room',user_id='12345')
            self.assertFalse(result['success'])
            rpc.assert_not_called()

    async def test_group_without_actor_can_use_verified_current_query_message(self):
        p=self.plugin()
        f=self.bridge()
        p.ctx.message.get_by_id.return_value=self.origin(message_id='current-query',timestamp='1')
        with patch('plugins.local_multica.plugin.request',side_effect=lambda _u,_t,b:f.service.handle(b)):
            result=await p.multica_mika('status',reference='DEMO-21',request_message_id='current-query',
                                      **host_payload({},context(user='')))
            self.assertTrue(result['success'])
        p.ctx.message.get_by_id.assert_awaited_once_with('current-query',stream_id='room')

    async def test_source_mismatch_and_missing_trust_fail_closed(self):
        p=self.plugin()
        with patch('plugins.local_multica.plugin.request') as rpc:
            for source in (None,self.origin(message_id='wrong'),self.origin(session_id='other'),
                           self.origin(message_info={'user_info':{'user_id':'67890'}}),self.origin(account_id='12345')):
                p.ctx.message.get_by_id.return_value=source
                result=await p.multica_mika('status',reference='DEMO-21',request_message_id='msg',**host_payload({},context()))
                self.assertFalse(result['success'])
            result=await p.multica_mika('status',reference='DEMO-21',**host_payload({},context(user='')))
            self.assertFalse(result['success'])
            rpc.assert_not_called()
