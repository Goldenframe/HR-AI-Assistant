import json
from pathlib import Path
import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai import REPORT_VERSION, DEMO_ANSWERS, PREVIOUS_DEMO_ANSWERS, format_report, validate_report, generate_report, add_rating_summary_if_empty
from bot import Bot, FIXTURE
from helpers import UserError
from store import Store
from survey import QUESTIONS, QUESTIONNAIRE, LEGACY_QUESTIONNAIRE, PREVIOUS_QUESTIONNAIRE, RATINGS, SKIP


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3', FIXTURE)
        # Bind the whole fictional team without external Telegram calls.
        with self.store.connect() as db:
            for i, e in enumerate(self.store.employees(), 1):
                db.execute('UPDATE employees SET telegram_id=? WHERE id=?', (i, e['id']))
        self.users = {e['id']: e['telegram_id'] for e in self.store.employees()}
        self.owner, self.hr = self.users['e01'], self.users['hr01']

    def tearDown(self):
        self.tmp.cleanup()

    def create_open(self):
        token, _ = self.store.create(self.owner, selected=['e02', 'e03', 'e04', 'e05', 'e06'])
        self.store.activate(self.owner, token)
        return token

    def answer(self, employee, token, completed=True):
        user = self.users[employee]
        self.store.begin(user, token)
        for i, text in enumerate(DEMO_ANSWERS[0][:len(QUESTIONS) if completed else 1], 1):
            self.store.answer(user, text, i)

    def fake_bot(self):
        bot = Bot.__new__(Bot)
        bot.store, bot.username, bot.settings = self.store, 'example_bot', ('http://localhost', 'dummy', 'dummy')
        bot.reports, bot.pending = queue.Queue(), set()
        bot.enrollment_attempts = {}
        bot.matcher, bot.match_results, bot.match_pending = None, queue.Queue(), set()
        bot.sent = []
        bot.send = lambda user, text, markup=None: bot.sent.append((user, text, markup))
        bot.api = lambda *args, **kwargs: True
        return bot

    def test_colleagues_are_automatically_selected_from_shared_projects(self):
        token, candidates = self.store.create(self.owner)
        self.assertEqual({i['employee'] for i in self.store.invitations(token)}, {'e02', 'e03', 'e04', 'e05', 'e06'})
        self.assertEqual(candidates[0]['id'], 'e02')
        self.assertEqual(len(candidates[0]['projects']), 2)
        self.assertNotIn('e09', {c['id'] for c in candidates})
        self.assertNotIn('e01', {c['id'] for c in candidates})
        self.assertNotIn('hr01', {c['id'] for c in candidates})
        with self.assertRaises(UserError):
            self.store.toggle_colleague(self.owner, token, 'e09')
        for employee in ('e02', 'e03', 'e04'):
            self.store.toggle_colleague(self.owner, token, employee)
        with self.assertRaises(UserError):
            self.store.activate(self.owner, token)
        self.store.toggle_colleague(self.owner, token, 'e02')
        self.store.activate(self.owner, token)
        with self.assertRaises(UserError):
            self.store.toggle_colleague(self.owner, token, 'e03')

    def test_background_matching_creates_reviewable_draft_without_sending_invites(self):
        bot = self.fake_bot()
        snapshot = {'projects': [{'id': 'REAL', 'name': 'Общий проект'}],
                    'memberships': [{'employee': e, 'project': 'REAL', 'sources': ['Jira']}
                                    for e in ('e01', 'e02', 'e03', 'e04')]}
        bot.matcher = SimpleNamespace(collect=lambda: snapshot)
        bot.draft(self.owner)
        user, token, error = bot.match_results.get(timeout=5)
        self.assertEqual(user, self.owner)
        self.assertIsNone(error)
        self.assertEqual(self.store.campaign(token)['state'], 'draft')
        bot.selection_card(user, token)
        self.assertNotIn('Общий проект', bot.sent[-1][1])
        self.assertNotIn('Jira + Jira', bot.sent[-1][1])
        self.assertFalse(any(text.startswith('Приглашение оставить') for _, text, _ in bot.sent))

    def test_pending_matching_blocks_messages_and_old_callbacks_only_for_requester(self):
        bot = self.fake_bot()
        token, _ = self.store.create(self.owner)
        before = self.store.invitations(token)
        bot.match_pending.add(self.owner)
        for text in ('/start', 'Получить обратную связь', 'Оставить отзыв'):
            bot.handle({'chat': {'type':'private'}, 'from':{'id':self.owner}, 'text':text})
            self.assertTrue(bot.sent[-1][2]['remove_keyboard'])
        calls = []
        bot.api = lambda method, **payload: calls.append((method, payload))
        bot.handle_callback({'id':'busy', 'from':{'id':self.owner}, 'message':{'chat':{'type':'private'}, 'message_id':9}, 'data':'toggle:' + token + ':e02'})
        self.assertEqual(self.store.invitations(token), before)
        self.assertIn('Дождитесь списка', calls[-1][1]['text'])
        bot.handle({'chat': {'type':'private'}, 'from':{'id':self.users['e02']}, 'text':'/start'})
        self.assertIn('keyboard', bot.sent[-1][2])
        bot.match_pending.discard(self.owner)
        bot.handle({'chat': {'type':'private'}, 'from':{'id':self.owner}, 'text':'/start'})
        self.assertIn('keyboard', bot.sent[-1][2])

    def test_background_matching_failure_does_not_create_survey(self):
        bot = self.fake_bot()
        def fail():
            raise UserError('Нет доступа к Confluence.')
        bot.matcher = SimpleNamespace(collect=fail)
        bot.draft(self.owner)
        user, token, error = bot.match_results.get(timeout=5)
        self.assertIsNone(token)
        self.assertEqual(error, 'Нет доступа к Confluence.')
        self.assertEqual(self.store.visible(self.owner), [])

    def test_ai_payload_preserves_explicit_question_numbers_when_skips_are_removed(self):
        answer = [RATINGS[-1] if q['type'] == 'rating' else SKIP for q in QUESTIONNAIRE]
        answer[-1] = 'Рекомендую предупреждать о рисках раньше.'
        parsed = {'version': REPORT_VERSION, 'strengths': [], 'development': [], 'contradictions': [], 'advice': [
            {'theme': 'Предупреждать о рисках раньше', 'explanation': 'Коллега предлагает заранее сообщать о задержках.', 'evidence': [
                {'response': 1, 'question': 50, 'quote': answer[-1]}]}]}
        with patch('ai.request_json', return_value={'choices': [{'message': {'content': json.dumps(parsed)}}]}) as fetch:
            report = generate_report([answer], ('http://local', 'fake-key', 'fake-model'))
        payload = fetch.call_args.args[1]
        data = json.loads(payload['messages'][1]['content'].split('\n/no_think')[0])
        self.assertEqual(data['responses'][0]['answers'], [{'question': 50, 'answer': answer[-1]}])
        self.assertEqual(data['questions'][-1]['question'], 50)
        self.assertEqual(report['advice'][0]['count'], 1)

    def test_rating_fallback_is_explicit_and_supported_by_exact_assessments(self):
        rows = [[RATINGS[2] if q['type'] == 'rating' else SKIP for q in QUESTIONNAIRE] for _ in range(3)]
        empty = {k: [] for k in ('strengths', 'development', 'advice', 'contradictions')}
        self.assertTrue(add_rating_summary_if_empty(empty, rows, QUESTIONNAIRE))
        report = validate_report(empty, rows)
        self.assertEqual(report['strengths'], [])
        self.assertTrue(all(item['count'] == 3 for item in report['development']))
        report['rating_fallback'] = True
        self.assertIn('подсчитаны программой', format_report({'subject': 'Тест', 'demo': True}, report, 3))

    def test_one_time_enrollment_cannot_choose_or_reuse_hr_role(self):
        tmp = Store(Path(self.tmp.name) / 'enrollment.sqlite3', FIXTURE)
        codes = tmp.enrollment_codes()
        hr_code = next(code for _, role, code in codes if role == 'hr')
        with self.assertRaises(UserError):
            tmp.enroll(123, 'hr01')
        self.assertEqual(tmp.enroll(123, hr_code)['role'], 'hr')
        with self.assertRaises(UserError):
            tmp.enroll(456, hr_code)

    def test_employee_cannot_request_for_another_or_open_hr_panel(self):
        with self.assertRaises(UserError):
            self.store.create(self.owner, 'e02')
        token, _ = self.store.create(self.hr, 'e01')
        self.assertEqual(self.store.campaign(token)['target'], 'e01')
        with self.assertRaises(UserError):
            self.fake_bot().hr_panel(self.owner)

    def test_only_invited_peers_can_answer_and_draft_does_not_collect(self):
        token, _ = self.store.create(self.owner, selected=['e02', 'e03', 'e04'])
        with self.assertRaises(UserError):
            self.store.begin(self.users['e02'], token)
        self.store.activate(self.owner, token)
        for denied in ('e01', 'e09', 'hr01'):
            with self.assertRaises(UserError):
                self.store.begin(self.users[denied], token)

    def test_minimum_completed_distinct_reviews_and_role_permissions(self):
        token = self.create_open()
        self.answer('e02', token)
        self.answer('e03', token)
        self.answer('e04', token, completed=False)
        with self.assertRaises(UserError):
            self.store.report_data(self.owner, token)
        for i in range(2, len(QUESTIONS) + 1):
            self.store.answer(self.users['e04'], DEMO_ANSWERS[0][i - 1], i)
        for allowed in (self.owner, self.hr):
            self.assertEqual(len(self.store.report_data(allowed, token)[1]), 3)
        with self.assertRaises(UserError):
            self.store.report_data(self.users['e02'], token)
        with self.assertRaises(UserError):
            self.store.begin(self.users['e02'], token)

    def test_repeat_telegram_update_does_not_advance_question(self):
        token = self.create_open()
        user = self.users['e02']
        self.store.begin(user, token)
        self.assertEqual(self.store.answer(user, RATINGS[0], 100), 1)
        self.assertEqual(self.store.answer(user, RATINGS[0], 100), 1)
        self.assertEqual(len(json.loads(self.store.session(user)['answers'])), 1)
        reloaded = Store(self.store.path)
        self.assertEqual(reloaded.answer(user, 'Второй ответ', 101), 2)
        reloaded.cancel(user)
        self.assertEqual(reloaded.responses(token), [])

    def test_fabricated_evidence_and_single_source_contradiction_rejected(self):
        ev = {'response': 1, 'question': 1, 'quote': 'Понятно объясняет сложные задачи'}
        report = {'strengths': [{'theme': 'Понятные объяснения', 'evidence': [ev, ev]}],
                  'development': [], 'advice': [], 'contradictions': []}
        self.assertEqual(validate_report(report, PREVIOUS_DEMO_ANSWERS)['strengths'][0]['count'], 1)
        report['strengths'][0]['evidence'] = [dict(ev, quote='Всегда выполняет задачи в срок')]
        with self.assertRaises(UserError):
            validate_report(report, PREVIOUS_DEMO_ANSWERS)
        report['strengths'] = []
        report['contradictions'] = [{'theme': 'Противоположные мнения', 'evidence': [ev]}]
        with self.assertRaises(UserError):
            validate_report(report, PREVIOUS_DEMO_ANSWERS)

    def test_insufficient_information_is_valid_and_reports_are_different(self):
        report = validate_report({'strengths': [], 'development': [], 'advice': [], 'contradictions': []}, DEMO_ANSWERS)
        c = {'subject': 'Тестовый сотрудник', 'demo': False}
        employee = format_report(c, report, 3, 'employee', 5)
        hr = format_report(c, report, 3, 'hr', 5)
        self.assertIn('Недостаточно конкретных', employee)
        self.assertNotIn('Завершение:', employee)
        self.assertIn('Завершение: 3 из 5', hr)

    def test_buttons_consent_and_completion_flow(self):
        bot = self.fake_bot()
        def message(user, text, mid=1):
            bot.handle({'chat': {'id': user, 'type': 'private'}, 'from': {'id': user},
                        'text': text, 'message_id': mid})
        message(self.owner, 'Получить обратную связь')
        token = self.store.visible(self.owner)[0]['id']
        buttons = bot.sent[-1][2]['inline_keyboard']
        self.assertTrue(any('Мария Иванова' in row[0]['text'] for row in buttons))
        for employee in ('e05', 'e06'):
            bot.action(self.owner, 'toggle', token + ':' + employee)
        bot.action(self.owner, 'launch', token)
        user = self.users['e02']
        bot.action(user, 'join', token)
        self.assertIsNone(self.store.session(user))
        bot.action(user, 'accept', token)
        for i, text in enumerate(DEMO_ANSWERS[0], 10):
            message(user, text, i)
        self.assertEqual(self.store.responses(token), [])
        bot.action(user, 'rvsubmit', 'n:' + token + ':0:0')
        self.assertEqual(len(self.store.responses(token)), 1)

    def test_demo_does_not_send_invites_and_cache_invalidates_on_count_change(self):
        bot = self.fake_bot()
        token = self.store.demo(self.owner, DEMO_ANSWERS)
        self.assertEqual(len(self.store.responses(token)), 3)
        self.assertTrue(self.store.campaign(token)['demo'])
        with self.assertRaises(UserError):
            self.store.begin(self.users['e02'], token)
        report = {'strengths': [], 'development': [], 'advice': [], 'contradictions': []}
        report['_version'] = REPORT_VERSION
        self.store.save_report(token, 3, report)
        self.assertEqual(self.store.cached_report(token, 3), report)
        self.assertIsNone(self.store.cached_report(token, 4))
        bot.request_report(self.owner, token)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn('Тестовый отчёт', bot.sent[0][1])

    def test_invitations_are_automatic_no_links_or_manual_resending(self):
        bot = self.fake_bot()
        with self.store.connect() as db:
            db.execute('UPDATE employees SET telegram_id=NULL WHERE id=?', ('e02',))
        token, _ = self.store.create(self.owner, selected=['e02', 'e03', 'e04', 'e05', 'e06'])
        bot.launch(self.owner, token)
        invitations = [m for m in bot.sent if m[1].startswith('Приглашение оставить')]
        self.assertEqual(len(invitations), 4)
        initiator_text = '\n'.join(text for user, text, _ in bot.sent if user == self.owner)
        self.assertNotIn('https://t.me/', initiator_text)
        self.assertNotIn('Передайте', initiator_text)
        self.assertEqual(self.store.delivery_stats(self.owner, token)['waiting'], 1)
        bot.sent.clear()
        codes = self.store.enrollment_codes()
        code = next(code for name, _, code in codes if name == 'Мария Иванова')
        bot.handle({'chat': {'id': 888, 'type': 'private'}, 'from': {'id': 888},
                    'text': code, 'message_id': 1})
        self.assertEqual(sum(user == 888 and text.startswith('Приглашение оставить')
                             for user, text, _ in bot.sent), 1)
        bot.sent.clear()
        bot.dispatch_invitations()
        self.assertEqual(bot.sent, [])

    def test_failed_delivery_retries_after_restart_and_stops_after_five_attempts(self):
        token = self.create_open()
        user = self.users['e02']
        self.store.mark_delivery(token, 'e02', 'failed', now=100)
        reloaded = Store(self.store.path)
        self.assertEqual(reloaded.due_invitations(user=user, now=120), [])
        self.assertEqual(len(reloaded.due_invitations(user=user, now=131)), 1)
        for attempt in range(4):
            reloaded.mark_delivery(token, 'e02', 'failed', now=200 + attempt * 1000)
        self.assertEqual(reloaded.due_invitations(user=user, now=9999), [])

    def test_old_manual_invites_are_migrated_to_automatic_queue(self):
        token = self.create_open()
        with self.store.connect() as db:
            db.execute("UPDATE invitations SET delivery='manual' WHERE campaign=? AND employee='e02'", (token,))
        reloaded = Store(self.store.path)
        due = reloaded.due_invitations(user=self.users['e02'])
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]['delivery'], 'waiting')

    def test_corporate_questions_and_buttons_preserve_incomplete_progress(self):
        bot = self.fake_bot()
        token = self.create_open()
        user = self.users['e02']
        bot.action(user, 'accept', token)
        self.assertIn('Общее впечатление', bot.sent[-1][1])
        self.assertEqual(len(QUESTIONNAIRE), 50)
        self.assertEqual(sum(q['type'] == 'rating' for q in QUESTIONNAIRE), 43)
        self.assertEqual([r[0]['text'] for r in bot.sent[-1][2]['keyboard'][:-1]], list(RATINGS))
        with self.assertRaises(UserError):
            self.store.answer(user, 'Любой текст вместо оценки', 999)
        self.assertEqual(json.loads(self.store.session(user)['answers']), [])
        for i, q in enumerate(QUESTIONNAIRE[:-1], 1):
            text = RATINGS[-1] if q['type'] == 'rating' else SKIP
            bot.handle({'chat': {'id': user, 'type': 'private'}, 'from': {'id': user},
                        'text': text, 'message_id': i})
        self.assertEqual(self.store.responses(token), [])
        self.assertEqual(len(json.loads(self.store.session(user)['answers'])), 49)
        self.assertEqual(bot.sent[-1][2]['inline_keyboard'][0][0]['text'], 'Пропустить комментарий')
        bot.handle({'chat': {'id': user, 'type': 'private'}, 'from': {'id': user},
                    'text': SKIP, 'message_id': 50})
        bot.action(user, 'rvsubmit', 'n:' + token + ':0:0')
        self.assertEqual(len(self.store.responses(token)[0]), 50)

    def test_inline_comment_skip_advances_once_and_rejects_old_question_button(self):
        bot = self.fake_bot()
        token = self.create_open()
        user = self.users['e02']
        bot.action(user, 'accept', token)
        self.store.answer(user, RATINGS[0])
        bot.ask_question(user, token, 1)
        self.assertEqual(bot.sent[-1][2]['inline_keyboard'][0][0]['text'], 'Пропустить комментарий')
        bot.action(user, 'skipanswer', token + ':1')
        self.assertEqual(json.loads(self.store.session(user)['answers']), [RATINGS[0], SKIP])
        with self.assertRaises(UserError):
            bot.action(user, 'skipanswer', token + ':1')
        self.assertEqual(len(json.loads(self.store.session(user)['answers'])), 2)

    def test_rating_evidence_and_computed_distribution(self):
        answers = [[RATINGS[-1] if q['type'] == 'rating' else SKIP for q in QUESTIONNAIRE] for _ in range(3)]
        empty = {k: [] for k in ('strengths', 'development', 'advice', 'contradictions')}
        report = validate_report(empty, answers)
        self.assertTrue(all(value == 0 for value in report['coverage'].values()))
        answers[0][0] = RATINGS[2]
        report = validate_report(empty, answers)
        self.assertEqual(report['ratings']['Общее впечатление'][RATINGS[2]], 1)
        empty['development'] = [{'theme': 'Есть сомнения в совместной работе', 'evidence': [
            {'response': 1, 'question': 1, 'quote': RATINGS[2]}]}]
        self.assertEqual(validate_report(empty, answers)['development'][0]['count'], 1)
        empty['development'][0]['evidence'][0]['quote'] = 'согласен'
        with self.assertRaises(UserError):
            validate_report(empty, answers)
        empty['development'][0]['evidence'][0]['quote'] = RATINGS[-1]
        with self.assertRaises(UserError):
            validate_report(empty, answers)
        empty['development'][0]['evidence'][0]['quote'] = RATINGS[2]
        empty['advice'] = empty['development']
        empty['development'] = []
        with self.assertRaises(UserError):
            validate_report(empty, answers)

    def test_existing_eight_question_snapshot_keeps_progress_after_restart(self):
        token = self.create_open()
        with self.store.connect() as db:
            db.execute('UPDATE campaigns SET questionnaire=? WHERE id=?',
                       (json.dumps(PREVIOUS_QUESTIONNAIRE, ensure_ascii=False), token))
        user = self.users['e02']
        self.store.begin(user, token)
        self.store.answer(user, PREVIOUS_DEMO_ANSWERS[0][0], 1)
        reloaded = Store(self.store.path)
        for i, text in enumerate(PREVIOUS_DEMO_ANSWERS[0][1:], 2):
            reloaded.answer(user, text, i)
        self.assertEqual(reloaded.responses(token), [PREVIOUS_DEMO_ANSWERS[0]])
        self.assertEqual(reloaded.campaign(token)['questions'], PREVIOUS_QUESTIONNAIRE)

    def test_skip_is_not_evidence_and_eighth_question_is_valid(self):
        empty = {k: [] for k in ('strengths', 'development', 'advice', 'contradictions')}
        report = validate_report(empty, [[SKIP] * 8 for _ in range(3)])
        self.assertEqual(list(report['coverage'].values()), [0, 0, 0, 0])
        empty['advice'] = [{'theme': 'Раньше сообщать о рисках', 'evidence': [
            {'response': 1, 'question': 8, 'quote': 'Рекомендую раньше сообщать о рисках.'}]}]
        report = validate_report(empty, PREVIOUS_DEMO_ANSWERS)
        self.assertEqual(report['advice'][0]['sections'], ['Самостоятельность и развитие'])
        empty['advice'][0]['evidence'] = [{'response': 1, 'question': 6, 'quote': SKIP}]
        with self.assertRaises(UserError):
            validate_report(empty, PREVIOUS_DEMO_ANSWERS)

    def test_legacy_three_question_survey_can_finish_after_update(self):
        token = self.create_open()
        with self.store.connect() as db:
            db.execute('UPDATE campaigns SET questionnaire=NULL WHERE id=?', (token,))
        reloaded = Store(self.store.path)
        self.assertEqual(reloaded.campaign(token)['questions'], LEGACY_QUESTIONNAIRE)
        user = self.users['e02']
        reloaded.begin(user, token)
        for i, text in enumerate(('Понятно объясняет задачи.', 'Поздно сообщает о рисках.', 'Рекомендую сообщать о рисках раньше.'), 1):
            self.assertEqual(reloaded.answer(user, text, i), i)
        self.assertEqual(len(reloaded.responses(token)[0]), 3)
        new_token, _ = reloaded.create(self.owner)
        self.assertEqual(reloaded.campaign(new_token)['questions'], QUESTIONNAIRE)


if __name__ == '__main__':
    unittest.main()
