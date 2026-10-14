"""Read-only, bounded Jira workload snapshot; no capacity or performance inference."""
from datetime import date, datetime, timedelta, timezone
import json
import time
from urllib.parse import urlencode
from helpers import UserError


def unavailable(reason='unavailable'):
    return {'available': False, 'reason': reason}


def jira_workload(matcher, employee_id, today=None):
    employee = next((e for e in matcher.directory['employees'] if e['id'] == employee_id), None)
    if not employee or not employee.get('jira_user'):
        return unavailable('account_unmapped')
    today = today or datetime.now(timezone(timedelta(hours=3))).date()
    deadline = time.monotonic() + 20
    jql = 'assignee = ' + json.dumps(employee['jira_user']) + ' AND statusCategory != Done ORDER BY key'
    result = {'available': True, 'as_of': today.isoformat(), 'open_tasks': 0, 'estimated_tasks': 0,
              'remaining_seconds': 0, 'overdue_tasks': 0, 'due_next_14_days': 0, 'without_due_date': 0,
              'scope': 'visible_assigned_unfinished_issues', 'capacity_known': False}
    seen, start = set(), 0
    for _ in range(20):
        remaining = deadline - time.monotonic()
        if remaining <= 0:raise UserError('Не удалось вовремя получить загрузку Jira.')
        page = matcher.fetch(matcher.jira_url + '/rest/api/2/search?' + urlencode(
            {'jql': jql, 'startAt': start, 'maxResults': 100,
             'fields': 'status,duedate,timeestimate,timetracking'}),
            headers=matcher.jira_headers, timeout=min(8, remaining))
        issues, total = page.get('issues'), page.get('total')
        if not isinstance(issues, list) or type(total) is not int or total < 0:
            raise UserError('Jira вернула неполные данные загрузки.')
        for issue in issues:
            key, fields = issue.get('key'), issue.get('fields')
            if not key or key in seen or not isinstance(fields, dict):
                raise UserError('Jira вернула неполную выборку загрузки.')
            seen.add(key)
            if (fields.get('status') or {}).get('statusCategory', {}).get('key') == 'done':continue
            result['open_tasks'] += 1
            estimate = fields.get('timeestimate')
            if estimate is None:estimate = (fields.get('timetracking') or {}).get('remainingEstimateSeconds')
            if type(estimate) is int and estimate >= 0:
                result['estimated_tasks'] += 1
                result['remaining_seconds'] += estimate
            due = fields.get('duedate')
            try:due = date.fromisoformat(due) if due else None
            except (ValueError, TypeError):due = None
            if due is None:result['without_due_date'] += 1
            elif due < today:result['overdue_tasks'] += 1
            elif due <= today + timedelta(days=14):result['due_next_14_days'] += 1
        start += len(issues)
        if start >= total:
            result['unestimated_tasks'] = result['open_tasks'] - result['estimated_tasks']
            return result
        if not issues:raise UserError('Jira вернула неполную выборку загрузки.')
    raise UserError('Слишком большая выборка загрузки Jira.')


def workload_text(snapshot):
    if not snapshot or not snapshot.get('available'):
        return 'Нагрузка Jira: данные недоступны; рекомендации без оценки занятости.'
    tasks = snapshot['open_tasks']
    if tasks == 0:
        return 'В Jira нет доступных незавершённых задач сотрудника. Это не означает отсутствие нагрузки.'
    text = f"Jira: открытых задач — {tasks}; со сроком в ближайшие 14 дней — {snapshot['due_next_14_days']}; с прошедшим сроком — {snapshot['overdue_tasks']}."
    if snapshot['estimated_tasks']:
        hours = round(snapshot['remaining_seconds'] / 3600, 1)
        text += f" Осталось по оценкам: {hours:g} ч."
    if snapshot['unestimated_tasks']:
        text += f" Без оценки: {snapshot['unestimated_tasks']}."
    return text + ' Это часть нагрузки, свободное время неизвестно.'
