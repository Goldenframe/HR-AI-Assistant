import unittest
from io import BytesIO
from zipfile import ZipFile
import xml.etree.ElementTree as E
from xlsx_export import export_answers
from survey import QUESTIONNAIRE,RATINGS,SKIP
from helpers import UserError
from telegram_client import TelegramClient
from test_telegram_client import Connection

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
class ExcelExportTests(unittest.TestCase):
    def test_all_questions_and_original_answers_are_preserved_and_header_is_frozen(self):
        answers=[[RATINGS[0] if q.get('type')=='rating' else SKIP for q in QUESTIONNAIRE] for _ in range(2)]
        answers[0][1]='=HYPERLINK("https://example.org", "Пример") & текст < >'
        result=export_answers('Сотрудник',QUESTIONNAIRE,answers)
        with ZipFile(BytesIO(result)) as z:
            root=E.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows=root.findall('s:sheetData/s:row',NS)
        self.assertEqual(len(rows),3);self.assertEqual(len(rows[0]),52)
        self.assertEqual(root.find('s:dimension',NS).get('ref'),'A1:AZ3')
        for number,answers_row in enumerate(answers,1):
            for index,text in enumerate(answers_row,2):
                self.assertEqual(rows[number][index].find('s:is/s:t',NS).text,text)
                self.assertIsNone(rows[number][index].find('s:f',NS))
        pane=root.find('s:sheetViews/s:sheetView/s:pane',NS)
        self.assertEqual((pane.get('xSplit'),pane.get('ySplit')),('2','1'))
        self.assertEqual(rows[1][1].find('s:v',NS).text,'1')
    def test_mismatched_questionnaire_is_not_exported(self):
        with self.assertRaises(UserError):export_answers('Тест',QUESTIONNAIRE,[['short']])
    def test_document_upload_is_multipart_and_does_not_retry_failed_send(self):
        connection=Connection('api.telegram.org',30)
        client=TelegramClient('123:fake',use_proxy=False,connection_factory=lambda *a,**k:connection)
        client.send_document(123,b'PK-test','answers.xlsx','Исходные ответы')
        body=connection.calls[0][1]['body']
        self.assertIn(b'PK-test',body);self.assertIn(b'filename="answers.xlsx"',body)
        self.assertIn(b'chat_id',body);self.assertEqual(len(connection.calls),1)
        connection.failure=True
        with self.assertRaises(UserError):client.send_document(123,b'PK-test','answers.xlsx')
        self.assertEqual(len(connection.calls),2)
