from __future__ import annotations

import csv
import io
import os
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from .reports import reports_bp


BACKUP_TABLES = [
    "companies",
    "employees",
    "leave_grants",
    "leave_usages",
    "planned_leave_days",
]

CSV_NULL = "\\N"

STATUTORY_GRANT_DAYS = [
    (6, 10),
    (18, 11),
    (30, 12),
    (42, 14),
    (54, 16),
    (66, 18),
    (78, 20),
]

LEAVE_TYPES = {
    "employee": "本人指定",
    "planned": "計画行使",
    "employer": "使用者時季指定",
}


@dataclass(frozen=True)
class GrantRule:
    months_after_hire: int
    days: int


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["DATABASE_PATH"] = os.environ.get("DATABASE_PATH", "instance/nenkyuu.db")
    app.config["APP_START_YEAR"] = int(os.environ.get("APP_START_YEAR", "2026"))

    Path(app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        init_db(app.config["DATABASE_PATH"])
        seed_defaults(app.config["DATABASE_PATH"], app.config["APP_START_YEAR"])

    @app.template_filter("ymd")

    def ymd(value: str | None) -> str:
        return value or ""

    @app.template_filter("leave_type")
    def leave_type(value: str) -> str:
        return LEAVE_TYPES.get(value, value)

    @app.route("/")
    def dashboard():
        year = int(request.args.get("year", app.config["APP_START_YEAR"]))
        with get_db(app) as db:
            companies = db.execute("select * from companies order by name").fetchall()
            company_id = request.args.get("company_id", type=int)
            if company_id is None and companies:
                company_id = companies[0]["id"]
            refresh_annual_data(db, year)
            employees = get_employee_balances(db, year, company_id)
            planned_days = db.execute(
                """
                select p.*, c.name as company_name
                from planned_leave_days p
                join companies c on c.id = p.company_id
                where strftime('%Y', p.leave_date) = ?
                  and (? is null or p.company_id = ?)
                order by p.leave_date, c.name
                """,
                (str(year), company_id, company_id),
            ).fetchall()
        return render_template(
            "dashboard.html",
            companies=companies,
            company_id=company_id,
            employees=employees,
            planned_days=planned_days,
            year=year,
        )

    @app.route("/companies", methods=["GET", "POST"])
    def companies():
        with get_db(app) as db:
            if request.method == "POST":
                db.execute(
                    """
                    insert into companies(name, legal_name, closing_month, created_at)
                    values(?, ?, ?, ?)
                    """,
                    (
                        request.form["name"].strip(),
                        request.form.get("legal_name", "").strip(),
                        int(request.form.get("closing_month") or 12),
                        now(),
                    ),
                )
                db.commit()
                flash("会社を登録しました。")
                return redirect(url_for("companies"))
            rows = db.execute("select * from companies order by name").fetchall()
        return render_template("companies.html", companies=rows)

    @app.route("/employees", methods=["GET", "POST"])
    def employees():
        with get_db(app) as db:
            companies = db.execute("select * from companies order by name").fetchall()
            if request.method == "POST":
                employee_id = create_employee(db, request.form)
                db.commit()
                refresh_employee_data(db, employee_id, app.config["APP_START_YEAR"], date.today().year + 1)
                db.commit()
                flash("従業員を登録しました。")
                return redirect(url_for("employees"))
            rows = db.execute(
                """
                select e.*, c.name as company_name
                from employees e
                join companies c on c.id = e.company_id
                order by c.name, e.employee_code
                """
            ).fetchall()
        return render_template("employees.html", companies=companies, employees=rows)

    @app.route("/employees/upload", methods=["POST"])
    def upload_employees():
        company_id = int(request.form["company_id"])
        upload = request.files.get("csv_file")
        if upload is None or upload.filename == "":
            flash("CSVファイルを選択してください。")
            return redirect(url_for("employees"))

        try:
            rows = read_employee_csv(upload.read())
        except UnicodeDecodeError:
            flash("CSVの文字コードを読み取れませんでした。UTF-8またはShift_JISで保存してください。")
            return redirect(url_for("employees"))

        created = 0
        updated = 0
        errors = []
        with get_db(app) as db:
            for index, row in enumerate(rows, start=2):
                try:
                    result = upsert_employee_from_csv(db, row, company_id)
                    if result == "created":
                        created += 1
                    elif result == "updated":
                        updated += 1
                except ValueError as exc:
                    errors.append(f"{index}行目: {exc}")
            db.commit()
            for employee in db.execute("select id from employees where active = 1").fetchall():
                refresh_employee_data(db, employee["id"], app.config["APP_START_YEAR"], date.today().year + 1)
            db.commit()

        if errors:
            flash(" / ".join(errors[:5]))
        flash(f"CSV取込が完了しました。追加 {created} 件、更新 {updated} 件。")
        return redirect(url_for("employees"))
    @app.route("/employees/<int:employee_id>")
    def employee_detail(employee_id: int):
        with get_db(app) as db:
            refresh_employee_data(db, employee_id, app.config["APP_START_YEAR"], date.today().year + 1)
            employee = db.execute(
                """
                select e.*, c.name as company_name
                from employees e
                join companies c on c.id = e.company_id
                where e.id = ?
                """,
                (employee_id,),
            ).fetchone()
            if employee is None:
                flash("従業員が見つかりません。")
                return redirect(url_for("employees"))
            grants = db.execute(
                "select * from leave_grants where employee_id = ? order by grant_date desc",
                (employee_id,),
            ).fetchall()
            leaves = db.execute(
                "select * from leave_usages where employee_id = ? order by leave_date desc, id desc",
                (employee_id,),
            ).fetchall()
            balance = get_employee_balance(db, employee_id, date.today().year)
        return render_template(
            "employee_detail.html",
            employee=employee,
            grants=grants,
            leaves=leaves,
            balance=balance,
            leave_types=LEAVE_TYPES,
        )

    @app.route("/leave-usages", methods=["POST"])
    def add_leave_usage():
        employee_id = int(request.form["employee_id"])
        leave_date = request.form["leave_date"]
        days = float(request.form.get("days") or 1)
        leave_kind = request.form.get("leave_kind") or "employee"
        note = request.form.get("note", "").strip()
        with get_db(app) as db:
            refresh_employee_data(db, employee_id, app.config["APP_START_YEAR"], date.today().year + 1)
            available = available_days_on(db, employee_id, parse_date(leave_date))
            if days <= 0:
                flash("行使日数は0より大きい値で入力してください。")
            elif available < days:
                flash(f"残日数が不足しています。指定日時点の残日数は {available:.1f} 日です。")
            else:
                db.execute(
                    """
                    insert into leave_usages(employee_id, leave_date, days, leave_kind, note, created_at)
                    values(?, ?, ?, ?, ?, ?)
                    """,
                    (employee_id, leave_date, days, leave_kind, note, now()),
                )
                db.commit()
                flash("年休行使を登録しました。")
        return redirect(url_for("employee_detail", employee_id=employee_id))

    @app.route("/planned-days", methods=["POST"])
    def add_planned_day():
        with get_db(app) as db:
            company_id = int(request.form["company_id"])
            leave_date = request.form["leave_date"]
            note = request.form.get("note", "").strip() or "会社指定計画行使"
            try:
                db.execute(
                    """
                    insert into planned_leave_days(company_id, leave_date, days, note, created_at)
                    values(?, ?, 1, ?, ?)
                    """,
                    (company_id, leave_date, note, now()),
                )
                apply_planned_day(db, company_id, parse_date(leave_date), note)
                db.commit()
                flash("計画行使日を登録し、対象従業員へ反映しました。")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("同じ会社・日付の計画行使日は既に登録されています。")
        return redirect(url_for("dashboard", year=parse_date(leave_date).year, company_id=company_id))

    @app.route("/planned-days/<int:planned_day_id>", methods=["POST"])
    def update_planned_day(planned_day_id: int):
        with get_db(app) as db:
            planned_day = db.execute(
                "select * from planned_leave_days where id = ?",
                (planned_day_id,),
            ).fetchone()
            if planned_day is None:
                flash("計画行使日が見つかりません。")
                return redirect(url_for("dashboard"))

            company_id = int(request.form["company_id"])
            leave_date = request.form["leave_date"]
            note = request.form.get("note", "").strip() or "会社指定計画行使"
            old_date = parse_date(planned_day["leave_date"])
            new_date = parse_date(leave_date)
            try:
                remove_planned_day_usages(db, planned_day["company_id"], old_date)
                db.execute(
                    """
                    update planned_leave_days
                    set company_id = ?, leave_date = ?, note = ?
                    where id = ?
                    """,
                    (company_id, leave_date, note, planned_day_id),
                )
                apply_planned_day(db, company_id, new_date, note)
                db.commit()
                flash("計画行使日を更新し、対象従業員へ再反映しました。")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("同じ会社・日付の計画行使日が既にあります。")
                company_id = planned_day["company_id"]
                new_date = old_date
        return redirect(url_for("dashboard", year=new_date.year, company_id=company_id))

    @app.route("/planned-days/<int:planned_day_id>/delete", methods=["POST"])
    def delete_planned_day(planned_day_id: int):
        with get_db(app) as db:
            planned_day = db.execute(
                "select * from planned_leave_days where id = ?",
                (planned_day_id,),
            ).fetchone()
            if planned_day is None:
                flash("計画行使日が見つかりません。")
                return redirect(url_for("dashboard"))

            company_id = planned_day["company_id"]
            leave_date = parse_date(planned_day["leave_date"])
            remove_planned_day_usages(db, company_id, leave_date)
            db.execute("delete from planned_leave_days where id = ?", (planned_day_id,))
            db.commit()
        flash("計画行使日を削除し、反映済みの計画行使も取り消しました。")
        return redirect(url_for("dashboard", year=leave_date.year, company_id=company_id))

    @app.route("/backup")
    def backup():
        return render_template("backup.html")

    @app.route("/backup/export")
    def export_backup():
        with get_db(app) as db:
            archive = export_database_csv_zip(db)
        filename = f"nenkyuuman-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return send_file(
            archive,
            as_attachment=True,
            download_name=filename,
            mimetype="application/zip",
        )

    @app.route("/backup/restore", methods=["POST"])
    def restore_backup():
        upload = request.files.get("backup_file")
        if upload is None or upload.filename == "":
            flash("復元するバックアップZIPを選択してください。")
            return redirect(url_for("backup"))
        try:
            with get_db(app) as db:
                restore_database_csv_zip(db, upload.read())
                db.commit()
        except (ValueError, zipfile.BadZipFile, sqlite3.Error) as exc:
            flash(f"バックアップの復元に失敗しました: {exc}")
            return redirect(url_for("backup"))
        flash("バックアップからデータベースを復元しました。")
        return redirect(url_for("dashboard"))

    @app.route("/refresh", methods=["POST"])
    def refresh():
        year = int(request.form.get("year") or app.config["APP_START_YEAR"])
        with get_db(app) as db:
            refresh_annual_data(db, year)
            db.commit()
        flash(f"{year}年の付与・計画行使を再計算しました。")
        return redirect(url_for("dashboard", year=year, company_id=request.form.get("company_id")))

    app.register_blueprint(reports_bp)
    return app


