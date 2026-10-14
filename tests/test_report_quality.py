import unittest
from ai import REPORT_VERSION,validate_report,format_report,filter_supported_items
from helpers import UserError
from survey import QUESTIONNAIRE,RATINGS,SKIP

class ReportQualityTests(unittest.TestCase):
    def rows(self):return [[SKIP]*len(QUESTIONNAIRE) for _ in range(2)]
    def empty(self):return {'strengths':[],'development':[],'advice':[],'contradictions':[]}
    def test_different_questions_and_different_degrees_of_agreement_are_not_conflicts(self):
        rows=self.rows();rows[0][0]=RATINGS[0];rows[1][0]=RATINGS[1];rows[1][2]=RATINGS[2]
        report=self.empty();item={'theme':'Разные мнения','evidence':[{'response':1,'question':1,'quote':RATINGS[0]},{'response':2,'question':3,'quote':RATINGS[2]}]}
        report['contradictions']=[item]
        self.assertEqual(validate_report(report,rows)['contradictions'],[])
        item['evidence'][1]={'response':2,'question':1,'quote':RATINGS[1]}
        self.assertEqual(validate_report(report,rows)['contradictions'],[])
        rows[1][0]=RATINGS[2];item['evidence'][1]['quote']=RATINGS[2]
        self.assertEqual(len(validate_report(report,rows)['contradictions']),1)
    def test_positive_rating_cannot_prove_negative_observation(self):
        rows=self.rows();rows[0][0]=RATINGS[0];report=self.empty()
        report['development']=[{'theme':'Не хочет работать вместе','evidence':[{'response':1,'question':1,'quote':RATINGS[0]}]}]
        with self.assertRaises(UserError):validate_report(report,rows)
    def test_explanations_and_actions_are_readable_without_raw_statistical_tables(self):
        rows=self.rows();rows[0][2]=RATINGS[2];report=self.empty();report['version']=REPORT_VERSION
        report['development']=[{'theme':'Идеи не всегда понятны коллегам','explanation':'Коллеге бывает сложно понять предложенное решение; это может затруднять обсуждение.',
                               'action':'Перед встречей запишите решение и два аргумента.', 'check':'На двух встречах спросите, какие вопросы остались.',
                               'evidence':[{'response':1,'question':3,'quote':RATINGS[2]}]}]
        valid=validate_report(report,rows);text=format_report({'subject':'Тест','demo':False},valid,2)
        self.assertIn(report['development'][0]['action'],text);self.assertIn('Как проверить:',text)
        self.assertIn('Варианты развития',text)
        self.assertIn('с учётом нагрузки, без обязательных сроков',text)
        self.assertNotIn('за две недели',text)
        for unwanted in ('[','Полнота обратной связи','Оценки по разделам','упоминаний в отзывах'):self.assertNotIn(unwanted,text)
        del report['development'][0]['action']
        with self.assertRaises(UserError):validate_report(report,rows)

    def test_inconsistent_rating_and_comment_are_distinguished_from_disagreement_between_people(self):
        rows=self.rows();rows[0][0]=RATINGS[0];rows[0][1]='Не хочу продолжать совместную работу.'
        report=self.empty();report['contradictions']=[{'theme':'Оценка и комментарий не согласованы','kind':'inconsistent_answer',
          'evidence':[{'response':1,'question':1,'quote':RATINGS[0]},{'response':1,'question':2,'quote':rows[0][1]}]}]
        self.assertEqual(len(validate_report(report,rows)['contradictions']),1)
        del report['contradictions'][0]['kind']
        with self.assertRaises(UserError):validate_report(report,rows)

    def test_unsupported_item_is_removed_without_losing_valid_observation(self):
        rows=self.rows();rows[0][0]=RATINGS[0];rows[1][2]=RATINGS[2]
        report=self.empty()
        report['development']=[{'theme':'Неверный вывод','evidence':[{'response':1,'question':1,'quote':RATINGS[0]}]},
          {'theme':'Не всегда понятны идеи','evidence':[{'response':2,'question':3,'quote':RATINGS[2]}]}]
        filtered=filter_supported_items(report,rows,QUESTIONNAIRE)
        self.assertEqual(len(filtered['development']),1)
        self.assertEqual(filtered['development'][0]['theme'],'Не всегда понятны идеи')
        self.assertEqual(filtered['_rejected_items'],1)

    def test_report_is_brief_even_for_long_cached_analysis(self):
        item={'theme':'Длинное наблюдение '*15,'explanation':'Объяснение '*45,'action':'Конкретное действие '*25,'check':'Проверить результат '*15,'count':1}
        report={'strengths':[item]*4,'development':[item]*4,'advice':[],'contradictions':[item]*4}
        text=format_report({'subject':'Тестовый сотрудник','demo':False},report,2)
        self.assertLess(len(text),1500)
        self.assertNotIn('Объяснение',text.split('Стоит уточнить')[0])
        self.assertEqual(text.count('Как проверить:'),2)
