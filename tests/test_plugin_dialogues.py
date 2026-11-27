"""Exercise actual tool -> request payload, never the server's missing-ID fallback."""
import asyncio
import json
import unittest
from unittest.mock import patch

import test_sdk as sdk
import test_dialogue as fixtures
from plugins.local_multica.client import ClientError


@unittest.skipUnless(sdk.HAS_SDK, 'requires installed MaiBot SDK')
class PluginDialogueTests(unittest.IsolatedAsyncioTestCase):
    plugin = sdk.SdkTests.plugin
    origin = sdk.SdkTests.origin

    def setUp(self):
        self.f = fixtures.DialogueTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.p = self.plugin()
        self.p.ctx.message.get_by_id.return_value = self.origin()
        self.sent = []
        def send(url, token, body):
            self.sent.append(dict(body))
            if body['operation'] == 'confirm_plan':
                self.assertTrue(body.get('dialogue_id'), 'must not use server fallback')
            try:
                return self.f.service.handle(body)
            except fixtures.BridgeError as error:
                raise ClientError(str(error)) from error
        self.mock = patch('plugins.local_multica.plugin.request', side_effect=send)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    async def call(self, action, **kwargs):
        return await self.p.multica_mika(action, _multica_context={
            'platform': 'qq', 'stream_id': 'room', 'user_id': '12345'}, **kwargs)

    async def plan(self):
        response = await self.call('discuss', request_message_id='msg', message='implement reminder')
        self.assertTrue(response['success'], response)
        dialogue = json.loads(response['content'])['dialogue_id']
        self.f.reply(dialogue)
        result = await self.call('plan', dialogue_id=dialogue)
        self.assertTrue(result['success'], result)
        return json.loads(result['content'])

    async def confirm(self, plan, **kwargs):
        return await self.call('confirm', version=plan['version'], review='scope reviewed', **kwargs)

    async def test_tool_injects_id_and_survives_new_plugin_and_server(self):
        plan = await self.plan()
        old = self.p
        self.p = self.plugin()
        self.p._ctx.paths.data_dir = old.ctx.paths.data_dir
        self.f.service = fixtures.Service(self.f.settings, self.f.cli)
        for _ in range(2):
            result = await self.confirm(plan)
            self.assertTrue(result['success'], result)
        confirms = [b for b in self.sent if b['operation'] == 'confirm_plan']
        self.assertEqual([b['dialogue_id'] for b in confirms], [plan['dialogue_id']] * 2)
        self.assertEqual(self.f.cli.created, 2)
        self.assertEqual(len(self.p.jobs), 1)
        with self.p.dialogue_bindings.connect() as db:
            row = db.execute('SELECT * FROM bindings').fetchone()
            self.assertEqual((row['message_id'], row['turn_id'], row['version']), ('msg', plan['turn_id'], plan['version']))

    async def test_poll_persists_proposal_before_wakeup(self):
        result = await self.call('discuss', request_message_id='msg', message='task')
        dialogue = json.loads(result['content'])['dialogue_id']
        self.f.reply(dialogue)
        await self.p.poll_one(dialogue, self.p.pending[dialogue])
        self.assertNotIn(dialogue, self.p.pending)
        with self.p.dialogue_bindings.connect() as db:
            row = db.execute('SELECT * FROM bindings').fetchone()
        self.assertEqual(row['state'], 'proposal')
        result = await self.call('confirm', version=row['version'], review='reviewed')
        self.assertTrue(result['success'], result)

    async def test_upgrade_without_bindings_uses_pending_then_plan(self):
        plan = self.f.proposal()
        self.assertTrue((await self.confirm(plan))['success'])
        self.assertEqual([b['operation'] for b in self.sent], ['pending', 'plan', 'confirm_plan'])

    async def test_wrong_user_group_private_session_and_explicit_id(self):
        plan = await self.plan()
        self.p.config.bridge.allowed_users.append('67890')
        for user, session in [('67890', 'room'), ('12345', 'other-group'), ('12345', 'private')]:
            for explicit in ['', plan['dialogue_id']]:
                result = await self.p.multica_mika('confirm', dialogue_id=explicit, version=plan['version'],
                    review='reviewed', _multica_context={'user_id': user, 'stream_id': session, 'platform': 'qq'})
                self.assertFalse(result['success'], result)
        self.assertFalse(any(b['operation'] == 'confirm_plan' for b in self.sent))

    async def test_stale_version_after_external_refinement(self):
        plan = await self.plan()
        self.f.call(dialogue_id=plan['dialogue_id'], message='changed scope', message_id='new')
        result = await self.confirm(plan)
        self.assertFalse(result['success'])
        self.assertIn('版本已变化', result['content'])
        self.assertFalse(any(b['operation'] == 'confirm_plan' for b in self.sent))

    async def test_ambiguous_version_requires_selection(self):
        plan = await self.plan()
        with self.p.dialogue_bindings.connect() as db:
            db.execute("INSERT INTO bindings SELECT user_id,session_id,'duplicate',message_id,turn_id,version,state FROM bindings")
        result = await self.confirm(plan)
        self.assertFalse(result['success'])
        self.assertIn('多个讨论', result['content'])
        self.assertTrue((await self.confirm(plan, dialogue_id=plan['dialogue_id']))['success'])

    async def test_unknown_create_retries_do_not_create_again(self):
        plan = await self.plan()
        self.f.cli.fail = True
        self.assertFalse((await self.confirm(plan))['success'])
        count = len(self.f.cli.calls)
        self.p.dialogue_bindings = None
        self.f.service = fixtures.Service(self.f.settings, self.f.cli)
        self.assertFalse((await self.confirm(plan))['success'])
        self.assertEqual(len(self.f.cli.calls), count)

    async def test_known_execution_readback_failure_recovers(self):
        plan = await self.plan()
        self.f.cli.readback_fails = True
        self.assertFalse((await self.confirm(plan))['success'])
        self.f.cli.readback_fails = False
        self.assertTrue((await self.confirm(plan))['success'])
        self.assertEqual(self.f.cli.created, 2)

    async def test_concurrent_confirm_and_new_request_keep_original_binding(self):
        plan = await self.plan()
        results = await asyncio.gather(self.confirm(plan), self.confirm(plan),
            self.call('discuss', request_message_id='msg', message='different new task'))
        self.assertTrue(all(r['success'] for r in results), results)
        self.assertEqual(self.f.cli.created, 3)
        confirms = [b for b in self.sent if b['operation'] == 'confirm_plan']
        self.assertTrue(all(b['dialogue_id'] == plan['dialogue_id'] for b in confirms))

    async def test_review_required_and_model_identity_cannot_override_host(self):
        plan = await self.plan()
        for review in ['', ' ', 'x' * 2001]:
            self.assertFalse((await self.call('confirm', version=plan['version'], review=review))['success'])
        result = await self.confirm(plan, user_id='67890', stream_id='other')
        self.assertTrue(result['success'], result)
        self.assertEqual(self.sent[-1]['user_id'], '12345')
        result = await self.p.multica_mika('discuss', request_message_id='msg', message='forged', user_id='12345',
             _multica_context={'platform': 'qq', 'stream_id': 'room', 'user_id': '67890'})
        self.assertFalse(result['success'])

    async def test_pending_pagination_does_not_choose_first_match(self):
        plan = self.f.proposal()
        send = self.mock.target.request.side_effect
        def paged(url, token, body):
            if body['operation'] == 'pending':
                if not body.get('cursor'):
                    return {'dialogues': [{'dialogue_id': plan['dialogue_id'], 'version': plan['version']}],
                            'has_more': True, 'next_cursor': '10'}
                return {'dialogues': [{'dialogue_id': 'duplicate', 'version': plan['version']}], 'has_more': False}
            return send(url, token, body)
        with patch('plugins.local_multica.plugin.request', side_effect=paged):
            self.assertFalse((await self.confirm(plan))['success'])
        self.assertFalse(any(b['operation'] == 'confirm_plan' for b in self.sent))

    async def test_two_proposals_select_exact_version_not_latest(self):
        plan = await self.plan()
        with self.f.service.connect() as db:
            db.execute("INSERT INTO dialogues (id,user_id,session_id,current_turn,state,version) "
                       "VALUES ('newer','12345','room','other','proposal','other-version')")
        self.assertTrue((await self.confirm(plan))['success'])
        self.assertEqual(self.sent[-1]['dialogue_id'], plan['dialogue_id'])

    async def test_real_http_body_contains_id_without_server_fallback(self):
        import threading
        from bridge.server import Server
        self.mock.stop()
        original = self.f.service.handle
        received = []
        def strict(body):
            received.append(body)
            if body['operation'] == 'confirm_plan' and not body.get('dialogue_id'):
                raise fixtures.BridgeError('missing explicit dialogue ID')
            return original(body)
        self.f.service.handle = strict
        server = Server(('127.0.0.1', 0), self.f.service)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        token = self.p.ctx.paths.data_dir / 'test.token'
        token.write_text(self.f.settings.token)
        self.p.config.bridge.token_file = str(token)
        self.p.config.bridge.url = 'http://127.0.0.1:' + str(server.server_port)
        try:
            plan = await self.plan()
            result = await self.confirm(plan)
            self.assertTrue(result['success'], result)
            confirms = [b for b in received if b['operation'] == 'confirm_plan']
            self.assertEqual(len(confirms), 1)
            self.assertEqual(confirms[0]['dialogue_id'], plan['dialogue_id'])
        finally:
            await asyncio.to_thread(server.shutdown)
            thread.join()
            server.server_close()