def export_database_csv_zip(db: sqlite3.Connection) -> io.BytesIO:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as backup:
        backup.writestr("README.txt", "NENKYUUMAN CSV backup. Restore this ZIP from the backup screen.\n")
        for table in BACKUP_TABLES:
            columns = table_columns(db, table)
            csv_buffer = io.StringIO(newline="")
            writer = csv.writer(csv_buffer)
            writer.writerow(columns)
            rows = db.execute(
                f"select {', '.join(columns)} from {table} order by id"
            ).fetchall()
            for row in rows:
                writer.writerow([csv_encode_value(row[column]) for column in columns])
            backup.writestr(f"{table}.csv", csv_buffer.getvalue().encode("utf-8-sig"))
    archive.seek(0)
    return archive


def restore_database_csv_zip(db: sqlite3.Connection, payload: bytes) -> None:
    if not payload:
        raise ValueError("ファイルが空です。")
    with zipfile.ZipFile(io.BytesIO(payload)) as backup:
        csv_names = set(backup.namelist())
        missing = [f"{table}.csv" for table in BACKUP_TABLES if f"{table}.csv" not in csv_names]
        if missing:
            raise ValueError("必要なCSVが不足しています: " + ", ".join(missing))

        table_rows: dict[str, list[dict[str, Any]]] = {}
        for table in BACKUP_TABLES:
            columns = table_columns(db, table)
            raw = backup.read(f"{table}.csv")
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames != columns:
                raise ValueError(f"{table}.csv の列が現在のDB定義と一致しません。")
            table_rows[table] = [
                {column: csv_decode_value(row[column]) for column in columns}
                for row in reader
            ]

    db.execute("pragma foreign_keys = off")
    try:
        for table in reversed(BACKUP_TABLES):
            db.execute(f"delete from {table}")
        for table in BACKUP_TABLES:
            columns = table_columns(db, table)
            placeholders = ", ".join("?" for _ in columns)
            column_list = ", ".join(columns)
            for row in table_rows[table]:
                db.execute(
                    f"insert into {table}({column_list}) values({placeholders})",
                    [row[column] for column in columns],
                )
        db.execute("pragma foreign_keys = on")
        violations = db.execute("pragma foreign_key_check").fetchall()
        if violations:
            raise ValueError("復元データの関連付けに不整合があります。")
    except Exception:
        db.rollback()
        db.execute("pragma foreign_keys = on")
        raise


