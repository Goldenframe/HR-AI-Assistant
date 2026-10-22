import os
import sys
import unittest
from unittest.mock import patch

from helpers import UserError
from server import main, validate_environment


class ServerTests(unittest.TestCase):
    def settings(self):
        return {'TELEGRAM_BOT_TOKEN': '123456:fake_test_token', 'AI_BASE_URL': 'http://localhost:62432/v1',
                'AI_API_KEY': 'fake_test_key', 'TEAM_SOURCE': 'atlassian'}

    def test_missing_settings_fail_without_interactive_login_or_network_calls(self):
        with patch.dict(os.environ, {}, clear=True), patch('bot.main') as run, patch('builtins.print'):
            with self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)
            run.assert_not_called()

    def test_server_entry_always_uses_fictional_team_even_if_environment_requests_real_team(self):
        def check_mode():
            self.assertEqual(sys.argv[1:], ['--demo-team'])
            self.assertEqual(os.environ['TEAM_SOURCE'], 'demo')
        with patch.dict(os.environ, self.settings(), clear=True), patch.object(sys, 'argv', ['server.py']), \
                patch('bot.main', side_effect=check_mode) as run:
            main()
            run.assert_called_once()

    def test_invalid_ai_address_and_missing_key_are_rejected_without_printing_secrets(self):
        values = self.settings()
        values['AI_BASE_URL'] = 'tcp://localhost:62432'
        with self.assertRaises(UserError):
            validate_environment(values)
        values['AI_BASE_URL'] = 'http://localhost:62432/v1'
        values['AI_API_KEY'] = ''
        with self.assertRaises(UserError):
            validate_environment(values)
