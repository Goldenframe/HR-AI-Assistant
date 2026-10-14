"""Fill the authored Excel template using stdlib only; preserve original text."""
from io import BytesIO
from pathlib import Path
import math
import re
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

from helpers import UserError

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('', NS)

def column(number):
    result = ''
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result

def make_cell(number, row, value, style):
    attributes = {'r': f'{column(number)}{row}', 's': style}
    cell = ET.Element(f'{{{NS}}}c', attributes)
    if type(value) is int:
        ET.SubElement(cell, f'{{{NS}}}v').text = str(value)
    else:
        cell.set('t', 'inlineStr')
        node = ET.SubElement(ET.SubElement(cell, f'{{{NS}}}is'), f'{{{NS}}}t')
        node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        node.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
    return cell

def export_answers(subject, questions, answers):
    if not questions or not answers or any(len(row) != len(questions) for row in answers):
        raise UserError('Ответы не соответствуют сохранённой анкете.')
    template = Path(__file__).parent / 'fixtures/feedback-export-template.xlsx'
    try:
        with ZipFile(template) as source:
            files = {name: source.read(name) for name in source.namelist()}
        root = ET.fromstring(files['xl/worksheets/sheet1.xml'])
        data = root.find(f'{{{NS}}}sheetData')
        sample_header, sample_row = list(data)[:2]
        header_style = sample_header[2].get('s', '0')
        body_style = sample_row[2].get('s', '0')
        comment_style = sample_row[3].get('s', body_style)
        identifying_style = sample_row[0].get('s', body_style)
        number_style = sample_row[1].get('s', body_style)
        data.clear()
        headers = ['На кого обратная связь', 'Отзыв', *[q['text'] for q in questions]]
        header = ET.SubElement(data, f'{{{NS}}}row', {'r': '1', 'ht': '100', 'customHeight': '1'})
        for number, text in enumerate(headers, 1):
            header.append(make_cell(number, 1, text, header_style))
        for number, answers_row in enumerate(answers, 1):
            row_number = number + 1
            lines = max([2, math.ceil(len(subject)/26)] + [
                sum(max(1, math.ceil(len(line)/(45 if q.get('type') != 'rating' else 28)))
                    for line in str(value).split('\n'))
                for q, value in zip(questions, answers_row)])
            row = ET.SubElement(data, f'{{{NS}}}row', {'r': str(row_number),
                'ht': str(min(409, max(42, lines*14+10))), 'customHeight': '1'})
            row.append(make_cell(1, row_number, subject, identifying_style))
            row.append(make_cell(2, row_number, number, number_style))
            for index, (q, value) in enumerate(zip(questions, answers_row), 3):
                row.append(make_cell(index, row_number, value,
                                     body_style if q.get('type') == 'rating' else comment_style))
        dimension = root.find(f'{{{NS}}}dimension')
        if dimension is None:
            dimension = ET.Element(f'{{{NS}}}dimension')
            root.insert(1 if root.find(f'{{{NS}}}sheetPr') is not None else 0, dimension)
        dimension.set('ref', f'A1:{column(len(headers))}{len(answers)+1}')
        cols = root.find(f'{{{NS}}}cols')
        cols.clear()
        widths = [26, 10, *[28 if q.get('type') == 'rating' else 45 for q in questions]]
        for index, width in enumerate(widths, 1):
            ET.SubElement(cols, f'{{{NS}}}col', {'min':str(index), 'max':str(index),
                                              'width':str(width), 'customWidth':'1'})
        old_filter = root.find(f'{{{NS}}}autoFilter')
        if old_filter is not None:root.remove(old_filter)
        root.insert(list(root).index(data)+1, ET.Element(f'{{{NS}}}autoFilter',
                    {'ref':f'A1:{column(len(headers))}{len(answers)+1}'}))
        files['xl/worksheets/sheet1.xml'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        output = BytesIO()
        with ZipFile(output, 'w', ZIP_DEFLATED) as result:
            for name, content in files.items():result.writestr(name, content)
        return output.getvalue()
    except (OSError, KeyError, ValueError, ET.ParseError) as exc:
        raise UserError('Не удалось подготовить Excel с исходными ответами.') from None