def table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    columns = [row[1] for row in db.execute(f"pragma table_info({table})").fetchall()]
    if not columns:
        raise ValueError(f"テーブルが見つかりません: {table}")
    return columns


def csv_encode_value(value: Any) -> str:
    if value is None:
        return CSV_NULL
    text = str(value)
    if text == CSV_NULL:
        return "\\N"
    return text


def csv_decode_value(value: str | None) -> str | None:
    if value == CSV_NULL:
        return None
    if value == "\\N":
        return CSV_NULL
    return value or ""

def get_db(app: Flask):
    db = sqlite3.connect(app.config["DATABASE_PATH"])
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys = on")
    return closing(db)


def init_db(path: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys = on")
        db.executescript(
            """
            create table if not exists companies (
                id integer primary key autoincrement,
                name text not null unique,
                legal_name text,
                closing_month integer not null default 12,
                created_at text not null
            );

            create table if not exists employees (
                id integer primary key autoincrement,
                company_id integer not null references companies(id),
                employee_code text not null,
                name text not null,
                name_kana text,
                email text,
                department text,
                position text,
                employment_status text not null default 'regular',
                hire_date text not null,
                birth_date text,
                weekly_work_days real not null default 5,
                weekly_work_hours real not null default 40,
                scheduled_annual_work_days integer,
                address text,
                phone text,
                emergency_contact text,
                social_insurance_note text,
                active integer not null default 1,
                created_at text not null,
                unique(company_id, employee_code)
            );

            create table if not exists leave_grants (
                id integer primary key autoincrement,
                employee_id integer not null references employees(id) on delete cascade,
                grant_date text not null,
                days real not null,
                expires_on text not null,
                source text not null default 'statutory',
                note text,
                created_at text not null,
                unique(employee_id, grant_date)
            );

            create table if not exists leave_usages (
                id integer primary key autoincrement,
                employee_id integer not null references employees(id) on delete cascade,
                leave_date text not null,
                days real not null,
                leave_kind text not null,
                note text,
                created_at text not null,
                unique(employee_id, leave_date, leave_kind)
            );

            create table if not exists planned_leave_days (
                id integer primary key autoincrement,
                company_id integer not null references companies(id) on delete cascade,
                leave_date text not null,
                days real not null default 1,
                note text,
                created_at text not null,
                unique(company_id, leave_date)
            );
            """
        )


def seed_defaults(path: str, start_year: int) -> None:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys = on")
        count = db.execute("select count(*) from companies").fetchone()[0]
        if count == 0:
            db.executemany(
                """
                insert into companies(name, legal_name, closing_month, created_at)
                values(?, ?, 12, ?)
                """,
                [
                    ("株式会社サンプル物流", "株式会社サンプル物流", now()),
                    ("サンプル運輸有限会社", "サンプル運輸有限会社", now()),
                ],
            )
        for company in db.execute("select id from companies").fetchall():
            ensure_default_planned_days(db, company["id"], start_year)
        db.commit()


def create_employee(db: sqlite3.Connection, form: Any) -> int:
    cur = db.execute(
        """
        insert into employees(
            company_id, employee_code, name, name_kana, email, department, position,
            employment_status, hire_date, birth_date, weekly_work_days, weekly_work_hours,
            scheduled_annual_work_days, address, phone, emergency_contact,
            social_insurance_note, active, created_at
        )
        values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            int(form["company_id"]),
            form["employee_code"].strip(),
            form["name"].strip(),
            form.get("name_kana", "").strip(),
            form.get("email", "").strip(),
            form.get("department", "").strip(),
            form.get("position", "").strip(),
            form.get("employment_status", "regular"),
            form["hire_date"],
            form.get("birth_date") or None,
            float(form.get("weekly_work_days") or 5),
            float(form.get("weekly_work_hours") or 40),
            int(form["scheduled_annual_work_days"]) if form.get("scheduled_annual_work_days") else None,
            form.get("address", "").strip(),
            form.get("phone", "").strip(),
            form.get("emergency_contact", "").strip(),
            form.get("social_insurance_note", "").strip(),
            now(),
        ),
    )
    return int(cur.lastrowid)




def read_employee_csv(raw: bytes) -> list[dict[str, str]]:
    text = None
    for encoding in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise UnicodeDecodeError("csv", raw, 0, 1, "unsupported encoding")
    reader = csv.DictReader(io.StringIO(text))
    return [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def csv_value(row: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        if name in row and row[name] != "":
            return row[name]
    return default


def normalize_employment_status(value: str) -> str:
    mapping = {
        "正社員": "regular",
        "契約社員": "contract",
        "パート": "part_time",
        "アルバイト": "part_time",
        "派遣": "temporary",
        "その他": "temporary",
    }
    return mapping.get(value, value or "regular")


def company_id_for_csv_row(db: sqlite3.Connection, row: dict[str, str], fallback_company_id: int) -> int:
    company_name = csv_value(row, "会社", "会社名", "company", "company_name")
    if not company_name:
        return fallback_company_id
    company = db.execute("select id from companies where name = ?", (company_name,)).fetchone()
    if company is None:
        raise ValueError(f"会社が見つかりません: {company_name}")
    return int(company["id"])


def upsert_employee_from_csv(db: sqlite3.Connection, row: dict[str, str], fallback_company_id: int) -> str:
    company_id = company_id_for_csv_row(db, row, fallback_company_id)
    employee_code = csv_value(row, "社員番号", "従業員番号", "employee_code")
    name = csv_value(row, "氏名", "名前", "name")
    hire_date = csv_value(row, "入社日", "hire_date")
    if not employee_code:
        raise ValueError("社員番号が空です。")
    if not name:
        raise ValueError("氏名が空です。")
    if not hire_date:
        raise ValueError("入社日が空です。")
    parse_date(hire_date)

    values = {
        "name": name,
        "name_kana": csv_value(row, "氏名カナ", "フリガナ", "name_kana"),
        "email": csv_value(row, "メール", "メールアドレス", "email"),
        "department": csv_value(row, "部署", "department"),
        "position": csv_value(row, "役職", "position"),
        "employment_status": normalize_employment_status(csv_value(row, "雇用区分", "employment_status", default="regular")),
        "hire_date": hire_date,
        "birth_date": csv_value(row, "生年月日", "birth_date") or None,
        "weekly_work_days": float(csv_value(row, "週所定労働日数", "weekly_work_days", default="5")),
        "weekly_work_hours": float(csv_value(row, "週所定労働時間", "weekly_work_hours", default="40")),
        "scheduled_annual_work_days": csv_value(row, "年間所定労働日数", "scheduled_annual_work_days") or None,
        "address": csv_value(row, "住所", "address"),
        "phone": csv_value(row, "電話", "電話番号", "phone"),
        "emergency_contact": csv_value(row, "緊急連絡先", "emergency_contact"),
        "social_insurance_note": csv_value(row, "社会保険・労務メモ", "労務メモ", "social_insurance_note"),
    }
    if values["birth_date"]:
        parse_date(values["birth_date"])
    if values["scheduled_annual_work_days"] is not None:
        values["scheduled_annual_work_days"] = int(values["scheduled_annual_work_days"])

    existing = db.execute(
        "select id from employees where company_id = ? and employee_code = ?",
        (company_id, employee_code),
    ).fetchone()
    if existing:
        db.execute(
            """
            update employees set
                name = ?, name_kana = ?, email = ?, department = ?, position = ?,
                employment_status = ?, hire_date = ?, birth_date = ?, weekly_work_days = ?,
                weekly_work_hours = ?, scheduled_annual_work_days = ?, address = ?, phone = ?,
                emergency_contact = ?, social_insurance_note = ?, active = 1
            where id = ?
            """,
            (
                values["name"], values["name_kana"], values["email"], values["department"],
                values["position"], values["employment_status"], values["hire_date"],
                values["birth_date"], values["weekly_work_days"], values["weekly_work_hours"],
                values["scheduled_annual_work_days"], values["address"], values["phone"],
                values["emergency_contact"], values["social_insurance_note"], existing["id"],
            ),
        )
        return "updated"

    db.execute(
        """
        insert into employees(
            company_id, employee_code, name, name_kana, email, department, position,
            employment_status, hire_date, birth_date, weekly_work_days, weekly_work_hours,
            scheduled_annual_work_days, address, phone, emergency_contact,
            social_insurance_note, active, created_at
        )
        values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            company_id, employee_code, values["name"], values["name_kana"], values["email"],
            values["department"], values["position"], values["employment_status"],
            values["hire_date"], values["birth_date"], values["weekly_work_days"],
            values["weekly_work_hours"], values["scheduled_annual_work_days"], values["address"],
            values["phone"], values["emergency_contact"], values["social_insurance_note"], now(),
        ),
    )
    return "created"
