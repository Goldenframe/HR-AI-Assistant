"""Telegram entry point for HR AI Assistant. No third-party dependencies."""
import argparse
import getpass
import json
import os
from pathlib import Path
import queue
import re
import threading
import time

from ai import DEMO_ANSWERS, generate_report, format_report
from helpers import UserError, request_json, ai_settings, load_local_env, save_bot_token
from store import Store, MIN_RESPONSES
from survey import SKIP, question_message

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'fixtures' / 'team.json'


def inline(rows):
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row]
                                for row in rows]}


class Bot:
    def __init__(self, token, store, settings):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.store, self.settings = store, settings
        self.reports, self.pending = queue.Queue(), set()
        self.enrollment_attempts = {}
        self.last_dispatch = 0
        me = self.api('getMe')
        self.username, self.bot_id = me['username'], me['id']
        if self.api('getWebhookInfo').get('url'):
            raise UserError('У бота настроен webhook. Для этого прототипа используйте отдельного бота.')

    def api(self, method, **payload):
        response = request_json(self.base + method, payload, timeout=40)
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
        p = self.store.require_profile(user)
        rows = [['Получить обратную связь', 'Оставить отзыв'], ['Мои отчёты', 'Демо'], ['Отменить ответ']]
        if p['role'] == 'hr':
            rows.insert(0, ['Панель HR'])
        s = self.store.session(user)
        text = f"Ваш профиль: {p['name']}\nРоль: {'HR' if p['role'] == 'hr' else 'сотрудник'}.\nВыберите действие."
        if s:
            questions = self.store.campaign(s['campaign'])['questions']
            text += '\n\nУ вас есть незавершённый опрос.\n' + question_message(questions, len(json.loads(s['answers'])))
            rows.insert(0, [SKIP])
        self.send(user, text, {'keyboard': [[{'text': v} for v in row] for row in rows],
                              'resize_keyboard': True})

    def draft(self, user, target=None):
        token, _ = self.store.create(user, target)
        self.selection_card(user, token)

    def selection_card(self, user, token, message_id=None):
        self.store.controlled(user, token)
        c = self.store.campaign(token)
        if c['state'] != 'draft':
            raise UserError('Список этого опроса уже подтверждён или отменён.')
        selected = {i['employee'] for i in self.store.invitations(token)}
        text = (f"Обратная связь о сотруднике: {c['subject']}\n\n"
                f'Выберите коллег, от которых хотите получить отзыв. Выбрано: {len(selected)}.\n'
                f'Для запуска нужно минимум {MIN_RESPONSES}. Повторное нажатие снимает выбор.\n\n'
                'После подтверждения бот сам отправит приглашения.')
        rows = [[(('✓ ' if e['id'] in selected else '○ ') + e['name'],
                  'toggle:' + token + ':' + e['id'])] for e in self.store.candidates(c['target'])]
        rows += [[('Запустить опрос', 'launch:' + token)], [('Отменить', 'discard:' + token)]]
        if message_id is not None:
            self.api('editMessageText', chat_id=user, message_id=message_id, text=text, reply_markup=inline(rows))
        else:
            self.send(user, text, inline(rows))

    def ask_question(self, user, token, index):
        questions = self.store.campaign(token)['questions']
        self.send(user, question_message(questions, index),
                  {'keyboard': [[{'text': SKIP}], [{'text': 'Отменить ответ'}]], 'resize_keyboard': True})

    def campaign_card(self, user, token):
        c, invited, count = self.store.stats(user, token)
        state = {'draft': 'ожидает подтверждения', 'open': 'сбор открыт', 'cancelled': 'отменён'}[c['state']]
        text = f"{c['subject']}\nСтатус: {state}.\nПолучено отзывов: {count}. Минимум для отчёта: {MIN_RESPONSES}."
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
        if c['state'] == 'open':
            rows.append([('Получить отчёт', 'report:' + token)])
        if c['state'] == 'draft' and (c['requester'] == user or self.store.profile(user)['role'] == 'hr'):
            rows.append([('Выбрать коллег', 'select:' + token), ('Отменить', 'discard:' + token)])
        rows.append([('Обновить статистику', 'status:' + token)])
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
                          'Сотрудник или HR выбрал вас для обратной связи. Участие добровольное.',
                          inline([[('Подробнее и оставить отзыв', 'join:' + invite['campaign'])]]))
                self.store.mark_delivery(invite['campaign'], invite['employee'], 'sent')
            except UserError:
                self.store.mark_delivery(invite['campaign'], invite['employee'], 'failed')

    def consent(self, user, token):
        c = self.store.eligible(user, token)
        self.send(user, f"Отзыв о сотруднике: {c['subject']}\n\n"
                  'Ваши ответы будут переданы корпоративной Qwen. Сотрудник и HR получат обобщённый отчёт '
                  'без имени автора и исходных цитат. Полная анонимность не гарантируется: '
                  'администратор компьютера имеет доступ к исходным ответам, а детали могут выдать автора. '
                  'Не указывайте имена других людей и конфиденциальные сведения.\n\n'
                  f"Согласны пройти опрос из {len(c['questions'])} вопросов? Можно пропускать вопросы без достаточного опыта.",
                  inline([[('Согласен, начать', 'accept:' + token)], [('Не участвовать', 'decline')]]))

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
        self.store.require_profile(user)
        if action == 'new':
            self.draft(user, arg or None)
        elif action == 'select':
            self.selection_card(user, arg, message_id)
        elif action == 'toggle':
            token, _, employee = arg.partition(':')
            self.store.toggle_colleague(user, token, employee)
            self.selection_card(user, token, message_id)
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
            self.send(user, 'Вы отказались от участия. Ответы не собирались.')
            self.menu(user)
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
        command, _, arg = text.partition(' ')
        command = command.split('@')[0].lower()
        try:
            p = self.store.profile(user)
            if not p:
                if command in ('/start', '/help'):
                    if arg:
                        self.store.metadata('invite:' + str(user), arg)
                    self.send(user, 'HR AI Assistant · демонстрационная команда.\n'
                              'Чтобы подключить профиль, отправьте одноразовый код организатора. '
                              'Коды видны в окне запуска бота. Роль HR также выдаётся кодом.')
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
                    self.send(user, 'Выберите опрос:', inline(
                        [[(c['subject'], 'join:' + c['id'])] for c in campaigns]))
            elif text == 'Мои отчёты' or command == '/my':
                campaigns = self.store.visible(user)
                if not campaigns:
                    self.send(user, 'Опросов пока нет. Нажмите «Получить обратную связь» или «Демо».')
                else:
                    self.send(user, 'Выберите опрос:', inline(
                        [[(('ДЕМО · ' if c['demo'] else '') + c['subject'], 'status:' + c['id'])] for c in campaigns]))
            elif text == 'Панель HR' or command == '/hr':
                self.hr_panel(user)
            elif text == 'Демо' or command == '/demo':
                token = self.store.demo(user, DEMO_ANSWERS)
                self.send(user, 'Создан отдельный демонстрационный опрос. Все отзывы вымышленные; приглашения не отправлены.')
                self.campaign_card(user, token)
            elif text == 'Отменить ответ' or command == '/cancel':
                self.store.cancel(user)
                self.send(user, 'Незавершённый ответ отменён и не войдёт в отчёт.')
                self.menu(user)
            elif command in ('/report', '/status'):
                self.action(user, command[1:], arg)
            elif text.startswith('/'):
                self.send(user, 'Откройте меню командой /start.')
            else:
                session = self.store.session(user)
                step = self.store.answer(user, text, message.get('message_id'))
                if step is None:
                    self.send(user, 'Выберите действие кнопкой меню или отправьте /start.')
                elif session and step == len(self.store.campaign(session['campaign'])['questions']):
                    self.send(user, 'Спасибо! Отзыв сохранён. Он войдёт в обобщённый отчёт без вашего имени.')
                    self.menu(user)
                else:
                    if session:
                        self.ask_question(user, session['campaign'], step)
        except UserError as exc:
            self.send(user, str(exc))

    def handle_callback(self, callback):
        try:
            self.api('answerCallbackQuery', callback_query_id=callback['id'])
        except UserError:
            # A callback can expire while the bot is restarting; do not block later updates.
            pass
        if callback.get('message', {}).get('chat', {}).get('type') != 'private':
            return
        user = callback['from']['id']
        action, _, arg = callback.get('data', '').partition(':')
        try:
            self.action(user, action, arg, callback['message'].get('message_id'))
        except UserError as exc:
            self.send(user, str(exc))

    def run(self):
        print(f'Бот: https://t.me/{self.username}\nОткройте его и отправьте /start. Остановка: Ctrl+C.')
        offset_key = 'offset:' + str(self.bot_id)
        offset = int(self.store.metadata(offset_key) or 0)
        while True:
            try:
                if time.monotonic() - self.last_dispatch >= 30:
                    self.dispatch_invitations()
                    self.last_dispatch = time.monotonic()
                while not self.reports.empty():
                    token, user, text = self.reports.get()
                    self.pending.discard(token)
                    self.send(user, text)
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
    args = parser.parse_args()
    try:
        load_local_env(ROOT / '.env')
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
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        remember_token = not token
        if not token:
            token = getpass.getpass('Токен BotFather (ввод скрыт; будет сохранён локально): ')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token.strip()):
            raise UserError('Неверный формат токена BotFather.')
        store = Store(ROOT / 'data' / 'feedback.sqlite3', FIXTURE)
        bot = Bot(token.strip(), store, settings)
        if remember_token:
            save_bot_token(ROOT / '.env', token.strip())
            print('Токен сохранён в локальном .env. При следующих запусках вводить его не нужно.')
        print('\nОдноразовые коды для вымышленных профилей. Отправьте свой код боту в Telegram.\n'
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
