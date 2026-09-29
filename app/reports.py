from __future__ import annotations

import io
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, current_app, render_template, request, send_file
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

reports_bp = Blueprint('reports', __name__)
JP_FONT = 'HeiseiKakuGo-W5'


def _db():
    connection = sqlite3.connect(current_app.config['DATABASE_PATH'])
    connection.row_factory = sqlite3.Row
    return connection


@reports_bp.get('/reports')
def reports():
    company_id = request.args.get('company_id', type=int)
    include_inactive = request.args.get('include_inactive') == '1'
    with _db() as db:
        companies = db.execute('select * from companies order by name').fetchall()
        if company_id is None and companies:
            company_id = companies[0]['id']
        employees = db.execute(
            '''select e.*, c.name company_name from employees e
               join companies c on c.id=e.company_id
               where (? is null or e.company_id=?) and (?=1 or e.active=1)
               order by c.name, e.employee_code''',
            (company_id, company_id, int(include_inactive)),
        ).fetchall()
    return render_template('reports.html', companies=companies, employees=employees,
                           company_id=company_id, include_inactive=include_inactive,
                           current_year=date.today().year)


@reports_bp.post('/reports/pdf')
def report_pdf():
    year = request.form.get('year', type=int) or date.today().year
    company_id = request.form.get('company_id', type=int)
    selected = [int(value) for value in request.form.getlist('employee_ids') if value.isdigit()]
    include_inactive = request.form.get('include_inactive') == '1'
    with _db() as db:
        sql = '''select e.*, c.name company_name, c.legal_name, c.closing_month
                 from employees e join companies c on c.id=e.company_id
                 where (? is null or e.company_id=?) and (?=1 or e.active=1)'''
        params = [company_id, company_id, int(include_inactive)]
        if selected:
            sql += ' and e.id in (' + ','.join('?' for _ in selected) + ')'
            params.extend(selected)
        sql += ' order by c.name, e.employee_code'
        employees = db.execute(sql, params).fetchall()
        payload = [_employee_payload(db, employee, year) for employee in employees]
    stream = _build_pdf(payload, year)
    filename = f'paid_leave_report_{year}_{date.today().isoformat()}.pdf'
    return send_file(stream, mimetype='application/pdf', as_attachment=True, download_name=filename)


def _employee_payload(db, employee, year):
    start, end = f'{year}-01-01', f'{year}-12-31'
    grants = db.execute('select * from leave_grants where employee_id=? order by grant_date',
                        (employee['id'],)).fetchall()
    usages = db.execute(
        '''select * from leave_usages where employee_id=? and leave_date between ? and ?
           order by leave_date, id''', (employee['id'], start, end)).fetchall()
    return {'employee': employee, 'grants': grants,
            'planned': [row for row in usages if row['leave_kind'] == 'planned'],
            'individual': [row for row in usages if row['leave_kind'] != 'planned'],
            'current_balance': min(_remaining_on(db, employee['id'], date.today()), 40.0)}


def _remaining_on(db, employee_id, target):
    grants = [{'start': row['grant_date'], 'end': row['expires_on'], 'left': float(row['days'])}
              for row in db.execute(
                  'select grant_date, expires_on, days from leave_grants '
                  'where employee_id=? and grant_date<=? order by expires_on, grant_date, id',
                  (employee_id, target.isoformat())).fetchall()]
    usages = db.execute(
        'select leave_date, days from leave_usages where employee_id=? and leave_date<=? '
        'order by leave_date, id', (employee_id, target.isoformat())).fetchall()
    for usage in usages:
        amount = float(usage['days'])
        for grant in grants:
            if amount <= 0:
                break
            if grant['start'] <= usage['leave_date'] <= grant['end']:
                applied = min(grant['left'], amount)
                grant['left'] -= applied
                amount -= applied
    return max(sum(grant['left'] for grant in grants
                   if grant['start'] <= target.isoformat() <= grant['end']), 0.0)


def _p(value, style):
    return Paragraph(str(value or '-').replace('&', '&amp;').replace('<', '&lt;'), style)