def refresh_annual_data(db: sqlite3.Connection, year: int, as_of: date | None = None) -> None:
    for company in db.execute("select id from companies").fetchall():
        ensure_default_planned_days(db, company["id"], year)
    for employee in db.execute("select id from employees where active = 1").fetchall():
        refresh_employee_data(db, employee["id"], year, year + 1)
    for planned in db.execute(
        "select * from planned_leave_days where strftime('%Y', leave_date) = ?",
        (str(year),),
    ).fetchall():
        apply_planned_day(db, planned["company_id"], parse_date(planned["leave_date"]), planned["note"] or "会社指定計画行使")


def refresh_employee_data(db: sqlite3.Connection, employee_id: int, start_year: int, end_year: int, as_of: date | None = None) -> None:
    employee = db.execute("select * from employees where id = ?", (employee_id,)).fetchone()
    if employee is None:
        return
    hire_date = parse_date(employee["hire_date"])
    as_of = as_of or date.today()
    cutoff = min(as_of, date(end_year, 12, 31))
    active_from = date(start_year, 1, 1)
    horizon_end = date(end_year, 12, 31)
    expected_grant_dates = set()
    for rule_months, days in grant_rules_through(hire_date, start_year, end_year, cutoff):
        grant_date = add_months(hire_date, rule_months)
        expires_on = grant_date.replace(year=grant_date.year + 2) - timedelta(days=1)
        if grant_date <= cutoff and expires_on >= active_from:
            expected_grant_dates.add(grant_date.isoformat())
            sync_statutory_grant(db, employee, grant_date, days, expires_on)
    for grant in db.execute(
        """
        select id, grant_date from leave_grants
        where employee_id = ? and source = 'statutory'
          and grant_date <= ? and expires_on >= ?
        """,
        (employee_id, horizon_end.isoformat(), active_from.isoformat()),
    ).fetchall():
        if grant["grant_date"] not in expected_grant_dates:
            db.execute("delete from leave_grants where id = ?", (grant["id"],))


