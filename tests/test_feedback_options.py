import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai import DEMO_ANSWERS
from bot import Bot, FIXTURE
from helpers import UserError
from store import Store


class FeedbackOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3', FIXTURE)
        with self.store.connect() as db:
            for number, e in enumerate(self.store.employees(), 1):
                db.execute('UPDATE employees SET telegram_id=? WHERE id=?', (number, e['id']))
        self.users = {e['id']: e['telegram_id'] for e in self.store.employees()}
        self.owner = self.users['e01']
        self.token, _ = self.store.create(self.owner)
        self.bot = Bot.__new__(Bot)
        self.bot.store = self.store
        self.sent = []
        self.bot.send = lambda user, text, markup=None: self.sent.append((user, text, markup))

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_add_is_durable_deduplicated_and_does_not_require_shared_project(self):
        self.assertIn('e09', {e['id'] for e in self.store.additional_colleagues(self.owner, self.token)})
        self.store.add_colleague(self.owner, self.token, 'e09')
        self.store.add_colleague(self.owner, self.token, 'e09')
        reloaded = Store(self.store.path)
        candidates = reloaded.campaign_candidates(self.token)
        self.assertEqual(sum(e['id'] == 'e09' for e in candidates), 1)
        self.assertTrue(next(e for e in candidates if e['id'] == 'e09')['manual'])
        self.assertEqual(sum(i['employee'] == 'e09' for i in reloaded.invitations(self.token)), 1)
        reloaded.toggle_colleague(self.owner, self.token, 'e09')
        self.assertNotIn('e09', {i['employee'] for i in reloaded.invitations(self.token)})
        reloaded.toggle_colleague(self.owner, self.token, 'e09')
        reloaded.activate(self.owner, self.token)
        with self.assertRaises(UserError):
            reloaded.add_colleague(self.owner, self.token, 'e07')

    def test_manual_add_rejects_wrong_user_self_and_hr(self):
        for employee in ('e01', 'hr01', 'unknown'):
            with self.assertRaises(UserError):
                self.store.add_colleague(self.owner, self.token, employee)
        with self.assertRaises(UserError):
            self.store.add_colleague(self.users['e02'], self.token, 'e09')

    def test_bulk_selection_covers_all_pages_and_manual_additions_without_duplicates(self):
        with self.store.connect() as db:
            for i in range(10, 14):
                db.execute('INSERT INTO employees(id,name,role) VALUES(?,?,?)', (f'e{i}', f'Extra {i}', 'employee'))
        for employee in ('e09', 'e10', 'e11', 'e12', 'e13'):
            self.store.add_colleague(self.owner, self.token, employee)
        expected = {e['id'] for e in self.store.campaign_candidates(self.token)}
        self.assertGreater(len(expected), 8)
        self.bot.action(self.owner, 'none', self.token + ':1')
        self.assertEqual(self.store.invitations(self.token), [])
        self.assertEqual({e['id'] for e in self.store.campaign_candidates(self.token)}, expected)
        with self.assertRaises(UserError):
            self.store.activate(self.owner, self.token)
        self.bot.action(self.owner, 'all', self.token + ':1')
        self.bot.action(self.owner, 'all', self.token + ':1')
        self.assertEqual({i['employee'] for i in self.store.invitations(self.token)}, expected)
        self.assertEqual(len(self.store.invitations(self.token)), len(expected))
        self.assertIn('Выбрано коллег для отзыва: ' + str(len(expected)), self.sent[-1][1])
        buttons = [b['text'] for row in self.sent[-1][2]['inline_keyboard'] for b in row]
        self.assertIn('Выбрать всех', buttons)
        self.assertIn('Снять выбор со всех', buttons)

    def test_bulk_selection_checks_owner_and_preserves_launched_survey(self):
        before = self.store.invitations(self.token)
        with self.assertRaises(UserError):
            self.store.set_all_colleagues(self.users['e02'], self.token, False)
        self.store.activate(self.owner, self.token)
        with self.assertRaises(UserError):
            self.store.set_all_colleagues(self.owner, self.token, False)
        self.assertEqual(self.store.invitations(self.token), before)

    def test_manual_picker_and_added_profile_appear_in_buttons(self):
        self.bot.colleague_picker(self.owner, self.token)
        self.assertTrue(any('София Лебедева' in row[0]['text'] for row in self.sent[-1][2]['inline_keyboard']))
        self.bot.action(self.owner, 'add', self.token + ':e09')
        self.assertTrue(any('София Лебедева' in button['text'] for row in self.sent[-1][2]['inline_keyboard'] for button in row))

    def test_colleague_search_finds_matching_names_and_cancels_cleanly(self):
        self.bot.action(self.owner, 'find', self.token)
        self.bot.handle({'chat': {'type': 'private'}, 'from': {'id': self.owner}, 'text': 'София'})
        names = [row[0]['text'] for row in self.sent[-1][2]['inline_keyboard']]
        self.assertIn('София Лебедева', names)
        self.assertNotIn('Елена Морозова', names)
        self.assertEqual(self.store.metadata('colleague-search:' + str(self.owner)), '')

    def test_new_addition_resets_search_but_result_pagination_keeps_it(self):
        for number in range(10, 20):
            with self.store.connect() as db:
                db.execute('INSERT INTO employees(id,name,role) VALUES(?,?,?)', (f'e{number}', f'Ak {number}', 'employee'))
        self.bot.colleague_picker(self.owner, self.token, query='Ak')
        self.bot.action(self.owner, 'pick', self.token + ':1')
        self.assertIn('Поиск: Ak', self.sent[-1][1])
        self.bot.action(self.owner, 'add', self.token + ':e10')
        self.assertEqual(self.store.metadata(f'colleague-query:{self.owner}:{self.token}'), 'Ak')
        self.bot.action(self.owner, 'pick', self.token)
        self.assertNotIn('Поиск:', self.sent[-1][1])
        self.bot.action(self.owner, 'pick', self.token + ':2')
        names = [button['text'] for row in self.sent[-1][2]['inline_keyboard'] for button in row]
        self.assertIn('София Лебедева', names)
        self.assertNotIn('Поиск:', self.sent[-1][1])
        self.assertNotIn('Ak 10', names)
        self.bot.colleague_picker(self.owner, self.token, query='София')
        self.bot.action(self.owner, 'select', self.token)
        self.bot.action(self.owner, 'pick', self.token)
        self.assertNotIn('Поиск:', self.sent[-1][1])

    def test_adding_colleague_stays_on_same_search_page_without_shifting_names(self):
        with self.store.connect() as db:
            for number in range(10, 28):
                db.execute('INSERT INTO employees(id,name,role) VALUES(?,?,?)', (f'e{number}', f'Dash {number}', 'employee'))
        self.bot.colleague_picker(self.owner, self.token, page=1, query='Dash')
        original = [row[0]['text'] for row in self.sent[-1][2]['inline_keyboard'][:8]]
        self.bot.action(self.owner, 'add', self.token + ':e18:1')
        self.assertIn('Страница 2', self.sent[-1][1])
        self.assertIn('Поиск: Dash', self.sent[-1][1])
        current = [row[0]['text'].removeprefix('✓ ') for row in self.sent[-1][2]['inline_keyboard'][:8]]
        self.assertEqual(current, original)
        self.assertEqual(self.sent[-1][2]['inline_keyboard'][0][0]['text'], '✓ Dash 18')
        self.bot.action(self.owner, 'add', self.token + ':e19:1')
        self.assertIn('Страница 2', self.sent[-1][1])
        self.assertTrue({'e18', 'e19'} <= {i['employee'] for i in self.store.invitations(self.token)})
        self.bot.action(self.owner, 'add', self.token + ':e18:1')
        self.assertNotIn('e18', {i['employee'] for i in self.store.invitations(self.token)})

    def test_catalog_navigation_updates_one_message_instead_of_sending_pages(self):
        with self.store.connect() as db:
            for i in range(10, 20):
                db.execute('INSERT INTO employees(id,name,role) VALUES(?,?,?)', (f'e{i}', f'Extra {i}', 'employee'))
        edits = []
        self.bot.api = lambda method, **payload: edits.append((method, payload))
        self.bot.action(self.owner, 'pick', self.token, message_id=77)
        self.bot.action(self.owner, 'pick', self.token + ':1', message_id=77)
        self.bot.action(self.owner, 'pickall', self.token, message_id=77)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(method == 'editMessageText' and payload['message_id'] == 77 for method, payload in edits))
        self.assertIn('Страница 2', edits[1][1]['text'])

    def test_search_and_return_keep_the_catalog_message_and_clear_search_state(self):
        edits = []
        self.bot.api = lambda method, **payload: edits.append((method, payload))
        self.bot.action(self.owner, 'find', self.token, message_id=77)
        self.bot.handle({'chat': {'type': 'private'}, 'from': {'id': self.owner}, 'text': 'София'})
        self.assertEqual(self.sent, [])
        self.assertEqual([payload['message_id'] for _, payload in edits], [77, 77])
        self.assertIn('Поиск: София', edits[-1][1]['text'])
        self.bot.action(self.owner, 'find', self.token, message_id=77)
        self.bot.action(self.owner, 'pickall', self.token, message_id=77)
        self.assertEqual(self.store.metadata('colleague-search:' + str(self.owner)), '')
        self.assertEqual(self.store.metadata('colleague-search-message:' + str(self.owner)), '')
        self.bot.action(self.owner, 'find', self.token)
        self.bot.handle({'chat': {'type': 'private'}, 'from': {'id': self.owner}, 'text': '/start'})
        self.assertEqual(self.store.metadata('colleague-search:' + str(self.owner)), '')

    def test_reminder_survives_restart_rescheduling_replaces_previous_time(self):
        self.store.activate(self.owner, self.token)
        user = self.users['e02']
        self.store.schedule_reminder(user, self.token, 300, now=1000)
        reloaded = Store(self.store.path)
        self.assertEqual(reloaded.due_reminders(now=1299), [])
        self.assertEqual(len(reloaded.due_reminders(now=1300)), 1)
        reloaded.schedule_reminder(user, self.token, 3600, now=1200)
        self.assertEqual(reloaded.due_reminders(now=1300), [])
        self.assertEqual(len(reloaded.due_reminders(now=4800)), 1)
        with self.assertRaises(UserError):
            reloaded.schedule_reminder(user, self.token, 999, now=1200)
        with self.assertRaises(UserError):
            reloaded.schedule_reminder(self.users['e09'], self.token, 300, now=1200)

    def test_reminder_sent_once_and_canceled_on_start(self):
        self.store.activate(self.owner, self.token)
        user = self.users['e02']
        self.store.schedule_reminder(user, self.token, 300, now=0)
        self.bot.dispatch_reminders()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], user)
        self.assertIn('Напоминание', self.sent[0][1])
        self.bot.dispatch_reminders()
        self.assertEqual(len(self.sent), 1)
        self.store.schedule_reminder(user, self.token, 300, now=0)
        self.store.begin(user, self.token)
        self.assertEqual(self.store.due_reminders(), [])

    def test_refusal_cancels_reminder_and_partial_answer_only_for_that_campaign(self):
        self.store.activate(self.owner, self.token)
        user = self.users['e02']
        self.store.begin(user, self.token)
        self.store.answer(user, DEMO_ANSWERS[0][0])
        self.store.schedule_reminder(user, self.token, 300, now=0)
        self.bot.action(user, 'decline', self.token)
        self.assertIsNone(self.store.session(user))
        self.assertEqual(self.store.due_reminders(), [])

    def test_delivery_failure_retries_with_limit_and_completed_response_cancels(self):
        self.store.activate(self.owner, self.token)
        user = self.users['e02']
        self.store.schedule_reminder(user, self.token, 300, now=0)
        for _ in range(5):
            reminder = self.store.due_reminders(now=1000)[0]
            self.store.reminder_failed(self.token, 'e02', reminder['due'], now=0)
        self.assertEqual(self.store.due_reminders(now=1000), [])
        self.store.begin(user, self.token)
        self.store.schedule_reminder(user, self.token, 300, now=0)
        for answer in DEMO_ANSWERS[0]:
            self.store.answer(user, answer)
        self.assertEqual(self.store.due_reminders(now=1000), [])

    def test_original_answers_access_threshold_comments_and_no_ai_call(self):
        token = self.store.demo(self.owner, DEMO_ANSWERS)
        self.bot.show_answers(self.owner, token, comments=True)
        self.assertIn(DEMO_ANSWERS[0][1], self.sent[0][1])
        self.assertIn('Отзыв №1', self.sent[0][1])
        self.assertNotIn('Мария Иванова', self.sent[0][1])
        self.assertTrue(any(row[0]['text'] == 'Все вопросы' for row in self.sent[-1][2]['inline_keyboard']))
        with self.assertRaises(UserError):
            self.bot.show_answers(self.users['e02'], token)
        self.bot.show_answers(self.users['hr01'], token)
        self.store.activate(self.owner, self.token)
        with self.assertRaises(UserError):
            self.bot.show_answers(self.owner, self.token)

    def test_consent_discloses_original_answers_and_snooze_preserves_progress(self):
        self.store.activate(self.owner, self.token)
        user = self.users['e02']
        self.bot.consent(user, self.token)
        self.assertIn('исходные оценки и комментарии', self.sent[-1][1])
        self.assertIn('Напомнить позже', [row[0]['text'] for row in self.sent[-1][2]['inline_keyboard']])
        self.store.begin(user, self.token)
        self.store.answer(user, DEMO_ANSWERS[0][0])
        self.bot.action(user, 'snooze', self.token + ':300')
        self.assertEqual(len(json.loads(self.store.session(user)['answers'])), 1)
        self.bot.action(user, 'resume', self.token)
        self.assertIn('Вопрос 2', self.sent[-1][1])
        self.assertEqual(self.store.due_reminders(now=10**12), [])