def _table(rows, widths, header=True):
    table = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign='LEFT')
    commands = [
        ('FONTNAME', (0, 0), (-1, -1), JP_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('LEADING', (0, 0), (-1, -1), 11),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#9aa7a5')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    if header:
        commands += [('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#dcefeb')),
                     ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#153936'))]
    table.setStyle(TableStyle(commands))
    return table


def _usage_rows(items, body):
    rows = [[_p('\u884c\u4f7f\u65e5', body), _p('\u65e5\u6570', body),
             _p('\u7a2e\u5225', body), _p('\u30e1\u30e2', body)]]
    labels = {'planned': '\u8a08\u753b\u5e74\u4f11', 'full': '\u5168\u65e5',
              'half_am': '\u5348\u524d\u534a\u4f11', 'half_pm': '\u5348\u5f8c\u534a\u4f11'}
    for item in items:
        rows.append([_p(item['leave_date'], body), _p(item['days'], body),
                     _p(labels.get(item['leave_kind'], item['leave_kind']), body),
                     _p(item['note'], body)])
    if len(rows) == 1:
        rows.append([_p('-', body), _p('-', body), _p('-', body), _p('\u8a72\u5f53\u306a\u3057', body)])
    return rows


def _build_pdf(payload, year):
    pdfmetrics.registerFont(UnicodeCIDFont(JP_FONT))
    stream = io.BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, rightMargin=14*mm, leftMargin=14*mm,
                                 topMargin=15*mm, bottomMargin=14*mm,
                                 title=f'Paid leave report {year}')
    styles = getSampleStyleSheet()
    title = ParagraphStyle('jp-title', parent=styles['Title'], fontName=JP_FONT,
                           fontSize=17, leading=22, textColor=colors.HexColor('#153936'))
    heading = ParagraphStyle('jp-heading', parent=styles['Heading2'], fontName=JP_FONT,
                             fontSize=11, leading=15, spaceBefore=7, spaceAfter=4,
                             textColor=colors.HexColor('#236c66'))
    body = ParagraphStyle('jp-body', parent=styles['BodyText'], fontName=JP_FONT,
                          fontSize=8, leading=11)
    small_right = ParagraphStyle('jp-right', parent=body, alignment=TA_RIGHT)
    story = []
    for index, item in enumerate(payload):
        if index:
            story.append(PageBreak())
        employee = item['employee']
        story += [_p('\u5e74\u6b21\u6709\u7d66\u4f11\u6687\u7ba1\u7406\u5e33\u7968', title),
                  _p(f'\u5bfe\u8c61\u5e74: {year}\u5e74    \u4f5c\u6210\u65e5: {date.today().isoformat()}', small_right),
                  Spacer(1, 3*mm)]
        master = [
            [_p('\u4f1a\u793e\u540d', body), _p(employee['company_name'], body),
             _p('\u793e\u54e1\u756a\u53f7', body), _p(employee['employee_code'], body)],
            [_p('\u6c0f\u540d', body), _p(employee['name'], body),
             _p('\u5165\u793e\u65e5', body), _p(employee['hire_date'], body)],
            [_p('\u6c0f\u540d\u30ab\u30ca', body), _p(employee['name_kana'], body),
             _p('\u751f\u5e74\u6708\u65e5', body), _p(employee['birth_date'], body)],
            [_p('\u90e8\u7f72', body), _p(employee['department'], body),
             _p('\u5f79\u8077', body), _p(employee['position'], body)],
            [_p('\u96c7\u7528\u533a\u5206', body), _p(employee['employment_status'], body),
             _p('\u73fe\u5728\u6b8b', body), _p(f"{item['current_balance']:.1f} \u65e5", body)],
            [_p('\u9031\u6240\u5b9a\u52b4\u50cd', body),
             _p(f"{employee['weekly_work_days']}\u65e5 / {employee['weekly_work_hours']}\u6642\u9593", body),
             _p('\u5728\u7c4d\u72b6\u614b', body), _p('\u5728\u7c4d' if employee['active'] else '\u9000\u8077', body)],
            [_p('\u9023\u7d61\u5148', body), _p(employee['phone'], body),
             _p('\u30e1\u30fc\u30eb', body), _p(employee['email'], body)],
            [_p('\u4f4f\u6240', body), _p(employee['address'], body),
             _p('\u7dca\u6025\u9023\u7d61\u5148', body), _p(employee['emergency_contact'], body)],
            [_p('\u5e74\u9593\u6240\u5b9a\u52b4\u50cd\u65e5\u6570', body),
             _p(employee['scheduled_annual_work_days'], body),
             _p('\u9000\u8077\u65e5', body), _p(employee['retirement_date'], body)],
            [_p('\u793e\u4f1a\u4fdd\u967a\u30e1\u30e2', body), _p(employee['social_insurance_note'], body),
             _p('\u30c7\u30fc\u30bf\u767b\u9332\u65e5', body), _p(employee['created_at'], body)],
        ]
        story += [_table(master, [27*mm, 64*mm, 27*mm, 64*mm], header=False),
                  _p('\u4ed8\u4e0e\u5c65\u6b74', heading)]
        grant_rows = [[_p('\u4ed8\u4e0e\u65e5', body), _p('\u65e5\u6570', body),
                       _p('\u5931\u52b9\u65e5', body), _p('\u533a\u5206', body), _p('\u30e1\u30e2', body)]]
        for grant in item['grants']:
            grant_rows.append([_p(grant['grant_date'], body), _p(grant['days'], body),
                               _p(grant['expires_on'], body), _p(grant['source'], body), _p(grant['note'], body)])
        story += [_table(grant_rows, [25*mm, 15*mm, 25*mm, 25*mm, 92*mm]),
                  _p('\u8a08\u753b\u5e74\u4f11\u306e\u8a2d\u5b9a\u65e5', heading),
                  _table(_usage_rows(item['planned'], body), [28*mm, 17*mm, 30*mm, 107*mm]),
                  _p('\u500b\u5225\u306b\u53d6\u5f97\u3057\u305f\u884c\u4f7f\u65e5', heading),
                  _table(_usage_rows(item['individual'], body), [28*mm, 17*mm, 30*mm, 107*mm])]
    if not payload:
        story.append(_p('\u51fa\u529b\u5bfe\u8c61\u304c\u3042\u308a\u307e\u305b\u3093\u3002', title))
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    stream.seek(0)
    return stream


def _footer(canvas, document):
    canvas.saveState()
    canvas.setFont(JP_FONT, 8)
    canvas.setFillColor(colors.HexColor('#63716f'))
    canvas.drawRightString(A4[0] - 14*mm, 8*mm, f'{document.page}')
    canvas.restoreState()