def sync_statutory_grant(
    db: sqlite3.Connection,
    employee: sqlite3.Row,
    grant_date: date,
    statutory_days: int,
    expires_on: date,
) -> None:
    days = prorated_days(employee, statutory_days)
    note = "\u52b4\u57fa\u6cd539\u6761\u306b\u57fa\u3065\u304f\u81ea\u52d5\u4ed8\u4e0e"
    result = db.execute(
        """
        update leave_grants
        set days = ?, expires_on = ?, source = 'statutory', note = ?
        where employee_id = ? and grant_date = ? and source = 'statutory'
        """,
        (days, expires_on.isoformat(), note, employee["id"], grant_date.isoformat()),
    )
    if result.rowcount == 0:
        db.execute(
            """
            insert into leave_grants(employee_id, grant_date, days, expires_on, source, note, created_at)
            values(?, ?, ?, ?, 'statutory', ?, ?)
            """,
            (employee["id"], grant_date.isoformat(), days, expires_on.isoformat(), note, now()),
        )


def grant_rules_through(hire_date: date, start_year: int, end_year: int, as_of: date | None = None) -> list[tuple[int, int]]:
    rules = list(STATUTORY_GRANT_DAYS)
    months = 90
    cutoff = min(as_of or date.today(), date(end_year, 12, 31))
    while add_months(hire_date, months) <= cutoff:
        rules.append((months, 20))
        months += 12
    active_from = date(start_year, 1, 1)
    return [
        (months_after_hire, days)
        for months_after_hire, days in rules
        if add_months(hire_date, months_after_hire).replace(
            year=add_months(hire_date, months_after_hire).year + 2
        )
        - timedelta(days=1)
        >= active_from
    ]


