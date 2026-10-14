import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import UserError, load_local_env, save_bot_token


class LocalSettingsTests(unittest.TestCase):
    def test_token_survives_restart_and_other_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('# Local settings\nAI_MODEL=sample-model\nTELEGRAM_BOT_TOKEN=old\n', encoding='utf-8')
            save_bot_token(path, '123456:dummy_test_value')
            with patch.dict(os.environ, {}, clear=True):
                load_local_env(path)
                self.assertEqual(os.environ['TELEGRAM_BOT_TOKEN'], '123456:dummy_test_value')
                self.assertEqual(os.environ['AI_MODEL'], 'sample-model')
            self.assertEqual(path.read_text(encoding='utf-8').count('TELEGRAM_BOT_TOKEN='), 1)
            self.assertFalse(path.with_name('.env.tmp').exists())

    def test_external_settings_take_priority_and_empty_local_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('AI_MODEL="local-model"\nAI_API_KEY=\nUNKNOWN_SETTING=ignored\n', encoding='utf-8')
            with patch.dict(os.environ, {'AI_MODEL': 'external-model'}, clear=True):
                load_local_env(path)
                self.assertEqual(os.environ['AI_MODEL'], 'external-model')
                self.assertNotIn('AI_API_KEY', os.environ)
                self.assertNotIn('UNKNOWN_SETTING', os.environ)

    def test_invalid_multiline_token_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            with self.assertRaises(UserError):
                save_bot_token(path, '123:dummy\nAI_MODEL=injected')
            self.assertFalse(path.exists())

    def test_atlassian_settings_loaded_locally_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('TEAM_SOURCE=atlassian\nTEAM_DIRECTORY=data/team.json\nJIRA_URL=http://localhost:1\n'
                            'JIRA_PERSONAL_TOKEN=local-test-key\nCONFLUENCE_URL=http://localhost:2\n'
                            'CONFLUENCE_PERSONAL_TOKEN=other-test-key\n', encoding='utf-8')
            with patch.dict(os.environ, {'JIRA_PERSONAL_TOKEN': 'external-test-key'}, clear=True):
                load_local_env(path)
                self.assertEqual(os.environ['TEAM_SOURCE'], 'atlassian')
                self.assertEqual(os.environ['TEAM_DIRECTORY'], 'data/team.json')
                self.assertEqual(os.environ['JIRA_PERSONAL_TOKEN'], 'external-test-key')
                self.assertEqual(os.environ['CONFLUENCE_PERSONAL_TOKEN'], 'other-test-key')


if __name__ == '__main__':
    unittest.main()
