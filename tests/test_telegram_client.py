import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from bot import Bot
from helpers import UserError
from telegram_client import TelegramClient


class Connection:
    def __init__(self, host, timeout):
        self.host, self.timeout, self.sock = host, timeout, None
        self.calls, self.closed, self.failure = [], False, False

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise OSError('secret_should_not_be_shown')

    def getresponse(self):
        class Response:
            status = 200
            def read(self):
                return json.dumps({'ok': True, 'result': True}).encode()
        return Response()

    def close(self):
        self.closed = True


class TelegramClientTests(unittest.TestCase):
    def test_connection_reused_within_thread_and_long_poll_timeout_has_margin(self):
        connections = []
        def factory(host, timeout):
            connection = Connection(host, timeout)
            connections.append(connection)
            return connection
        client = TelegramClient('123:fake', use_proxy=False, connection_factory=factory)
        client.request('getMe', {})
        client.request('getUpdates', {'timeout': 5})
        self.assertEqual(len(connections), 1)
        self.assertEqual(len(connections[0].calls), 2)
        self.assertGreater(connections[0].timeout, 5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(client.request, 'getMe', {}).result()
        self.assertEqual(len(connections), 2)

    def test_network_failure_does_not_resend_message_or_expose_error_details(self):
        connection = Connection('api.telegram.org', 10)
        connection.failure = True
        client = TelegramClient('123:fake', use_proxy=False, connection_factory=lambda *args, **kwargs: connection)
        with self.assertRaises(UserError) as error:
            client.request('sendMessage', {'chat_id': 1, 'text': 'example'})
        self.assertEqual(len(connection.calls), 1)
        self.assertTrue(connection.closed)
        self.assertNotIn('secret_should_not_be_shown', str(error.exception))

    def test_explicit_proxy_mode_uses_existing_transport(self):
        client = TelegramClient('123:fake', use_proxy=True)
        with patch('telegram_client.request_json', return_value={'ok': True, 'result': True}) as fetch:
            client.request('getMe', {})
        fetch.assert_called_once()

    def test_callback_action_does_not_wait_for_acknowledgement(self):
        bot = Bot.__new__(Bot)
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        def api(*args, **kwargs):
            entered.set()
            release.wait(2)
        bot.api = api
        bot.action = lambda *args, **kwargs: changed.set()
        bot.callback_acknowledgements = ThreadPoolExecutor(max_workers=1)
        try:
            bot.handle_callback({'id': 'fake', 'from': {'id': 1}, 'data': 'toggle:token:e02',
                                 'message': {'chat': {'type': 'private'}, 'message_id': 2}})
            self.assertTrue(changed.is_set())
            self.assertTrue(entered.wait(1))
        finally:
            release.set()
            bot.callback_acknowledgements.shutdown(wait=True)