def ensure_default_planned_days(db: sqlite3.Connection, company_id: int, year: int) -> None:
    dates = [
        date(year, 1, 1),
        date(year, 1, 2),
        date(year, 1, 3),
        date(year, 12, 29),
        date(year, 12, 30),
    ]
    for planned_date in dates:
        db.execute(
            """
            insert or ignore into planned_leave_days(company_id, leave_date, days, note, created_at)
            values(?, ?, 1, '年末年始の計画行使', ?)
            """,
            (company_id, planned_date.isoformat(), now()),
        )


def remove_planned_day_usages(db: sqlite3.Connection, company_id: int, planned_date: date) -> None:
    db.execute(
        """
        delete from leave_usages
        where leave_kind = 'planned'
          and leave_date = ?
          and employee_id in (
              select id from employees
              where company_id = ?
          )
        """,
        (planned_date.isoformat(), company_id),
    )

def apply_planned_day(db: sqlite3.Connection, company_id: int, planned_date: date, note: str) -> None:
    employees = db.execute(
        """
        select id from employees
        where company_id = ? and active = 1 and hire_date <= ?
        """,
        (company_id, planned_date.isoformat()),
    ).fetchall()
    for employee in employees:
        available = available_days_on(db, employee["id"], planned_date)
        if available >= 1:
            db.execute(
                """
                insert or ignore into leave_usages(employee_id, leave_date, days, leave_kind, note, created_at)
                values(?, ?, 1, 'planned', ?, ?)
                """,
                (employee["id"], planned_date.isoformat(), note, now()),
            )


