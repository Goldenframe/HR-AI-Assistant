"""Synthetic quality evaluation; no employee data or Telegram messages."""
import json
from pathlib import Path
import time
from ai import generate_report, format_report
from survey import SKIP

CASES = {
    'critical': [
        [SKIP, 'Передаёт задачи без описания.', SKIP, 'Не предупреждает о задержках.', SKIP, SKIP, SKIP, 'Советую заранее предупреждать о задержках.'],
        [SKIP, 'Передаёт задачи без описания.', SKIP, 'Часто не предупреждает о задержках.', SKIP, SKIP, SKIP, 'Рекомендую заранее предупреждать о задержках.'],
        [SKIP, 'Задачи передаёт без описания.', SKIP, SKIP, SKIP, SKIP, SKIP, 'Советую добавлять описание к передаваемым задачам.'],
    ],
    'conflicting': [
        [SKIP, 'Не хватает документации.', 'Заранее сообщает о рисках и задержках.', SKIP, SKIP, SKIP, SKIP, 'Рекомендую дополнять документацию.'],
        [SKIP, SKIP, SKIP, 'Сообщает о рисках только после срыва сроков.', 'Помогает разбирать ошибки.', SKIP, SKIP, 'Рекомендую сообщать о рисках раньше.'],
        [SKIP, 'Иногда пропускает документацию.', 'Заранее сообщает о рисках и задержках.', SKIP, SKIP, SKIP, SKIP, 'Рекомендую дополнять документацию.'],
    ],
    'vague': [['Норм', SKIP, SKIP, SKIP, SKIP, SKIP, SKIP, 'Без советов'],
              ['Хороший человек', SKIP, SKIP, SKIP, SKIP, SKIP, SKIP, 'Без рекомендаций'],
              ['Всё нормально', SKIP, SKIP, SKIP, SKIP, SKIP, SKIP, SKIP]],
    'skipped': [[SKIP] * 8 for _ in range(3)],
}


def evaluate(settings):
    results = []
    for name, answers in CASES.items():
        started = time.monotonic()
        report = generate_report(answers, settings)
        # These assess concrete properties, not complete semantic accuracy.
        if name == 'critical':
            passed = not report['strengths'] and bool(report['development'])
        elif name == 'conflicting':
            passed = bool(report['contradictions'])
        else:
            passed = not any(report.get(k) for k in ('strengths', 'development', 'advice', 'contradictions'))
        result = {'case': name, 'seconds': round(time.monotonic() - started, 2),
                  'structural_check': passed, 'report': report}
        results.append(result)
        print(f"\n{name}: {'PASS' if passed else 'REVIEW REQUIRED'}, {result['seconds']} сек.")
        print(format_report({'subject': 'Вымышленный сотрудник', 'demo': True}, report, 3, 'hr', 3))
    path = Path(__file__).resolve().parent / 'data' / 'evaluation.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\nПроверки структуры не заменяют оценку смысла человеком. Результаты:', path)
    if not all(r['structural_check'] for r in results):
        raise SystemExit(1)
