"""Thread-local persistent Telegram HTTPS connections, with an optional system proxy."""
import http.client
import json
import os
import threading
import secrets
import urllib.request

from helpers import UserError, request_json


class TelegramClient:
    def __init__(self, token, use_proxy=None, connection_factory=http.client.HTTPSConnection):
        self.token = token
        self.use_proxy = os.getenv('TELEGRAM_USE_PROXY', '0') == '1' if use_proxy is None else use_proxy
        self.connection_factory = connection_factory
        self.local = threading.local()

    def close(self):
        connection = getattr(self.local, 'connection', None)
        if connection:
            connection.close()
        self.local.connection = None

    def request(self, method, payload):
        timeout = max(10, int(payload.get('timeout', 0)) + 10) if method == 'getUpdates' else 10
        if method == 'answerCallbackQuery':
            timeout = 3
        if self.use_proxy:
            return request_json('https://api.telegram.org/bot' + self.token + '/' + method,
                                payload, timeout=timeout)
        connection = getattr(self.local, 'connection', None)
        if connection is None:
            connection = self.connection_factory('api.telegram.org', timeout=timeout)
            self.local.connection = connection
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        try:
            connection.request('POST', '/bot' + self.token + '/' + method,
                               body=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read()
            if response.status >= 400:
                self.close()
                # Re-rendering an unchanged keyboard is harmless.
                if method.startswith('editMessage') and response.status == 400:
                    try:
                        error = json.loads(data)
                        if 'message is not modified' in error.get('description', '').casefold():
                            return {'ok': True, 'result': True}
                    except (ValueError, AttributeError):
                        pass
                raise UserError(f'Telegram вернул HTTP {response.status}. Проверьте доступ к боту.')
            return json.loads(data)
        except UserError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
            self.close()
            # Never retry a send here: an ambiguous network failure can follow successful delivery.
            raise UserError('Не удалось связаться с Telegram. Проверьте интернет; при необходимости включите TELEGRAM_USE_PROXY=1.') from None

    def send_document(self, chat, content, filename, caption=''):
        boundary = 'feedback-' + secrets.token_hex(16)
        chunks = []
        for name,value in (('chat_id',str(chat)),('caption',caption)):
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode('utf-8'))
        safe_name = filename.replace('"','').replace('\r','').replace('\n','')
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'
                       'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode())
        chunks.extend([content, f'\r\n--{boundary}--\r\n'.encode()])
        body = b''.join(chunks)
        headers = {'Content-Type':f'multipart/form-data; boundary={boundary}'}
        connection = None
        try:
            if self.use_proxy:
                request = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/sendDocument',data=body,headers=headers)
                with urllib.request.urlopen(request,timeout=30) as response:data=json.load(response)
            else:
                connection=self.connection_factory('api.telegram.org',timeout=30)
                connection.request('POST','/bot'+self.token+'/sendDocument',body=body,headers=headers)
                response=connection.getresponse()
                data=json.loads(response.read())
                if response.status>=400:raise UserError(f'Telegram вернул HTTP {response.status} при отправке файла.')
            if not data.get('ok'):raise UserError('Telegram не принял Excel-файл.')
            return data['result']
        except UserError:raise
        except (OSError,http.client.HTTPException,ValueError,UnicodeDecodeError):
            raise UserError('Не удалось отправить Excel через Telegram. Попробуйте кнопку ещё раз.') from None
        finally:
            if connection:connection.close()