def get_employee_balances(db: sqlite3.Connection, year: int, company_id: int | None) -> list[dict[str, Any]]:
    employees = db.execute(
        """
        select e.*, c.name as company_name
        from employees e
        join companies c on c.id = e.company_id
        where e.active = 1 and (? is null or e.company_id = ?)
        order by c.name, e.employee_code
        """,
        (company_id, company_id),
    ).fetchall()
    return [get_employee_balance(db, employee["id"], year, employee) for employee in employees]


def get_employee_balance(
    db: sqlite3.Connection,
    employee_id: int,
    year: int,
    employee_row: sqlite3.Row | None = None,
) -> dict[str, Any]:
    employee = employee_row or db.execute(
        """
        select e.*, c.name as company_name
        from employees e
        join companies c on c.id = e.company_id
        where e.id = ?
        """,
        (employee_id,),
    ).fetchone()
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    balance_on = min(date.today(), end)
    if balance_on < start:
        balance_on = start
    granted_available = db.execute(
        """
        select coalesce(sum(days), 0) from leave_grants
        where employee_id = ? and grant_date <= ? and expires_on >= ?
        """,
        (employee_id, balance_on.isoformat(), balance_on.isoformat()),
    ).fetchone()[0]
    used = db.execute(
        """
        select coalesce(sum(days), 0) from leave_usages
        where employee_id = ? and leave_date between ? and ?
        """,
        (employee_id, start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    planned = db.execute(
        """
        select coalesce(sum(days), 0) from leave_usages
        where employee_id = ? and leave_kind = 'planned' and leave_date between ? and ?
        """,
        (employee_id, start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    total_used_to_end = db.execute(
        """
        select coalesce(sum(days), 0) from leave_usages
        where employee_id = ? and leave_date <= ?
        """,
        (employee_id, end.isoformat()),
    ).fetchone()[0]
    total_granted_to_end = db.execute(
        """
        select coalesce(sum(days), 0) from leave_grants
        where employee_id = ? and grant_date <= ? and expires_on >= ?
        """,
        (employee_id, end.isoformat(), end.isoformat()),
    ).fetchone()[0]
    granted_at_year_end = min(float(total_granted_to_end), 40.0)
    remaining_at_year_end = min(remaining_days_on(db, employee_id, end), 40.0)
    statutory_target = 5 if yearly_grant_days(db, employee_id, year) >= 10 else 0
    latest_grant_date = db.execute(
        'select max(grant_date) from leave_grants where employee_id = ? and grant_date <= ?',
        (employee_id, balance_on.isoformat()),
    ).fetchone()[0]
    return {
        'remaining_current': min(remaining_days_on(db, employee_id, balance_on), 40.0),
        'latest_grant_date': latest_grant_date or '-',
        "employee": employee,
        "granted_available_year": min(float(granted_available), 40.0),
        "used_year": float(used),
        "planned_year": float(planned),
        "remaining_at_year_end": remaining_at_year_end,
        "statutory_target": statutory_target,
        "target_gap": max(statutory_target - float(used), 0.0),
    }


def yearly_grant_days(db: sqlite3.Connection, employee_id: int, year: int) -> float:
    return float(
        db.execute(
            "select coalesce(sum(days), 0) from leave_grants where employee_id = ? and strftime('%Y', grant_date) = ?",
            (employee_id, str(year)),
        ).fetchone()[0]
    )


def available_days_on(db: sqlite3.Connection, employee_id: int, target_date: date) -> float:
    return remaining_days_on(db, employee_id, target_date)


def remaining_days_on(db: sqlite3.Connection, employee_id: int, target_date: date) -> float:
    grants = [
        {
            'grant_date': parse_date(row['grant_date']),
            'expires_on': parse_date(row['expires_on']),
            'remaining': float(row['days']),
        }
        for row in db.execute(
            '''
            select grant_date, expires_on, days from leave_grants
            where employee_id = ? and grant_date <= ?
            order by expires_on, grant_date, id
            ''',
            (employee_id, target_date.isoformat()),
        ).fetchall()
    ]
    usages = db.execute(
        '''
        select leave_date, days from leave_usages
        where employee_id = ? and leave_date <= ?
        order by leave_date, id
        ''',
        (employee_id, target_date.isoformat()),
    ).fetchall()

    for usage in usages:
        leave_date = parse_date(usage['leave_date'])
        days_to_apply = float(usage['days'])
        for grant in grants:
            if days_to_apply <= 0:
                break
            if not (grant['grant_date'] <= leave_date <= grant['expires_on']):
                continue
            applied = min(grant['remaining'], days_to_apply)
            grant['remaining'] -= applied
            days_to_apply -= applied

    return max(
        sum(
            grant['remaining']
            for grant in grants
            if grant['grant_date'] <= target_date <= grant['expires_on']
        ),
        0.0,
    )


def legacy_available_days_on(db: sqlite3.Connection, employee_id: int, target_date: date) -> float:
    granted = float(
        db.execute(
            """
            select coalesce(sum(days), 0) from leave_grants
            where employee_id = ? and grant_date <= ? and expires_on >= ?
            """,
            (employee_id, target_date.isoformat(), target_date.isoformat()),
        ).fetchone()[0]
    )
    used = float(
        db.execute(
            """
            select coalesce(sum(days), 0) from leave_usages
            where employee_id = ? and leave_date <= ?
            """,
            (employee_id, target_date.isoformat()),
        ).fetchone()[0]
    )
    return max(granted - used, 0.0)


def prorated_days(employee: sqlite3.Row, statutory_days: int) -> int:
    weekly_days = float(employee["weekly_work_days"] or 5)
    weekly_hours = float(employee["weekly_work_hours"] or 40)
    annual_days = employee["scheduled_annual_work_days"]
    if weekly_days >= 5 or weekly_hours >= 30:
        return statutory_days
    if weekly_days >= 4 or (annual_days and annual_days >= 169):
        table = [7, 8, 9, 10, 12, 13, 15]
    elif weekly_days >= 3 or (annual_days and annual_days >= 121):
        table = [5, 6, 6, 8, 9, 10, 11]
    elif weekly_days >= 2 or (annual_days and annual_days >= 73):
        table = [3, 4, 4, 5, 6, 6, 7]
    else:
        table = [1, 2, 2, 2, 3, 3, 3]
    index = min(STATUTORY_GRANT_DAYS.index(next(rule for rule in STATUTORY_GRANT_DAYS if rule[1] == statutory_days)), 6)
    return table[index]


def add_months(base: date, months: int) -> date:
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, days_in_month(year, month))
    return date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
