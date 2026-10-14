"""Telegram entry point for HR AI Assistant. No third-party dependencies."""
import argparse
import getpass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ai import DEMO_ANSWERS, generate_report, format_report
from helpers import UserError, ai_settings, load_local_env, save_bot_token
from store import Store, MIN_RESPONSES, REMINDER_DELAYS
from survey import SKIP, question_message, answer_keyboard
from atlassian import AtlassianMatcher
from telegram_client import TelegramClient
from trial import Trial
from review import AnswerReview

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures' / 'team.json'
REMINDER_LABELS = ('Через 5 минут', 'Через 1 час', 'Через 3 часа', 'Через 1 день', 'Через 2 дня', 'Через неделю')


def inline(rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row]
                                for row in rows]}


class Bot:
    def __init__(self, token, store, settings, matcher=None):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.telegram = TelegramClient(token)
        self.callback_acknowledgements = ThreadPoolExecutor(max_workers=1, thread_name_prefix='telegram-ack')
        self.store, self.settings = store, settings
        self.trial = Trial(store)
        self.reports, self.pending = queue.Queue(), set()
        self.enrollment_attempts = {}
        self.last_dispatch = 0
        self.matcher = matcher
        self.match_results, self.match_pending = queue.Queue(), set()
        me = self.api('getMe')
        self.username, self.bot_id = me['username'], me['id']
        if self.api('getWebhookInfo').get('url'):
            raise UserError('У бота настроен webhook. Для этого прототипа используйте отдельного бота.')

    def api(self, method, **payload):
        response = self.telegram.request(method, payload)
        if not response.get('ok'):
            raise UserError('Telegram не выполнил запрос. Проверьте настройки бота.')
        return response['result']

    def send(self, chat, text, markup=None):
        for start in range(0, len(text), 3500):
            payload = {'chat_id': chat, 'text': text[start:start + 3500]}
            if start == 0 and markup:
                payload['reply_markup'] = markup
            self.api('sendMessage', **payload)

    def menu(self, user):
        if getattr(self, 'trial', None) and self.trial.session(user):
            self.ask_trial(user)
            return
        p = self.store.require_profile(user)
        rows = [['Получить обратную связь', 'Оставить отзыв'], ['Мои отчёты', 'Демо']]
        if getattr(self, 'matcher', None):
            rows[1] = ['Мои отчёты']
        if p['role'] == 'hr':
            rows.insert(0, ['Панель HR'])
        s = self.store.session(user)
        text = "Запросите обратную связь о своей работе или оставьте отзыв коллеге.\n\nГотовые отчёты — в разделе «Мои отчёты»."
        if s:
            questions = self.store.campaign(s['campaign'])['questions']
            answers = json.loads(s['answers'])
            if len(answers) == len(questions):
                self.ask_question(user, s['campaign'], len(answers))
                return
            text += '\n\nУ вас есть незавершённый опрос.\n' + question_message(questions, len(answers), answers)
            self.send(user, text, self.question_markup(questions, len(answers), s['campaign']))
            return
        self.send(user, text, {'keyboard': [[{'text': v} for v in row] for row in rows],
                              'resize_keyboard': True, 'is_persistent': True})

    def draft(self, user, target=None):
        profile = self.store.require_profile(user)
        target = target or profile['id']
        if profile['role'] != 'hr' and target != profile['id']:
            raise UserError('Вы можете запрашивать обратную связь только о себе.')
        if getattr(self, 'matcher', None) is None:
            token, _ = self.store.create(user, target)
            self.selection_card(user, token)
            return
        if user in self.match_pending or len(self.match_pending) >= 2:
            raise UserError('Подбор коллег уже выполняется. Дождитесь результата.')
        self.send(user, 'Подбираю коллег по совместным проектам в Jira и Confluence.\nПожалуйста, дождитесь списка — на время подбора кнопки недоступны.', {'remove_keyboard': True})
        self.match_pending.add(user)
        def worker():
            try:
                snapshot = self.matcher.collect()
                self.store.sync_participation(snapshot)
                token, _ = self.store.create(user, target)
                self.store.metadata('matching-warnings:' + token, json.dumps(snapshot.get('warnings', [])))
                self.match_results.put((user, token, None))
            except UserError as exc:
                self.match_results.put((user, None, str(exc)))
            except Exception:
                self.match_results.put((user, None, 'Не удалось подобрать коллег. Проверьте подключение и корпоративный каталог.'))
        threading.Thread(target=worker, daemon=True).start()

    def selection_card(self, user, token, message_id=None, page=0):
        self.store.controlled(user, token)
        c = self.store.campaign(token)
        if c['state'] != 'draft':
            raise UserError('Список этого опроса уже подтверждён или отменён.')
        selected = {i['employee'] for i in self.store.invitations(token)}
        candidates = self.store.campaign_candidates(token)
        page = max(0, min(page, max(0, (len(candidates) - 1) // 8)))
        visible = candidates[page * 8:(page + 1) * 8]
        pages = max(1, (len(candidates) + 7) // 8)
        text = (f"Обратная связь для: {c['subject']}\n\n"
                f'Выбрано коллег для отзыва: {len(selected)}.\n'
                f'Для запуска опроса выберите минимум {MIN_RESPONSES} коллег.\n'
                f'Страница {page + 1} из {pages}\n\n'
                'Отметьте коллег кнопками ниже. Повторное нажатие снимает выбор.\n'
                'Можно добавить сотрудника из каталога.\n\n'
                'Приглашения отправятся после нажатия «Запустить опрос».')
        if any('Демо' in source for e in candidates for reason in e.get('reasons', [])
               for source in reason['target_sources'] + reason['peer_sources']):
            text = 'ДЕМО · участие в проектах вымышленное\n' + text
        if json.loads(self.store.metadata('matching-warnings:' + token) or '[]'):
            text += '\n\nНе всех коллег удалось проверить автоматически. При необходимости добавьте их из каталога.'
        if len(candidates) < MIN_RESPONSES:
            text += '\n\nДобавьте коллег: для запуска нужно минимум три человека.'
        rows = [[(('✓ ' if e['id'] in selected else '○ ') + e['name'],
                  f'toggle:{token}:{e["id"]}:{page}')] for e in visible]
        navigation = []
        if page:
            navigation.append(('← Назад', f'select:{token}:{page - 1}'))
        if (page + 1) * 8 < len(candidates):
            navigation.append(('Далее →', f'select:{token}:{page + 1}'))
        if navigation:
            rows.append(navigation)
        rows.append([('Выбрать всех', f'all:{token}:{page}'), ('Снять выбор со всех', f'none:{token}:{page}')])
        rows.append([('Добавить коллегу', 'pick:' + token)])
        rows += [[('Запустить опрос', 'launch:' + token)], [('Отменить', 'discard:' + token)]]
        if message_id is not None:
            self.api('editMessageText', chat_id=user, message_id=message_id, text=text, reply_markup=inline(rows))
        else:
            self.send(user, text, inline(rows))

    def colleague_picker(self, user, token, page=0, query=None, message_id=None):
        query_key = f'colleague-query:{user}:{token}'
        if query is not None:
            self.store.metadata(query_key, query)
        query = self.store.metadata(query_key) or ''
        self.store.additional_colleagues(user, token, query)  # Validate draft access.
        campaign = self.store.campaign(token)
        people = [e for e in self.store.employees() if e['role'] == 'employee'
                  and e['id'] != campaign['target'] and query.casefold() in e['name'].casefold()]
        selected = {i['employee'] for i in self.store.invitations(token)}
        page = max(0, min(page, max(0, (len(people) - 1) // 8)))
        rows = [[(('✓ ' if e['id'] in selected else '') + e['name'], f'add:{token}:{e["id"]}:{page}')]
                for e in people[page * 8:(page + 1) * 8]]
        navigation = []
        if page:
            navigation.append(('← Назад', f'pick:{token}:{page - 1}'))
        if (page + 1) * 8 < len(people):
            navigation.append(('Далее →', f'pick:{token}:{page + 1}'))
        if navigation:
            rows.append(navigation)
        rows.append([('Поиск по имени', 'find:' + token), ('Весь каталог', 'pickall:' + token)])
        rows.append([('Готово, к списку коллег', 'select:' + token)])
        text = ('Отметьте коллег, которых хотите добавить. Повторное нажатие снимает выбор.\n'
                  f'Выбрано коллег: {len(selected)}.\n'
                  + ('Ничего не найдено. Попробуйте другой запрос или весь каталог.' if not people else f'Страница {page + 1}.')
                  + (f'\nПоиск: {query}' if query else ''))
        if message_id is not None:
            self.api('editMessageText', chat_id=user, message_id=message_id, text=text, reply_markup=inline(rows))
        else:
            self.send(user, text, inline(rows))

    def reminder_picker(self, user, token):
        self.store.eligible(user, token)
        self.send(user, 'Когда напомнить об этом отзыве?', inline(
            [[(label, f'snooze:{token}:{seconds}')] for label, seconds in zip(REMINDER_LABELS, REMINDER_DELAYS)]
            + [[('Не менять напоминание', 'join:' + token)]]))

    def dispatch_reminders(self):
        for reminder in self.store.due_reminders():
            user, token = reminder['telegram_id'], reminder['campaign']
            session = self.store.session(user)
            action = 'resume' if session and session['campaign'] == token else 'join'
            try:
                self.send(user, f"Напоминание: вы отложили отзыв о сотруднике {reminder['subject']}.\n"
                          'Можете ответить сейчас или выбрать другое время.', inline(
                              [[('Продолжить отзыв' if action == 'resume' else 'Оставить отзыв', action + ':' + token)],
                               [('Напомнить позже', 'remind:' + token)]]))
                self.store.reminder_delivered(token, reminder['employee'], reminder['due'])
            except UserError:
                self.store.reminder_failed(token, reminder['employee'], reminder['due'])

    def show_answers(self, user, token, index=0, comments=False):
        c, answers = self.store.report_data(user, token)
        questions = c['questions']
        indices = [i for i, q in enumerate(questions) if not comments or q.get('type') != 'rating']
        if index not in indices:
            index = indices[0]
        question = questions[index]
        text = ('ДЕМО · вымышленные ответы\n' if c['demo'] else '')
        text += (f"Исходные ответы о сотруднике: {c['subject']}\nРаздел: {question['section']}\n"
                 f"Вопрос {index + 1} из {len(questions)}: {question['text']}\n\n"
                 'Номера отзывов не раскрывают профили авторов. Текст приводится без обработки ИИ.\n')
        for number, response in enumerate(answers, 1):
            text += f'\nОтзыв №{number}:\n{response[index]}\n'
        self.send(user, text)
        position = indices.index(index)
        prefix = 'comments' if comments else 'answers'
        navigation = []
        if position:
            navigation.append(('← Предыдущий', f'{prefix}:{token}:{indices[position - 1]}'))
        if position + 1 < len(indices):
            navigation.append(('Следующий →', f'{prefix}:{token}:{indices[position + 1]}'))
        rows = [navigation] if navigation else []
        rows.append([('Все вопросы' if comments else 'Только комментарии',
                      ('answers:' if comments else 'comments:') + token)])
        rows.append([('К опросу', 'status:' + token)])
        self.send(user, 'Просмотр исходных ответов:', inline(rows))

    def question_markup(self, questions, index, token, slot=None):
        if questions[index].get('type') == 'rating':
            return answer_keyboard(questions[index])
        suffix = f'{token}:{slot}:{index}' if slot is not None else f'{token}:{index}'
        mode = 'trial' if slot is not None else 'answer'
        return inline([[('Пропустить комментарий', f'skip{mode}:{suffix}')],
                       [('Отменить ответ', f'cancel{mode}:{suffix}')]])

    def ask_question(self, user, token, index):
        questions = self.store.campaign(token)['questions']
        session = self.store.session(user)
        answers = json.loads(session['answers']) if session else []
        if session and session.get('edit_index') is not None:
            index = session['edit_index']
        elif len(answers) == len(questions):
            self.review_summary(user, token)
            return
        prefix = ('Изменение ответа\nТекущий ответ: ' + answers[index] + '\n\n') if session and session.get('edit_index') is not None else ''
        self.send(user, prefix + question_message(questions, index, answers), self.question_markup(questions, index, token))

    def campaign_label(self, campaign, include_subject=False):
        created = datetime.fromisoformat(campaign['created']).replace(tzinfo=timezone.utc)
        date = created.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')
        count = len(self.store.responses(campaign['id']))
        state = {'draft': 'Черновик', 'open': f'Сбор · отзывов: {count}', 'cancelled': 'Отменён'}[campaign['state']]
        prefix = ('ДЕМО · ' if campaign['demo'] else '')
        subject = campaign['subject'] + ' · ' if include_subject else ''
        return f"{prefix}{subject}№{campaign['number']} · {date} · {state}"

    def campaign_card(self, user, token):
        c, invited, count = self.store.stats(user, token)
        state = {'draft': 'ожидает подтверждения', 'open': 'сбор открыт', 'cancelled': 'отменён'}[c['state']]
        text = f"Опрос №{c['number']} · {c['subject']}\nСтатус: {state}.\nПолучено отзывов: {count}."
        if c['state'] == 'open' and count < MIN_RESPONSES:
            remaining = MIN_RESPONSES - count
            word = 'отзыв' if remaining == 1 else 'отзыва'
            text += f"\nДля отчёта нужно ещё {remaining} {word}."
        if c['demo']:
            text = 'ДЕМО · вымышленные отзывы\n' + text
        if self.store.require_profile(user)['role'] == 'hr':
            text += f'\nВыбрано респондентов: {invited}.\nЗавершение опроса: {round(count / invited * 100) if invited else 0}%.'
        if c['state'] == 'open' and not c['demo']:
            delivery = self.store.delivery_stats(user, token)
            text += f"\nПриглашений доставлено: {delivery['sent']}. Ожидают подключения коллег: {delivery['waiting']}."
            if delivery['retrying']:
                text += f"\nОжидают автоматической доставки: {delivery['retrying']}."
            if delivery['failed']:
                text += f"\nНе удалось доставить после повторных попыток: {delivery['failed']}. Обратитесь к организатору."
        rows = []
        if c['state'] == 'open' and count >= MIN_RESPONSES:
            rows.append([('Получить отчёт', 'report:' + token)])
            rows.append([('Ответы коллег', 'answers:' + token)])
        if c['state'] == 'draft' and (c['requester'] == user or self.store.profile(user)['role'] == 'hr'):
            rows.append([('Выбрать коллег', 'select:' + token), ('Отменить', 'discard:' + token)])
        rows.append([('Обновить статистику', 'status:' + token)])
        rows.append([('Главное меню', 'menu:')])
        self.send(user, text, inline(rows))

    def launch(self, user, token):
        self.store.activate(user, token)
        self.dispatch_invitations(token=token)
        self.send(user, 'Опрос открыт. Приглашения отправляет бот автоматически. '
                  'Неподключённые коллеги получат их после подключения профиля. '
                  'Вам ничего пересылать не нужно.')
        self.campaign_card(user, token)

    def dispatch_invitations(self, token=None, user=None):
        for invite in self.store.due_invitations(token=token, user=user):
            try:
                self.send(invite['telegram_id'], f"Приглашение оставить обратную связь о сотруднике {invite['subject']}.\n"
                  'Вас приглашают дать обратную связь. Участие добровольное.',
                          inline([[('Оставить отзыв', 'join:' + invite['campaign'])]]))
                self.store.mark_delivery(invite['campaign'], invite['employee'], 'sent')
            except UserError:
                self.store.mark_delivery(invite['campaign'], invite['employee'], 'failed')

    def consent(self, user, token):
        c = self.store.eligible(user, token)
        session = self.store.session(user)
        resume = session and session['campaign'] == token
        self.send(user, f"Отзыв о сотруднике: {c['subject']}\n\n"
                  'Ваши ответы будут переданы корпоративной Qwen. Сотрудник и HR получат обобщённый отчёт '
                  'без имени автора. Также сотрудник и HR смогут посмотреть исходные оценки и комментарии '
                  'после минимум трёх завершённых отзывов. Полная анонимность не гарантируется: '
                  'администратор компьютера имеет доступ к исходным ответам, а детали могут выдать автора. '
                  'Не указывайте имена других людей и конфиденциальные сведения.\n\n'
                  f"Согласны пройти опрос из {len(c['questions'])} вопросов? Можно пропускать вопросы без достаточного опыта.",
                  inline([[('Продолжить отзыв' if resume else 'Согласен, начать',
                            ('resume:' if resume else 'accept:') + token)],
                          [('Не участвовать', 'decline:' + token)], [('Напомнить позже', 'remind:' + token)]]))

    def ask_trial(self, user):
        session = self.trial.session(user)
        run = self.trial.run(user, session['run'])
        answers = json.loads(session['answers'])
        index = session['edit_index'] if session.get('edit_index') is not None else len(answers)
        if index == len(run['questions']):
            self.review_summary(user, session['run'], session['slot'])
            return
        prefix = ('Изменение ответа\nТекущий ответ: ' + answers[index] + '\n\n') if session.get('edit_index') is not None else ''
        self.send(user, f"ТЕСТ · отзыв участника №{session['slot']}\nО сотруднике: {run['subject']}\n\n"
                  + prefix + question_message(run['questions'], index, answers),
                  self.question_markup(run['questions'], index, session['run'], session['slot']))

    def review_summary(self, user, token, slot=None):
        mode = 't' if slot is not None else 'n'
        arg = f'{mode}:{token}:{slot or 0}:0'
        self.send(user, ('ТЕСТ · ' if slot is not None else '') + 'Анкета заполнена.\n'
                  'Можно просмотреть и изменить ответы. Отзыв попадёт в отчёт после нажатия «Отправить отзыв».',
                  inline([[('Просмотреть мои ответы', 'rvview:' + arg)],
                          [('Отправить отзыв', 'rvsubmit:' + arg)], [('Отменить ответ', 'rvcancel:' + arg)]]))

    def review_view(self, user, token, slot=None, index=0, listing=False, message_id=None):
        helper = AnswerReview(self.store, getattr(self, 'trial', None))
        _, run, answers = helper.context(user, token, slot)
        mode = 't' if slot is not None else 'n'
        def callback(action, value): return f'{action}:{mode}:{token}:{slot or 0}:{value}'
        index = max(0, min(index, len(answers) - 1))
        rows = []
        if listing:
            start = (index // 8) * 8
            text = 'Выберите ответ для просмотра или изменения:'
            rows = [[(f"№{i+1} · {run['questions'][i]['text'][:48]}", callback('rvview',i))]
                    for i in range(start, min(start+8,len(answers)))]
            previous, following = start-8, start+8
        else:
            q = run['questions'][index]
            text = f"Мои ответы · {index+1} из {len(answers)}\nРаздел: {q['section']}\n\n{q['text']}\n\nВаш ответ:\n{answers[index]}"
            rows = [[('Изменить этот ответ', callback('rvedit',index))], [('Список вопросов', callback('rvlist',index))]]
            previous, following = index-1, index+1
        navigation = []
        if previous >= 0: navigation.append(('← Назад', callback('rvlist' if listing else 'rvview',previous)))
        if following < len(answers): navigation.append(('Далее →', callback('rvlist' if listing else 'rvview',following)))
        if navigation:rows.append(navigation)
        rows.append([('Отправить отзыв', callback('rvsubmit',0))])
        if message_id is not None:self.api('editMessageText', chat_id=user, message_id=message_id, text=text, reply_markup=inline(rows))
        else:self.send(user,text,inline(rows))

    def feedback_submitted(self, user, token, slot=None):
        if slot is not None:
            count = len(self.trial.answers(token))
            self.send(user, f'Тестовый отзыв №{slot} отправлен. Завершено: {count} из 2.')
            if count == 2:
                self.send(user, 'Оба отзыва получены. Готовлю тестовый отчёт через корпоративный ИИ.')
                self.dispatch_trial_reports()
            else:
                other = 2 if slot == 1 else 1
                self.send(user, 'Теперь заполните отзыв за второго тестового участника.',
                          inline([[('Начать второй отзыв', f'trial:{token}:{other}')]]))
        else:
            self.send(user, 'Спасибо! Отзыв отправлен. Сотрудник и HR смогут посмотреть отчёт и исходные ответы без имени автора.')
        self.menu(user)

    def dispatch_trial_reports(self):
        for run in self.trial.ready():
            pending_key = 'trial:' + run['id']
            if pending_key in self.pending or len(self.pending) >= 2:
                continue
            self.pending.add(pending_key)
            self.trial.attempted(run['id'])
            def worker(run=run, pending_key=pending_key):
                try:
                    questions = json.loads(run['questions'])
                    answers = self.trial.answers(run['id'])
                    report = json.loads(run['report']) if run['report'] else generate_report(answers, self.settings, questions=questions)
                    self.trial.save_report(run['id'], report)
                    text = 'ТЕСТ · два отзыва заполнены одним человеком в разных ролях.\n\n' + format_report(
                        {'subject':run['subject'], 'demo':False}, report, 2, 'employee', 2)
                    self.reports.put((pending_key,run['owner'],text))
                except Exception as exc:
                    self.pending.discard(pending_key)
                    detail = str(exc) if isinstance(exc, UserError) else 'Не удалось обработать ответ корпоративного ИИ.'
                    if any(f'HTTP {code}.' in detail for code in (500,502,503,504)):
                        detail = 'Сервер корпоративного ИИ временно недоступен.'
                    print('Тестовый отчёт: ' + detail)
                    if self.trial.record_error(run['id'], detail):
                        try:
                            self.send(run['owner'], 'Оба тестовых отзыва сохранены.\n' + detail + '\n'
                                      'Отчёт остаётся в очереди. Бот повторит запрос автоматически и пришлёт результат, '
                                      'когда ИИ заработает. Заполнять отзывы заново не нужно.',
                                      inline([[('Повторить запрос сейчас', 'trialreport:' + run['id'])]]))
                        except UserError:
                            pass
            threading.Thread(target=worker, daemon=True).start()

    def request_report(self, user, token):
        c, answers = self.store.report_data(user, token)
        role = self.store.require_profile(user)['role']
        _, invited, _ = self.store.stats(user, token)
        report = self.store.cached_report(token, len(answers))
        if report:
            self.send(user, format_report(c, report, len(answers), role, invited))
            return
        if token in self.pending:
            raise UserError('Отчёт уже формируется. Повторите запрос через минуту.')
        if len(self.pending) >= 2:
            raise UserError('AI занят. Попробуйте через минуту.')
        self.send(user, 'Готовлю отчёт через корпоративную Qwen. Обычно это занимает несколько секунд.')
        self.pending.add(token)
        def worker():
            try:
                report = generate_report(answers, self.settings, questions=c['questions'])
                self.store.save_report(token, len(answers), report)
                text = format_report(c, report, len(answers), role, invited)
            except UserError as exc:
                text = str(exc)
            except Exception:
                text = 'Не удалось сформировать отчёт. Повторите запрос.'
            self.reports.put((token, user, text))
        threading.Thread(target=worker, daemon=True).start()

    def hr_panel(self, user):
        if self.store.require_profile(user)['role'] != 'hr':
            raise UserError('Панель доступна только профилю HR.')
        campaigns = self.store.visible(user)
        self.send(user, 'Панель HR: выберите сотрудника, чтобы запросить обратную связь.',
                  inline([[(e['name'], 'new:' + e['id'])]
                          for e in self.store.employees() if e['role'] == 'employee']))
        if campaigns:
            self.send(user, 'Последние опросы:', inline(
                [[(('ДЕМО · ' if c['demo'] else '') + c['subject'], 'status:' + c['id'])] for c in campaigns]))

    def action(self, user, action, arg='', message_id=None):
        if user in getattr(self, 'match_pending', set()):
            raise UserError('Подбор коллег ещё идёт. Дождитесь списка.')
        self.store.require_profile(user)
        if action in ('pick', 'pickall', 'select', 'add', 'toggle', 'all', 'none', 'discard', 'launch'):
            self.store.metadata('colleague-search:' + str(user), '')
            self.store.metadata('colleague-search-message:' + str(user), '')
        if action in ('rvview','rvlist','rvedit','rvsubmit','rvcancel'):
            parts = arg.split(':')
            if len(parts) != 4 or parts[0] not in ('t','n') or not parts[2].isdigit() or not parts[3].isdigit():
                raise UserError('Неизвестный ответ.')
            mode, token, slot_value, index_value = parts
            slot = int(slot_value) if mode == 't' else None
            index = int(index_value)
            helper = AnswerReview(self.store, getattr(self, 'trial', None))
            helper.context(user, token, slot)
            if action == 'rvsubmit':
                helper.submit(user, token, slot)
                self.feedback_submitted(user, token, slot)
            elif action == 'rvcancel':
                (self.trial if slot is not None else self.store).cancel(user)
                self.send(user, 'Черновик отзыва отменён и не попадёт в отчёт.')
                self.menu(user)
            elif action == 'rvedit':
                helper.edit(user, token, index, slot)
                if slot is not None:self.ask_trial(user)
                else:self.ask_question(user,token,index)
            else:
                table = 'trial_sessions' if slot is not None else 'sessions'
                with self.store.connect() as db:db.execute(f'UPDATE {table} SET edit_index=NULL WHERE user=?',(user,))
                self.review_view(user,token,slot,index,action=='rvlist',message_id)
        elif action in ('skipanswer', 'cancelanswer', 'skiptrial', 'canceltrial'):
            parts = arg.split(':')
            is_trial = action.endswith('trial')
            session = self.trial.session(user) if is_trial else self.store.session(user)
            expected = 3 if is_trial else 2
            if len(parts) != expected or not parts[-1].isdigit() or not session:
                raise UserError('Этот вопрос уже пройден. Продолжите текущий отзыв.')
            token, index = parts[0], int(parts[-1])
            questions = (self.trial.run(user, token) if is_trial else self.store.campaign(token))['questions']
            active_token = session['run'] if is_trial else session['campaign']
            if (active_token != token or index != (session['edit_index'] if session.get('edit_index') is not None else len(json.loads(session['answers'])))
                    or index >= len(questions) or questions[index].get('type') == 'rating'
                    or (is_trial and parts[1] != str(session['slot']))):
                raise UserError('Этот вопрос уже пройден. Продолжите текущий отзыв.')
            self.handle({'chat': {'type': 'private'}, 'from': {'id': user},
                         'text': SKIP if action.startswith('skip') else '/cancel'})
        elif action == 'trial':
            token, _, slot = arg.partition(':')
            if not slot.isdigit():
                raise UserError('Неизвестный участник теста.')
            self.trial.begin(user, token, int(slot))
            self.ask_trial(user)
        elif action == 'trialreport':
            self.trial.retry(user, arg)
            self.dispatch_trial_reports()
        elif getattr(self, 'trial', None) and self.trial.session(user):
            raise UserError('Сначала завершите тестовый отзыв или нажмите «Отменить ответ».')
        elif action == 'menu':
            self.menu(user)
        elif action == 'new':
            self.draft(user, arg or None)
        elif action == 'select':
            token, _, page = arg.partition(':')
            self.selection_card(user, token, message_id, int(page) if page.isdigit() else 0)
        elif action == 'pick':
            token, _, page = arg.partition(':')
            self.colleague_picker(user, token, int(page) if page.isdigit() else 0,
                                  query=None if page.isdigit() else '', message_id=message_id)
        elif action == 'pickall':
            self.colleague_picker(user, arg, query='', message_id=message_id)
        elif action == 'find':
            self.store.additional_colleagues(user, arg)
            self.store.metadata('colleague-search:' + str(user), arg)
            self.store.metadata('colleague-search-message:' + str(user), str(message_id) if message_id is not None else '')
            text = 'Напишите имя, фамилию или их часть. Для отмены поиска отправьте /start.'
            if message_id is not None:
                self.api('editMessageText', chat_id=user, message_id=message_id, text=text,
                         reply_markup=inline([[('Вернуться к каталогу', 'pickall:' + arg)]]))
            else:
                self.send(user, text)
        elif action == 'add':
            token, _, rest = arg.partition(':')
            employee, _, page = rest.partition(':')
            selected = {i['employee'] for i in self.store.invitations(token)}
            if employee in selected:
                self.store.toggle_colleague(user, token, employee)
            else:
                self.store.add_colleague(user, token, employee)
            self.colleague_picker(user, token, int(page) if page.isdigit() else 0, message_id=message_id)
        elif action == 'toggle':
            token, _, rest = arg.partition(':')
            employee, _, page = rest.partition(':')
            self.store.toggle_colleague(user, token, employee)
            self.selection_card(user, token, message_id, int(page) if page.isdigit() else 0)
        elif action in ('all', 'none'):
            token, _, page = arg.partition(':')
            self.store.set_all_colleagues(user, token, action == 'all')
            self.selection_card(user, token, message_id, int(page) if page.isdigit() else 0)
        elif action == 'launch':
            self.launch(user, arg)
        elif action == 'discard':
            self.store.discard(user, arg)
            self.send(user, 'Опрос отменён. Приглашения не отправлялись.')
        elif action == 'join':
            self.consent(user, arg)
        elif action == 'accept':
            self.store.begin(user, arg)
            self.ask_question(user, arg, 0)
        elif action == 'decline':
            if arg:
                self.store.eligible(user, arg)
                self.store.cancel_reminder(user, arg)
                session = self.store.session(user)
                if session and session['campaign'] == arg:
                    self.store.cancel(user)
            self.send(user, 'Вы отказались от участия в этом опросе.')
            self.menu(user)
        elif action == 'remind':
            self.reminder_picker(user, arg)
        elif action == 'snooze':
            token, _, delay = arg.partition(':')
            if not delay.isdigit():
                raise UserError('Выберите срок кнопкой.')
            self.store.schedule_reminder(user, token, int(delay))
            label = REMINDER_LABELS[REMINDER_DELAYS.index(int(delay))]
            self.send(user, label + ' напомню об отзыве. Можно поменять время.',
                      inline([[('Изменить время', 'remind:' + token)]]))
        elif action == 'resume':
            self.store.eligible(user, arg)
            session = self.store.session(user)
            if not session or session['campaign'] != arg:
                raise UserError('Незавершённый отзыв не найден. Откройте приглашение заново.')
            self.store.cancel_reminder(user, arg)
            self.ask_question(user, arg, len(json.loads(session['answers'])))
        elif action in ('answers', 'comments'):
            token, _, index = arg.partition(':')
            self.show_answers(user, token, int(index) if index.isdigit() else 0, action == 'comments')
        elif action == 'report':
            self.request_report(user, arg)
        elif action == 'status':
            self.campaign_card(user, arg)
        else:
            raise UserError('Неизвестное действие. Откройте /start.')

    def handle(self, message):
        if message.get('chat', {}).get('type') != 'private' or 'text' not in message:
            return
        user, text = message['from']['id'], message['text'].strip()
        if user in getattr(self, 'match_pending', set()):
            self.send(user, 'Подбор коллег ещё идёт. Дождитесь списка.', {'remove_keyboard': True})
            return
        command, _, arg = text.partition(' ')
        command = command.split('@')[0].lower()
        try:
            p = self.store.profile(user)
            if not p:
                if command in ('/start', '/help'):
                    if arg:
                        self.store.metadata('invite:' + str(user), arg)
                    self.send(user, ('HR AI Assistant · корпоративная команда.\n' if getattr(self, 'matcher', None)
                                     else 'HR AI Assistant · демонстрационная команда.\n')
                              + 'Чтобы подключить профиль, отправьте одноразовый код организатора. '
                              'Коды видны в окне запуска бота. Роль HR также выдаётся кодом.',
                              {'remove_keyboard': True})
                    return
                attempts, until = self.enrollment_attempts.get(user, (0, 0))
                if attempts >= 5 and time.time() < until:
                    raise UserError('Слишком много неверных кодов. Попробуйте через 10 минут.')
                try:
                    self.store.enroll(user, text)
                except UserError:
                    self.enrollment_attempts[user] = (attempts + 1 if time.time() < until else 1,
                                                      until if time.time() < until else time.time() + 600)
                    raise
                self.enrollment_attempts.pop(user, None)
                self.menu(user)
                self.dispatch_invitations(user=user)
                invite = self.store.metadata('invite:' + str(user))
                if invite:
                    self.store.metadata('invite:' + str(user), '')
                    self.consent(user, invite)
                return
            if getattr(self, 'trial', None) and self.trial.session(user):
                if text == 'Отменить ответ' or command == '/cancel':
                    self.trial.cancel(user)
                    self.send(user, 'Незавершённый тестовый ответ отменён.')
                    self.menu(user)
                elif command in ('/start', '/help'):
                    self.ask_trial(user)
                else:
                    result = self.trial.answer(user, text, message.get('message_id'), review=True)
                    if result:
                        token, slot, step = result
                        run = self.trial.run(user, token)
                        self.ask_trial(user)
                return
            search = self.store.metadata('colleague-search:' + str(user))
            if search:
                self.store.metadata('colleague-search:' + str(user), '')
                search_message = self.store.metadata('colleague-search-message:' + str(user)) or ''
                self.store.metadata('colleague-search-message:' + str(user), '')
                if command in ('/start', '/cancel'):
                    self.menu(user)
                    return
                if not text.startswith('/'):
                    if not 1 <= len(text) <= 100:
                        raise UserError('Поисковый запрос должен содержать от 1 до 100 символов.')
                    self.colleague_picker(user, search, query=text,
                                          message_id=int(search_message) if search_message.isdigit() else None)
                    return
            if command == '/start' and arg:
                self.consent(user, arg)
            elif command in ('/start', '/help'):
                self.menu(user)
            elif text == 'Получить обратную связь' or command == '/feedback':
                if p['role'] == 'hr':
                    self.hr_panel(user)
                else:
                    self.draft(user)
            elif text == 'Оставить отзыв':
                campaigns = self.store.available(user)
                if not campaigns:
                    self.send(user, 'Пока нет доступных приглашений.')
                else:
                    self.send(user, 'Приглашения оставить отзыв. Выберите сотрудника и опрос:', inline(
                        [[(self.campaign_label(c, include_subject=True), 'join:' + c['id'])] for c in campaigns]))
            elif text == 'Мои отчёты' or command == '/my':
                campaigns = self.store.visible(user)
                if not campaigns:
                    self.send(user, 'Опросов пока нет. Нажмите «Получить обратную связь».'
                              + (' Для готового примера есть «Демо».' if not getattr(self, 'matcher', None) else ''))
                else:
                    self.send(user, 'Ваши запросы обратной связи. Выберите опрос по дате и статусу:', inline(
                        [[(self.campaign_label(c, include_subject=p['role'] == 'hr'), 'status:' + c['id'])] for c in campaigns]))
            elif text == 'Панель HR' or command == '/hr':
                self.hr_panel(user)
            elif text == 'Демо' or command == '/demo':
                if getattr(self, 'matcher', None):
                    raise UserError('В рабочем режиме эта команда недоступна. Откройте меню через /start.')
                token = self.store.demo(user, DEMO_ANSWERS)
                self.send(user, 'Создан отдельный демонстрационный опрос. Все отзывы вымышленные; приглашения не отправлены.')
                self.campaign_card(user, token)
            elif text == 'Отменить ответ' or command == '/cancel':
                if self.store.session(user):
                    self.store.cancel(user)
                    self.send(user, 'Незавершённый ответ отменён и не войдёт в отчёт.')
                else:
                    self.send(user, 'У вас нет незавершённого отзыва.')
                self.menu(user)
            elif command in ('/report', '/status'):
                self.action(user, command[1:], arg)
            elif text.startswith('/'):
                self.send(user, 'Откройте меню командой /start.')
            else:
                session = self.store.session(user)
                step = self.store.answer(user, text, message.get('message_id'), review=True)
                if step is None:
                    self.send(user, 'Выберите действие кнопкой меню или отправьте /start.')
                elif session and step == len(self.store.campaign(session['campaign'])['questions']):
                    self.review_summary(user, session['campaign'])
                else:
                    if session:
                        self.ask_question(user, session['campaign'], step)
        except UserError as exc:
            self.send(user, str(exc))

    def handle_callback(self, callback):
        private = callback.get('message', {}).get('chat', {}).get('type') == 'private'
        user = callback['from']['id']
        busy = private and user in getattr(self, 'match_pending', set())
        text = 'Подбор коллег ещё идёт. Дождитесь списка.' if busy else None
        acknowledgements = getattr(self, 'callback_acknowledgements', None)
        if acknowledgements:
            acknowledgements.submit(self.acknowledge_callback, callback['id'], text)
        else:
            self.acknowledge_callback(callback['id'], text)
        if not private or busy:
            return
        action, _, arg = callback.get('data', '').partition(':')
        try:
            self.action(user, action, arg, callback['message'].get('message_id'))
        except UserError as exc:
            self.send(user, str(exc))

    def acknowledge_callback(self, callback_id, text=None):
        try:
            payload = {'callback_query_id': callback_id}
            if text:
                payload['text'] = text
            self.api('answerCallbackQuery', **payload)
        except UserError:
            # Expired acknowledgements must not delay the actual button action.
            pass

    def run(self):
        print(f'Бот: https://t.me/{self.username}\nОткройте его и отправьте /start. Остановка: Ctrl+C.')
        offset_key = 'offset:' + str(self.bot_id)
        offset = int(self.store.metadata(offset_key) or 0)
        while True:
            try:
                if time.monotonic() - self.last_dispatch >= 30:
                    self.dispatch_invitations()
                    self.dispatch_reminders()
                    self.dispatch_trial_reports()
                    self.last_dispatch = time.monotonic()
                while not self.reports.empty():
                    token, user, text = self.reports.get()
                    self.pending.discard(token)
                    self.send(user, text)
                    if token.startswith('trial:'):
                        self.trial.delivered(token[6:])
                while not self.match_results.empty():
                    user, token, error = self.match_results.get()
                    self.match_pending.discard(user)
                    if error:
                        self.send(user, error)
                        self.menu(user)
                    else:
                        self.menu(user)
                        self.selection_card(user, token)
                updates = self.api('getUpdates', offset=offset, timeout=5,
                                   allowed_updates=['message', 'callback_query'])
                for update in updates:
                    if 'callback_query' in update:
                        self.handle_callback(update['callback_query'])
                    else:
                        self.handle(update.get('message', {}))
                    offset = update['update_id'] + 1
                    self.store.metadata(offset_key, offset)
            except UserError as exc:
                print(str(exc))
                time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description='HR AI Assistant')
    parser.add_argument('--check-ai', action='store_true')
    parser.add_argument('--evaluate-ai', action='store_true')
    parser.add_argument('--check-team', action='store_true')
    parser.add_argument('--demo-team', action='store_true')
    parser.add_argument('--import-team', action='store_true')
    args = parser.parse_args()
    try:
        load_local_env(ROOT / '.env')
        if args.import_team:
            directory = AtlassianMatcher.import_from_env(ROOT)
            unmatched = sum(e['role'] == 'employee' and not e.get('confluence_user') for e in directory['employees'])
            print(f"Каталог сохранён. Профилей: {len(directory['employees'])}; проектов: {len(directory['projects'])}.")
            print(f"Без подтверждённого логина Confluence: {unmatched}; пространств без привязки: {len(directory['unmapped_spaces'])}.")
            if directory.get('roster_source') == 'trck':
                missing = sum(e['role'] == 'employee' and not e.get('jira_user') for e in directory['employees'])
                print(f'Источник сотрудников: TRCK. Без однозначного соответствия Jira: {missing}.')
            else:
                print('Список сотрудников вводить не нужно. Проверьте служебные аккаунты и неподтверждённые связи в каталоге.')
            return
        if args.check_team:
            matcher = AtlassianMatcher.from_env(ROOT)
            snapshot = matcher.collect()
            print(f"Подключение проверено. Проектов: {len(snapshot['projects'])}; записей участия: {len(snapshot['memberships'])}.")
            return
        settings = ai_settings()
        if args.check_ai:
            report = generate_report(DEMO_ANSWERS, settings)
            c = {'subject': 'Тестовый сотрудник', 'demo': True}
            print(format_report(c, report, 3, 'employee', 3))
            print('\n' + format_report(c, report, 3, 'hr', 3))
            return
        if args.evaluate_ai:
            from evaluation import evaluate
            evaluate(settings)
            return
        source = 'demo' if args.demo_team else os.getenv('TEAM_SOURCE', 'atlassian')
        if source not in ('demo', 'atlassian'):
            raise UserError('TEAM_SOURCE должен быть atlassian или demo.')
        matcher = AtlassianMatcher.from_env(ROOT) if source == 'atlassian' else None
        store = Store(ROOT / 'data' / ('corporate.sqlite3' if matcher else 'feedback.sqlite3'),
                      None if matcher else FIXTURE)
        if matcher:
            store.sync_directory(matcher.directory)
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        remember_token = not token
        if not token:
            token = getpass.getpass('Токен BotFather (ввод скрыт; будет сохранён локально): ')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token.strip()):
            raise UserError('Неверный формат токена BotFather.')
        bot = Bot(token.strip(), store, settings, matcher)
        if remember_token:
            save_bot_token(ROOT / '.env', token.strip())
            print('Токен сохранён в локальном .env. При следующих запусках вводить его не нужно.')
        print(('\nКорпоративный каталог.\n' if matcher else '\nДЕМО · вымышленная команда.\n')
              + 'Одноразовые коды профилей. Отправьте свой код боту в Telegram.\n'
              'Остальные коды передайте участникам; код HR даёт доступ к панели HR.\n')
        for name, role, code in store.enrollment_codes():
            print(f'{name} [{role}]: {code}')
        print('\nКоды неподключённых профилей меняются при каждом запуске. Подключённые профили сохраняются.\n')
        bot.run()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except UserError as exc:
        print(str(exc))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
