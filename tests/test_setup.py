import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from scripts.configure import generate
from scripts.patch_host import ANCHOR, INSERT, patch_source

class SetupTests(unittest.TestCase):
    def options(self):
        return dict(cli_path=sys.executable,workspace_id='00000000-0000-4000-8000-000000000001',
                    project_id='00000000-0000-4000-8000-000000000002',
                    agent_id='00000000-0000-4000-8000-000000000003',users=['12345'])

    def test_configuration_is_private_disabled_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'private'
            result=generate(root,**self.options())
            self.assertEqual(result['mika_id'],result['assignee_id'])
            config=tomllib.loads((root/'plugin/config.toml').read_text())
            self.assertFalse(config['plugin']['enabled'])
            self.assertEqual(config['bridge']['allowed_users'],['12345'])
            token=(root/'bridge.token').read_bytes()
            self.assertEqual(token,(root/'plugin/bridge.token').read_bytes())
            self.assertGreaterEqual(len(token),32)
            with self.assertRaises(FileExistsError):generate(root,**self.options())
            self.assertEqual(token,(root/'bridge.token').read_bytes())

    def test_empty_allowlist_invalid_ids_and_relative_cli_are_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'private'
            for change in ({'users':[]},{'users':['not-a-number']},{'project_id':'guess'}, {'cli_path':'multica'}):
                with self.assertRaises(ValueError):generate(root,**{**self.options(),**change})
                self.assertFalse(root.exists())

    def test_host_patch_is_idempotent_and_rejects_unknown_source(self):
        source='class Host:\n    def invoke(self, invocation, context_payload, payload, legacy_action):\n'+ANCHOR+'\n            pass\n        return payload\n'
        updated=patch_source(source)
        self.assertIn(INSERT,updated)
        self.assertEqual(patch_source(updated),updated)
        for bad in ('unrelated code',source+source,source+'\n# _multica_context\n'):
            with self.assertRaises(ValueError):patch_source(bad)
