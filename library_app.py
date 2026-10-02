# -*- coding: utf-8 -*-
"""
Основная логика библиотеки: книги, читатели, выдача и возврат.
"""
import os
import sys
import sqlite3
import json
import urllib.parse
import urllib.request
import uuid
import threading
from datetime import date, datetime, timezone

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
except ModuleNotFoundError:
    class _TkUnavailable:
        Tk = object

    tk = _TkUnavailable()
    ttk = messagebox = simpledialog = None

from google_sheets_sync import DEFAULT_API_URL, GoogleSheetsSyncError, fetch_books, save_books, fetch_readers, save_readers

try:
    import cv2
    from PIL import Image, ImageTk
except Exception:
    cv2 = None
    Image = ImageTk = None

try:
    from supabase import create_client
except Exception:
    create_client = None

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
DEFAULT_DB = os.path.join(APP_DIR, "library.db")
ADMIN_PASSWORD = os.getenv("LIBRARY_ADMIN_PASSWORD", "admin123")
SYNC_INTERVAL_MS = max(60_000, int(os.getenv("GOOGLE_SYNC_INTERVAL_SECONDS", "60")) * 1000)
SYNC_CONFIG_FILE = os.path.join(
    os.getenv("APPDATA", os.path.expanduser("~")), "LibraryApp", "google_sheets_url.txt"
)

# Необязательный путь к базе данных в облачной папке:
# укажите LIBRARY_DB_PATH как путь к папке, синхронизируемой через OneDrive, Dropbox или Google Drive.
# Пример: set LIBRARY_DB_PATH=C:\Users\You\OneDrive\library\library.db
cloud_db_path = os.getenv("LIBRARY_DB_PATH")
if cloud_db_path:
    cloud_db_path = os.path.abspath(cloud_db_path)
    cloud_dir = os.path.dirname(cloud_db_path)
    if cloud_dir and not os.path.exists(cloud_dir):
        os.makedirs(cloud_dir, exist_ok=True)
    DB = cloud_db_path
else:
    DB = DEFAULT_DB

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY")
# Тестовая или демонстрационная база Supabase. Если переменные не заданы, используется SQLite.
# Для смены аккаунта задайте эти переменные заново с новым URL и ключом проекта.
SUPABASE_CLIENT = None
if create_client and SUPABASE_URL and SUPABASE_KEY:
    try:
        SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        SUPABASE_CLIENT = None

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS books(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        author TEXT NOT NULL,
        year INTEGER,
        status TEXT NOT NULL DEFAULT 'в наличии',  -- 'в наличии' / 'выдана'
        reader_id TEXT,
        reader_name TEXT,
        issue_date TEXT,
        due_date TEXT,
        return_date TEXT
    );
    CREATE TABLE IF NOT EXISTS readers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT
    );
    CREATE TABLE IF NOT EXISTS loans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL REFERENCES books(id),
        reader_id INTEGER NOT NULL REFERENCES readers(id),
        issue_date TEXT NOT NULL,
        due_date TEXT,
        return_date TEXT            -- NULL = книга ещё у читателя
    );
    """)
    con.commit()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(books)")}
    if "isbn" not in columns:
        con.execute("ALTER TABLE books ADD COLUMN isbn TEXT")
    if "inventory_id" not in columns:
        con.execute("ALTER TABLE books ADD COLUMN inventory_id TEXT")
    if "updated_at" not in columns:
        con.execute("ALTER TABLE books ADD COLUMN updated_at TEXT")
    for column in ("reader_id", "reader_name", "issue_date", "due_date", "return_date"):
        if column not in columns:
            con.execute(f"ALTER TABLE books ADD COLUMN {column} TEXT")
    for column in ("description", "language", "publisher", "categories", "cover_url", "info_url"):
        if column not in columns:
            con.execute(f"ALTER TABLE books ADD COLUMN {column} TEXT")
    con.execute("DROP TABLE IF EXISTS deleted_books")
    now = utc_now()
    for row in con.execute("SELECT id FROM books WHERE inventory_id IS NULL OR inventory_id='' OR updated_at IS NULL"):
        con.execute("UPDATE books SET inventory_id=?, updated_at=? WHERE id=?",
                    (str(uuid.uuid4()), now, row["id"]))
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_books_isbn ON books(isbn) WHERE isbn IS NOT NULL")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_books_inventory_id ON books(inventory_id)")
    reader_columns = {row["name"] for row in con.execute("PRAGMA table_info(readers)")}
    if "updated_at" not in reader_columns:
        con.execute("ALTER TABLE readers ADD COLUMN updated_at TEXT")
    for row in con.execute("SELECT id FROM readers WHERE updated_at IS NULL OR updated_at='' "):
        con.execute("UPDATE readers SET updated_at=? WHERE id=?", (now, row["id"]))
    con.commit()
    con.close()

# ---------- Логика ----------
def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def newer_than(left, right):
    return str(left or "") > str(right or "")


def normalize_isbn(value):
    isbn = "".join(ch for ch in str(value or "").upper() if ch.isdigit() or ch == "X")
    if len(isbn) == 10:
        if not (isbn[:9].isdigit() and (isbn[9].isdigit() or isbn[9] == "X")):
            raise ValueError("ISBN-10 должен содержать 9 цифр и контрольный символ.")
        total = sum((10 - index) * int(symbol) for index, symbol in enumerate(isbn[:9]))
        total += 10 if isbn[9] == "X" else int(isbn[9])
        if total % 11:
            raise ValueError("Неверная контрольная сумма ISBN-10.")
        isbn = "978" + isbn[:9]
        isbn += str((10 - sum((index + 1) * int(symbol) for index, symbol in enumerate(isbn)) % 10) % 10)
    elif len(isbn) == 13:
        if not isbn.isdigit() or isbn[:3] not in ("978", "979"):
            raise ValueError("ISBN-13 должен начинаться с 978 или 979.")
        if sum((1 if index % 2 == 0 else 3) * int(symbol) for index, symbol in enumerate(isbn)) % 10:
            raise ValueError("Неверная контрольная сумма ISBN-13.")
    elif isbn:
        raise ValueError("ISBN должен содержать 10 или 13 цифр.")
    return isbn or None


def book_info_from_isbn(isbn):
    normalized = normalize_isbn(isbn)
    if not normalized:
        raise ValueError("Введите ISBN.")
    encoded_isbn = urllib.parse.quote(normalized)
    urls = (
        "https://www.googleapis.com/books/v1/volumes?q=isbn:%s&maxResults=1" % encoded_isbn,
        "https://openlibrary.org/isbn/%s.json" % encoded_isbn,
        "https://openlibrary.org/api/books?bibkeys=ISBN:%s&format=json&jscmd=data" % encoded_isbn,
    )
    last_error = None
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LibraryApp/1.0"})
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.load(response)
        except Exception as exc:
            last_error = exc
            continue

        if "googleapis.com" in url:
            items = payload.get("items") or []
            book = items[0].get("volumeInfo", {}) if items else {}
            authors = book.get("authors") or []
            published = str(book.get("publishedDate") or "")
            if book.get("title"):
                return {"isbn": normalized, "title": book["title"].strip(),
                        "author": str(authors[0] if authors else "").strip(),
                        "year": published[:4] or None,
                        "description": str(book.get("description") or "").strip(),
                        "language": str(book.get("language") or "").strip(),
                        "publisher": str(book.get("publisher") or "").strip(),
                        "categories": ", ".join(str(item) for item in (book.get("categories") or [])),
                        "cover_url": str((book.get("imageLinks") or {}).get("thumbnail") or "").strip(),
                        "info_url": str(book.get("infoLink") or "").strip()}
        elif "/isbn/" in url:
            title = str(payload.get("title") or "").strip()
            if title:
                author = ""
                author_ref = (payload.get("authors") or [{}])[0].get("key")
                if author_ref:
                    try:
                        author_url = "https://openlibrary.org%s.json" % author_ref
                        author_request = urllib.request.Request(
                            author_url, headers={"User-Agent": "LibraryApp/1.0"}
                        )
                        with urllib.request.urlopen(author_request, timeout=8) as author_response:
                            author = str(json.load(author_response).get("name") or "").strip()
                    except Exception:
                        pass
                return {"isbn": normalized, "title": title, "author": author,
                    "year": str(payload.get("publish_date") or "")[:4] or None,
                    "description": str((payload.get("description") or {}).get("value") if isinstance(payload.get("description"), dict) else payload.get("description") or "").strip(),
                    "language": ", ".join(str(item.get("key", "")).split("/")[-1] for item in (payload.get("languages") or []) if isinstance(item, dict)),
                    "publisher": str((payload.get("publishers") or [""])[0]).strip(),
                    "categories": ", ".join(str(item) for item in (payload.get("subjects") or [])[:10]),
                    "cover_url": "https://covers.openlibrary.org/b/id/%s-M.jpg" % payload["covers"][0] if payload.get("covers") else "",
                    "info_url": "https://openlibrary.org/isbn/%s" % normalized}
        else:
            book = payload.get("ISBN:" + normalized)
            if book:
                authors = book.get("authors") or []
                return {"isbn": normalized, "title": (book.get("title") or "").strip(),
                        "author": (authors[0].get("name") if authors else "").strip(),
                    "year": book.get("publish_date", "")[:4] if book.get("publish_date") else None,
                    "description": str(book.get("notes") or "").strip(),
                    "language": "", "publisher": "", "categories": "",
                    "cover_url": str((book.get("cover") or {}).get("medium") or "").strip(),
                    "info_url": "https://openlibrary.org/isbn/%s" % normalized}

    if last_error:
        raise ValueError("Не удалось получить данные по ISBN. Проверьте интернет.") from last_error
    raise ValueError("Книга по этому ISBN не найдена.")


def add_book(title, author, year, isbn=None, metadata=None):
    isbn = normalize_isbn(isbn)
    metadata = metadata or {}
    con = db()
    try:
        con.execute("""
            INSERT INTO books(title,author,year,isbn,inventory_id,updated_at,description,language,publisher,categories,cover_url,info_url)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (title.strip(), author.strip(), year or None, isbn, str(uuid.uuid4()), utc_now(),
               str(metadata.get("description") or "").strip(), str(metadata.get("language") or "").strip(),
               str(metadata.get("publisher") or "").strip(), str(metadata.get("categories") or "").strip(),
               str(metadata.get("cover_url") or "").strip(), str(metadata.get("info_url") or "").strip()))
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise ValueError("Книга с таким ISBN уже есть в библиотеке.") from exc
    finally:
        con.close()


def update_book(book_id, title, author, year):
    if not title.strip() or not author.strip():
        raise ValueError("Нужны название и автор.")
    con = db()
    con.execute("UPDATE books SET title=?, author=?, year=?, updated_at=? WHERE id=?",
                (title.strip(), author.strip(), year or None, utc_now(), book_id))
    con.commit()
    con.close()


def add_reader(name, phone):
    name = name.strip()
    phone = phone.strip()
    if not name:
        raise ValueError("Введите ФИО читателя.")
    if not phone:
        raise ValueError("Введите номер телефона или другой контакт.")
    con = db()
    con.execute("INSERT INTO readers(name,phone,updated_at) VALUES(?,?,?)",
                (name, phone, utc_now()))
    con.commit(); con.close()


def sync_readers_with_google():
    """Сохранить локальный список читателей в Google Sheets."""
    init_db()
    con = db()
    try:
        records = _local_reader_records(con)
    finally:
        con.close()
    return save_readers(records)


def issue_book(book_id, reader_id, due_date):
    con = db()
    row = con.execute("SELECT status FROM books WHERE id=?", (book_id,)).fetchone()
    if not row: con.close(); return "Книга не найдена."
    if row["status"] == "выдана":
        con.close(); return "Эта книга уже выдана."
    reader = con.execute("SELECT name FROM readers WHERE id=?", (reader_id,)).fetchone()
    if not reader:
        con.close(); return "Читатель не найден."
    today = date.today().isoformat()
    con.execute("INSERT INTO loans(book_id,reader_id,issue_date,due_date) VALUES(?,?,?,?)",
                (book_id, reader_id, today, due_date or None))
    con.execute(
        "UPDATE books SET status='выдана', reader_id=?, reader_name=?, issue_date=?, due_date=?, return_date=NULL, updated_at=? WHERE id=?",
        (str(reader_id), reader["name"], today, due_date or None, utc_now(), book_id),
    )
    con.commit(); con.close()
    return None

def return_book(loan_id, book_id):
    con = db()
    con.execute("UPDATE loans SET return_date=? WHERE id=?",
                (date.today().isoformat(), loan_id))
    con.execute(
        "UPDATE books SET status='в наличии', reader_id=NULL, reader_name=NULL, issue_date=NULL, due_date=NULL, return_date=?, updated_at=? WHERE id=?",
        (date.today().isoformat(), utc_now(), book_id),
    )
    con.commit(); con.close()


def delete_book(book_id):
    con = db()
    con.execute("DELETE FROM loans WHERE book_id=?", (book_id,))
    con.execute("DELETE FROM books WHERE id=?", (book_id,))
    con.commit(); con.close()

def books_list(filter_status=None, search=""):
    sql = "SELECT * FROM books WHERE 1=1"
    params = []
    if filter_status in ("в наличии", "выдана"):
        sql += " AND status=?"; params.append(filter_status)
    if search:
        sql += " AND (title LIKE ? OR author LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY title"
    con = db()
    rows = con.execute(sql, params).fetchall(); con.close()
    return rows

def readers_list(search=""):
    sql = "SELECT * FROM readers"
    params = []
    if search:
        sql += " WHERE name LIKE ?"; params.append(f"%{search}%")
    sql += " ORDER BY name"
    con = db()
    rows = con.execute(sql, params).fetchall(); con.close()
    return rows

def active_loans():
    con = db()
    rows = con.execute("""
        SELECT l.id AS loan_id, l.issue_date, l.due_date, l.return_date,
               b.id AS book_id, b.title, b.author,
               r.name AS reader, r.phone
        FROM loans l
        JOIN books b ON b.id = l.book_id
        JOIN readers r ON r.id = l.reader_id
        WHERE l.return_date IS NULL
        ORDER BY l.issue_date
    """).fetchall(); con.close()
    return rows

def history():
    con = db()
    rows = con.execute("""
        SELECT l.issue_date, l.due_date, l.return_date,
               b.title, b.author, r.name AS reader
        FROM loans l
        JOIN books b ON b.id = l.book_id
        JOIN readers r ON r.id = l.reader_id
        WHERE l.return_date IS NOT NULL
        ORDER BY l.return_date DESC
    """).fetchall(); con.close()
    return rows

def overdue():
    today = date.today().isoformat()
    return [r for r in active_loans() if r["due_date"] and r["due_date"] < today]


def _local_sync_records(con):
    records = []
    for row in con.execute(
        "SELECT inventory_id,title,author,year,isbn,status,updated_at,reader_id,reader_name,issue_date,due_date,return_date FROM books"
    ):
        if not str(row["title"] or "").strip() or not str(row["author"] or "").strip():
            continue
        records.append({key: row[key] for key in row.keys()})
    return records


def _local_reader_records(con):
    return [{"id": row["id"], "name": row["name"], "phone": row["phone"] or "", "updated_at": row["updated_at"]}
            for row in con.execute("SELECT id,name,phone,updated_at FROM readers")]


def _validate_reader_records(records):
    validated = {}
    for record in records:
        reader_id = str(record.get("id") or "").strip()
        if not reader_id:
            raise ValueError("У читателя отсутствует id.")
        if reader_id in validated:
            raise ValueError(f"В Google Sheets найден дубликат читателя: {reader_id}")
        name = str(record.get("name") or "").strip()
        if not name:
            raise ValueError(f"У читателя {reader_id} отсутствует имя.")
        validated[reader_id] = {"id": reader_id, "name": name,
                                "phone": str(record.get("phone") or "").strip(),
                                "updated_at": str(record.get("updated_at") or utc_now()).strip()}
    return validated


def _validate_sync_records(records):
    validated = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Google Sheets содержит запись неправильного типа.")
        inventory_id = str(record.get("inventory_id") or "").strip()
        updated_at = str(record.get("updated_at") or "").strip()
        if not inventory_id or not updated_at:
            raise ValueError("У записи Google Sheets отсутствует inventory_id или updated_at.")
        if inventory_id in validated:
            raise ValueError(f"В Google Sheets найден дубликат inventory_id: {inventory_id}")
        deleted = str(record.get("deleted", "false")).strip().lower() in ("true", "1", "yes", "да")
        if deleted:
            validated[inventory_id] = {
                "inventory_id": inventory_id, "updated_at": updated_at, "deleted": True,
            }
            continue
        title = str(record.get("title") or "").strip()
        author = str(record.get("author") or "").strip()
        if not title or not author:
            raise ValueError(f"У книги {inventory_id} отсутствует название или автор.")
        status = str(record.get("status") or "\u0432 \u043d\u0430\u043b\u0438\u0447\u0438\u0438").strip().lower()
        if status == "\u0432\u044b\u0434\u0430\u043d":
            status = "\u0432\u044b\u0434\u0430\u043d\u0430"
        if status not in ("\u0432 \u043d\u0430\u043b\u0438\u0447\u0438\u0438", "\u0432\u044b\u0434\u0430\u043d\u0430"):
            raise ValueError(f"У книги {inventory_id} некорректный статус.")
        try:
            isbn = normalize_isbn(record.get("isbn"))
        except ValueError:
            isbn = None
        try:
            year = int(record["year"]) if record.get("year") not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"У книги {inventory_id} некорректный год.") from exc
        validated[inventory_id] = {
            "inventory_id": inventory_id, "title": title, "author": author,
            "year": year, "isbn": isbn, "status": status,
            "updated_at": updated_at,
            "reader_id": str(record.get("reader_id") or "").strip() or None,
            "reader_name": str(record.get("reader_name") or "").strip() or None,
            "issue_date": str(record.get("issue_date") or "").strip() or None,
            "due_date": str(record.get("due_date") or "").strip() or None,
            "return_date": str(record.get("return_date") or "").strip() or None,
            "deleted": False,
        }
    return validated


def sync_books_with_google():
    """Синхронизировать локальные книги и Google Sheets по updated_at."""
    init_db()
    remote = _validate_sync_records(fetch_books())
    remote_readers = _validate_reader_records(fetch_readers())
    con = db()
    try:
        local = _validate_sync_records(_local_sync_records(con))
        remote = {
            inventory_id: record
            for inventory_id, record in remote.items()
            if not record.get("deleted")
        }
        local_readers = _validate_reader_records(_local_reader_records(con))
        merged = dict(local)
        for inventory_id, remote_record in remote.items():
            local_record = local.get(inventory_id)
            if local_record is None or newer_than(remote_record["updated_at"], local_record["updated_at"]):
                if local_record is not None:
                    remote_record["reader_id"] = local_record.get("reader_id")
                    remote_record["reader_name"] = local_record.get("reader_name")
                merged[inventory_id] = remote_record

        for record in merged.values():
            inventory_id = record["inventory_id"]
            if record.get("deleted"):
                con.execute("DELETE FROM books WHERE inventory_id=?", (inventory_id,))
                continue
            existing = con.execute("SELECT id FROM books WHERE inventory_id=?", (inventory_id,)).fetchone()
            values = (
                record["title"], record["author"], record["year"], record["isbn"],
                record["status"], record["updated_at"],
                record.get("reader_id"), record.get("reader_name"),
                record.get("issue_date"), record.get("due_date"), record.get("return_date"),
            )
            if existing:
                con.execute("""
                    UPDATE books SET title=?,author=?,year=?,isbn=?,status=?,updated_at=?,
                    reader_id=?,reader_name=?,issue_date=?,due_date=?,return_date=?
                    WHERE inventory_id=?
                """, values + (inventory_id,))
            else:
                con.execute("""
                    INSERT INTO books(title,author,year,isbn,status,inventory_id,updated_at,
                    reader_id,reader_name,issue_date,due_date,return_date)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    record["title"], record["author"], record["year"], record["isbn"],
                    record["status"], inventory_id, record["updated_at"],
                    record.get("reader_id"), record.get("reader_name"),
                    record.get("issue_date"), record.get("due_date"), record.get("return_date"),
                ))
        merged_readers = dict(local_readers)
        for reader_id, remote_reader in remote_readers.items():
            local_reader = local_readers.get(reader_id)
            if local_reader is None or newer_than(remote_reader["updated_at"], local_reader["updated_at"]):
                merged_readers[reader_id] = remote_reader
        for reader in merged_readers.values():
            con.execute("""
                INSERT INTO readers(id,name,phone,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                updated_at=excluded.updated_at
            """, (reader["id"], reader["name"], reader["phone"], reader["updated_at"]))
        con.commit()
        final_records = _local_sync_records(con)
        final_readers = _local_reader_records(con)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    try:
        save_books(final_records)
        save_readers(final_readers)
    except GoogleSheetsSyncError:
        raise
    return len(final_records)

# ---------- Интерфейс ----------
class LibraryApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Частная библиотека")
        self.geometry("900x550")

        storage_text = "Хранилище: облако" if os.getenv("LIBRARY_DB_PATH") else "Хранилище: локальная база"
        self.storage_label = ttk.Label(self, text=f"{storage_text} — {DB}")
        self.storage_label.pack(fill="x", padx=8, pady=(8, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_books = ttk.Frame(nb);      nb.add(self.tab_books, text="  Книги  ")
        self.tab_readers = ttk.Frame(nb);    nb.add(self.tab_readers, text="  Читатели  ")
        self.tab_issue = ttk.Frame(nb);      nb.add(self.tab_issue, text="  Выдать / Вернуть  ")
        self.tab_history = ttk.Frame(nb);    nb.add(self.tab_history, text="  История  ")
        self.tab_admin = ttk.Frame(nb);      nb.add(self.tab_admin, text="  Админ  ")

        self.build_books()
        self.build_readers()
        self.build_issue()
        self.build_history()
        self.build_admin()
        self.refresh_all()
        self.sync_running = False
        self.after(SYNC_INTERVAL_MS, self.auto_sync)
        self.after(500, lambda: self.start_sync(show_result=True))

    # ----- Книги -----
    def build_books(self):
        f = self.tab_books
        top = ttk.Frame(f); top.pack(fill="x", pady=5)
        ttk.Button(top, text="Добавить книгу", command=self.open_add_book_dialog).pack(side="left", padx=8)
        ttk.Button(top, text="Редактировать выбранную", command=self.on_edit_book).pack(side="left", padx=8)

        fil = ttk.Frame(f); fil.pack(fill="x", pady=3)
        ttk.Label(fil, text="Поиск:").pack(side="left")
        self.b_search = ttk.Entry(fil, width=25); self.b_search.pack(side="left", padx=3)
        self.b_search.bind("<KeyRelease>", lambda e: self.refresh_books())
        ttk.Label(fil, text=" Показать:").pack(side="left")
        self.b_filter = ttk.Combobox(fil, state="readonly", width=12,
                                     values=["все", "в наличии", "выдана"])
        self.b_filter.set("все"); self.b_filter.pack(side="left", padx=3)
        self.b_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_books())

        cols = ("id", "title", "author", "year", "status")
        self.books_tv = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for c, t, w in zip(cols, ["ID", "Название", "Автор", "Год", "Статус"],
                           [40, 320, 220, 60, 100]):
            self.books_tv.heading(c, text=t); self.books_tv.column(c, width=w)
        self.books_tv.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.books_tv.yview)
        self.books_tv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")

    def open_add_book_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Добавить книгу")
        dlg.geometry("380x210")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        form = ttk.Frame(dlg, padding=12)
        form.pack(fill="both", expand=True)

        ttk.Label(form, text="Название:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        title_entry = ttk.Entry(form, width=30)
        title_entry.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(form, text="Автор:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        author_entry = ttk.Entry(form, width=30)
        author_entry.grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(form, text="Год:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        year_entry = ttk.Entry(form, width=12)
        year_entry.grid(row=2, column=1, sticky="w", padx=5, pady=5)

        btns = ttk.Frame(form)
        btns.grid(row=3, column=0, columnspan=2, pady=(12, 0))

        def save_book():
            t = title_entry.get().strip()
            a = author_entry.get().strip()
            if not t or not a:
                messagebox.showwarning("Библиотека", "Нужны название и автор.", parent=dlg)
                return

            y = year_entry.get().strip()
            try:
                y = int(y) if y else None
            except ValueError:
                messagebox.showwarning("Библиотека", "Год должен быть числом.", parent=dlg)
                return

            add_book(t, a, y)
            dlg.destroy()
            self.refresh_all()
            self.start_sync()

        ttk.Button(btns, text="Сохранить", command=save_book).pack(side="left", padx=5)
        ttk.Button(btns, text="Отмена", command=dlg.destroy).pack(side="left", padx=5)

    def on_del_book(self):
        sel = self.books_tv.selection()
        if not sel: return
        bid = self.books_tv.item(sel[0])["values"][0]
        con = db()
        busy = con.execute("SELECT id FROM loans WHERE book_id=? AND return_date IS NULL",
                           (bid,)).fetchone()
        con.close()
        if busy:
            messagebox.showwarning("Библиотека", "Книга выдана — сначала верните её."); return
        if messagebox.askyesno("Библиотека", "Удалить книгу из базы?"):
            delete_book(bid)
            self.refresh_all()

    def on_edit_book(self):
        sel = self.books_tv.selection()
        if not sel:
            messagebox.showwarning("Библиотека", "Выберите книгу.", parent=self)
            return
        book_id = self.books_tv.item(sel[0])["values"][0]
        con = db()
        book = con.execute("SELECT title, author, year FROM books WHERE id=?", (book_id,)).fetchone()
        con.close()
        if not book:
            return

        dlg = tk.Toplevel(self)
        dlg.title("Редактировать книгу")
        dlg.geometry("380x210")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        form = ttk.Frame(dlg, padding=12)
        form.pack(fill="both", expand=True)

        ttk.Label(form, text="Название:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        title_entry = ttk.Entry(form, width=30)
        title_entry.insert(0, book["title"])
        title_entry.grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(form, text="Автор:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        author_entry = ttk.Entry(form, width=30)
        author_entry.insert(0, book["author"])
        author_entry.grid(row=1, column=1, padx=5, pady=5)
        ttk.Label(form, text="Год:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        year_entry = ttk.Entry(form, width=12)
        year_entry.insert(0, book["year"] or "")
        year_entry.grid(row=2, column=1, sticky="w", padx=5, pady=5)

        def save_changes():
            try:
                year = int(year_entry.get().strip()) if year_entry.get().strip() else None
                update_book(book_id, title_entry.get(), author_entry.get(), year)
            except ValueError as exc:
                messagebox.showwarning("Библиотека", str(exc), parent=dlg)
                return
            dlg.destroy()
            self.refresh_all()
            self.start_sync()

        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(buttons, text="Сохранить", command=save_changes).pack(side="left", padx=5)
        ttk.Button(buttons, text="Отмена", command=dlg.destroy).pack(side="left", padx=5)
        title_entry.focus_set()

    def refresh_books(self):
        self.books_tv.delete(*self.books_tv.get_children())
        fl = self.b_filter.get()
        fl = fl if fl != "все" else None
        for r in books_list(fl, self.b_search.get().strip()):
            self.books_tv.insert("", "end", values=(
                r["id"], r["title"], r["author"], r["year"] or "", r["status"]))

    # ----- Читатели -----
    def build_readers(self):
        f = self.tab_readers
        top = ttk.Frame(f); top.pack(fill="x", pady=5)
        ttk.Label(top, text="Имя:").pack(side="left")
        self.r_name = ttk.Entry(top, width=25); self.r_name.pack(side="left", padx=3)
        ttk.Label(top, text="Телефон:").pack(side="left")
        self.r_phone = ttk.Entry(top, width=15); self.r_phone.pack(side="left", padx=3)
        ttk.Button(top, text="Добавить читателя", command=self.on_add_reader).pack(side="left", padx=8)

        fil = ttk.Frame(f); fil.pack(fill="x", pady=3)
        ttk.Label(fil, text="Поиск:").pack(side="left")
        self.r_search = ttk.Entry(fil, width=25); self.r_search.pack(side="left", padx=3)
        self.r_search.bind("<KeyRelease>", lambda e: self.refresh_readers())

        cols = ("id", "name", "phone")
        self.readers_tv = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for c, t, w in zip(cols, ["ID", "Имя", "Телефон"], [40, 400, 200]):
            self.readers_tv.heading(c, text=t); self.readers_tv.column(c, width=w)
        self.readers_tv.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.readers_tv.yview)
        self.readers_tv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")

    def on_add_reader(self):
        n = self.r_name.get().strip()
        if not n:
            messagebox.showwarning("Библиотека", "Введите имя."); return
        add_reader(n, self.r_phone.get())
        self.r_name.delete(0, "end"); self.r_phone.delete(0, "end")
        self.refresh_all()

    def on_del_reader(self):
        sel = self.readers_tv.selection()
        if not sel: return
        rid = self.readers_tv.item(sel[0])["values"][0]
        con = db()
        busy = con.execute("SELECT id FROM loans WHERE reader_id=? AND return_date IS NULL",
                           (rid,)).fetchone()
        con.close()
        if busy:
            messagebox.showwarning("Библиотека", "У читателя есть невозвращённые книги."); return
        if messagebox.askyesno("Библиотека", "Удалить читателя?"):
            con = db()
            con.execute("DELETE FROM loans WHERE reader_id=?", (rid,))
            con.execute("DELETE FROM readers WHERE id=?", (rid,))
            con.commit(); con.close()
            self.refresh_all()

    def refresh_readers(self):
        self.readers_tv.delete(*self.readers_tv.get_children())
        for r in readers_list(self.r_search.get().strip()):
            self.readers_tv.insert("", "end", values=(r["id"], r["name"], r["phone"] or ""))

    # ----- Выдача / возврат -----
    def build_issue(self):
        f = self.tab_issue
        ttk.Label(f, text="ВЫДАТЬ КНИГУ", font=("", 11, "bold")).pack(anchor="w", pady=(5, 2))
        g1 = ttk.Frame(f); g1.pack(fill="x")
        ttk.Label(g1, text="Книга (в наличии):").pack(side="left")
        self.i_book = ttk.Combobox(g1, state="readonly", width=45); self.i_book.pack(side="left", padx=5)
        ttk.Label(g1, text="Читатель:").pack(side="left")
        self.i_reader = ttk.Combobox(g1, state="readonly", width=30); self.i_reader.pack(side="left", padx=5)

        g2 = ttk.Frame(f); g2.pack(fill="x", pady=4)
        ttk.Label(g2, text="Вернуть до (ГГГГ-ММ-ДД):").pack(side="left")
        self.i_due = ttk.Entry(g2, width=12); self.i_due.pack(side="left", padx=5)
        ttk.Button(g2, text="Выдать", command=self.on_issue).pack(side="left", padx=10)

        ttk.Separator(f).pack(fill="x", pady=6)
        ttk.Label(f, text="ВЫДАННЫЕ СЕЙЧАС (двойной клик — вернуть книгу)",
                  font=("", 11, "bold")).pack(anchor="w")
        cols = ("loan_id", "book_id", "title", "reader", "phone", "issue", "due")
        self.loans_tv = ttk.Treeview(f, columns=cols, show="headings", height=14)
        heads = ["№ выдачи", "ID книги", "Книга", "Читатель", "Телефон", "Выдана", "Вернуть до"]
        widths = [70, 60, 250, 160, 110, 90, 90]
        for c, t, w in zip(cols, heads, widths):
            self.loans_tv.heading(c, text=t); self.loans_tv.column(c, width=w)
        self.loans_tv.pack(fill="both", expand=True, side="left", pady=3)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.loans_tv.yview)
        self.loans_tv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")
        self.loans_tv.bind("<Double-1>", self.on_return)

        self.overdue_lbl = ttk.Label(f, foreground="red")
        self.overdue_lbl.pack(anchor="w", pady=2)

    def on_issue(self):
        book_txt = self.i_book.get()
        reader_txt = self.i_reader.get()
        if not book_txt or not reader_txt:
            messagebox.showwarning("Библиотека", "Выберите книгу и читателя."); return
        bid = int(book_txt.split(".")[0])
        rid = int(reader_txt.split(".")[0])
        due = self.i_due.get().strip()
        if due:
            try:
                datetime.strptime(due, "%Y-%m-%d")
            except ValueError:
                messagebox.showwarning("Библиотека", "Дата должна быть в формате ГГГГ-ММ-ДД."); return
        err = issue_book(bid, rid, due)
        if err:
            messagebox.showwarning("Библиотека", err); return
        self.i_due.delete(0, "end")
        self.refresh_all()

    def on_return(self, _event=None):
        sel = self.loans_tv.selection()
        if not sel: return
        v = self.loans_tv.item(sel[0])["values"]
        if messagebox.askyesno("Библиотека", f"Вернуть книгу «{v[2]}» от {v[3]}?"):
            return_book(v[0], v[1])
            self.refresh_all()

    def refresh_issue(self):
        # Заполнить выпадающие списки.
        avail = books_list("в наличии")
        self.i_book["values"] = [f'{r["id"]}. {r["title"]} — {r["author"]}' for r in avail]
        readers = readers_list()
        self.i_reader["values"] = [f'{r["id"]}. {r["name"]}' for r in readers]
        # Обновить таблицу выданных книг.
        self.loans_tv.delete(*self.loans_tv.get_children())
        for r in active_loans():
            self.loans_tv.insert("", "end", values=(
                r["loan_id"], r["book_id"], f'{r["title"]} — {r["author"]}',
                r["reader"], r["phone"] or "", r["issue_date"], r["due_date"] or "—"))
        od = overdue()
        if od:
            names = ", ".join(f'«{r["title"]}» ({r["reader"]})' for r in od)
            self.overdue_lbl.config(text=f"⚠ Просрочено: {names}")
        else:
            self.overdue_lbl.config(text="")

    # ----- История -----
    def build_history(self):
        f = self.tab_history
        cols = ("title", "reader", "issue", "due", "returned")
        self.hist_tv = ttk.Treeview(f, columns=cols, show="headings", height=22)
        for c, t, w in zip(cols, ["Книга", "Читатель", "Выдана", "Вернуть до", "Возвращена"],
                           [330, 200, 100, 100, 110]):
            self.hist_tv.heading(c, text=t); self.hist_tv.column(c, width=w)
        self.hist_tv.pack(fill="both", expand=True, side="left", pady=5)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.hist_tv.yview)
        self.hist_tv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y")

    def refresh_history(self):
        self.hist_tv.delete(*self.hist_tv.get_children())
        for r in history():
            self.hist_tv.insert("", "end", values=(
                f'{r["title"]} — {r["author"]}', r["reader"],
                r["issue_date"], r["due_date"] or "—", r["return_date"]))

    def build_admin(self):
        f = self.tab_admin
        top = ttk.Frame(f); top.pack(fill="x", pady=10)
        ttk.Label(top, text="Пароль админ-панели:").pack(side="left")
        self.admin_password_entry = ttk.Entry(top, width=20, show="*")
        self.admin_password_entry.pack(side="left", padx=5)
        ttk.Button(top, text="Открыть админ-панель", command=self.open_admin_panel).pack(side="left", padx=5)
        ttk.Button(top, text="Синхронизировать сейчас",
                   command=lambda: self.start_sync(show_result=True)).pack(side="left", padx=5)

    def open_admin_panel(self):
        password = self.admin_password_entry.get().strip() if self.admin_password_entry else ""
        if password != ADMIN_PASSWORD:
            messagebox.showwarning("Библиотека", "Неверный пароль админ-панели.")
            return

        panel = tk.Toplevel(self)
        panel.title("Админ-панель")
        panel.geometry("1050x560")
        panel.transient(self)
        panel.grab_set()

        ttk.Label(panel, text="Админ-панель: удаление записей", font=("", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
        interval_minutes = SYNC_INTERVAL_MS // 60
        ttk.Label(panel, text=f"Синхронизация с Google Sheets выполняется автоматически каждые {interval_minutes} мин.").pack(anchor="w", padx=10, pady=(4, 0))

        main = ttk.Frame(panel, padding=10)
        main.pack(fill="both", expand=True)

        isbn_frame = ttk.LabelFrame(main, text="Добавить книгу по ISBN", padding=8)
        isbn_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(isbn_frame, text="ISBN:").pack(side="left")
        isbn_entry = ttk.Entry(isbn_frame, width=24)
        isbn_entry.pack(side="left", padx=5)
        ttk.Label(isbn_frame, text="Название:").pack(side="left")
        title_entry = ttk.Entry(isbn_frame, width=28)
        title_entry.pack(side="left", padx=5)
        ttk.Label(isbn_frame, text="Автор:").pack(side="left")
        author_entry = ttk.Entry(isbn_frame, width=24)
        author_entry.pack(side="left", padx=5)
        ttk.Label(isbn_frame, text="Год:").pack(side="left")
        year_entry = ttk.Entry(isbn_frame, width=8)
        year_entry.pack(side="left", padx=5)
        isbn_message = ttk.Label(isbn_frame)
        isbn_message.pack(side="left", padx=5)
        book_metadata = {}

        def find_isbn_book():
            nonlocal book_metadata
            try:
                info = book_info_from_isbn(isbn_entry.get())
            except ValueError as exc:
                isbn_message.config(text=str(exc), foreground="red")
                return
            title_entry.delete(0, "end"); title_entry.insert(0, info["title"])
            author_entry.delete(0, "end"); author_entry.insert(0, info["author"])
            year_entry.delete(0, "end"); year_entry.insert(0, info["year"] or "")
            isbn_entry.delete(0, "end"); isbn_entry.insert(0, info["isbn"])
            book_metadata = info
            isbn_message.config(text="Данные найдены", foreground="green")

        def save_isbn_book():
            try:
                title = title_entry.get().strip()
                author = author_entry.get().strip()
                if not title or not author:
                    find_isbn_book()
                    title = title_entry.get().strip()
                    author = author_entry.get().strip()
                year = int(year_entry.get()) if year_entry.get().strip() else None
                add_book(title, author, year, isbn_entry.get(), metadata=book_metadata)
            except ValueError as exc:
                isbn_message.config(text=str(exc), foreground="red")
                return
            isbn_message.config(text="Книга добавлена", foreground="green")
            isbn_entry.delete(0, "end"); title_entry.delete(0, "end")
            author_entry.delete(0, "end"); year_entry.delete(0, "end")
            self.refresh_all(); self.refresh_admin_panel(); self.start_sync()

        ttk.Button(isbn_frame, text="Найти", command=find_isbn_book).pack(side="left", padx=3)
        ttk.Button(isbn_frame, text="Добавить", command=save_isbn_book).pack(side="left", padx=3)
        ttk.Button(isbn_frame, text="Камера", command=lambda: self.open_isbn_camera(isbn_entry, find_isbn_book)).pack(side="left", padx=3)
        isbn_entry.focus_set()
        isbn_entry.bind("<Return>", lambda _event: find_isbn_book())

        # Слева: книги.
        bframe = ttk.LabelFrame(main, text="Книги", padding=8)
        bframe.pack(side="left", fill="y", padx=(0, 8))
        self.admin_books_tv = ttk.Treeview(bframe, columns=("id", "title", "author", "year", "status"), show="headings", height=8)
        for c, t, w in zip(("id", "title", "author", "year", "status"), ["ID", "Название", "Автор", "Год", "Статус"], [40, 180, 150, 60, 100]):
            self.admin_books_tv.heading(c, text=t); self.admin_books_tv.column(c, width=w)
        self.admin_books_tv.pack(fill="both", expand=True)
        ttk.Button(bframe, text="Удалить книгу", command=self.admin_del_book).pack(pady=(5,0))

        # В центре: читатели.
        rframe = ttk.LabelFrame(main, text="Читатели", padding=8)
        rframe.pack(side="left", fill="y", padx=(0, 8))
        self.admin_readers_tv = ttk.Treeview(rframe, columns=("id", "name", "phone"), show="headings", height=8)
        for c, t, w in zip(("id", "name", "phone"), ["ID", "Имя", "Телефон"], [40, 160, 120]):
            self.admin_readers_tv.heading(c, text=t); self.admin_readers_tv.column(c, width=w)
        self.admin_readers_tv.pack(fill="both", expand=True)
        ttk.Button(rframe, text="Удалить читателя", command=self.admin_del_reader).pack(pady=(5,0))

        # Справа: выдачи.
        mframe = ttk.LabelFrame(main, text="Выданные", padding=8)
        mframe.pack(side="left", fill="both", expand=True)

        ttk.Label(mframe, text="Выданные теперь:").pack(anchor="w")
        self.admin_loans_tv = ttk.Treeview(mframe, columns=("id", "book_id", "reader_id", "due_date"), show="headings", height=10)
        for c, t, w in zip(("id", "book_id", "reader_id", "due_date"), ["ID", "Книга", "Читатель", "Вернуть до"], [50, 70, 70, 110]):
            self.admin_loans_tv.heading(c, text=t); self.admin_loans_tv.column(c, width=w)
        self.admin_loans_tv.pack(fill="both", expand=True)
        ttk.Button(mframe, text="Удалить выдачу", command=self.admin_del_loan).pack(pady=(5,0))

        self.refresh_admin_panel()

    def open_isbn_camera(self, isbn_entry, find_book):
        if cv2 is None or Image is None or ImageTk is None:
            messagebox.showerror("Камера", "Не установлен модуль камеры OpenCV.", parent=self)
            return

        camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not camera.isOpened():
            camera.release()
            messagebox.showerror("Камера", "Камера не найдена или занята другой программой.", parent=self)
            return

        window = tk.Toplevel(self)
        window.title("Сканировать ISBN")
        window.geometry("700x560")
        window.transient(self)
        video_label = ttk.Label(window)
        video_label.pack(fill="both", expand=True, padx=10, pady=10)
        status_label = ttk.Label(window, text="Наведите камеру на штрихкод ISBN")
        status_label.pack(pady=(0, 8))
        detector = cv2.barcode.BarcodeDetector()
        stopped = False

        def close_camera():
            nonlocal stopped
            stopped = True
            camera.release()
            window.destroy()

        def update_frame():
            if stopped or not window.winfo_exists():
                return
            ok, frame = camera.read()
            if not ok:
                status_label.config(text="Не удалось получить изображение с камеры")
                window.after(100, update_frame)
                return
            try:
                result = detector.detectAndDecode(frame)
                decoded_values = []

                def collect_strings(value):
                    if isinstance(value, str):
                        decoded_values.append(value)
                    elif isinstance(value, (tuple, list)):
                        for item in value:
                            collect_strings(item)

                collect_strings(result)
            except Exception:
                decoded_values = []
            for decoded in decoded_values:
                try:
                    normalized = normalize_isbn(decoded)
                except ValueError:
                    normalized = None
                if normalized:
                    isbn_entry.delete(0, "end")
                    isbn_entry.insert(0, normalized)
                    close_camera()
                    find_book()
                    return
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame)
            image.thumbnail((680, 480))
            photo = ImageTk.PhotoImage(image=image)
            video_label.configure(image=photo)
            video_label.image = photo
            window.after(30, update_frame)

        window.protocol("WM_DELETE_WINDOW", close_camera)
        update_frame()

    def refresh_admin_panel(self):
        # Книги.
        if hasattr(self, 'admin_books_tv'):
            self.admin_books_tv.delete(*self.admin_books_tv.get_children())
            for r in books_list():
                self.admin_books_tv.insert('', 'end', values=(r['id'], r['title'], r['author'], r['year'] or '', r['status']))
        # Читатели.
        if hasattr(self, 'admin_readers_tv'):
            self.admin_readers_tv.delete(*self.admin_readers_tv.get_children())
            for r in readers_list():
                self.admin_readers_tv.insert('', 'end', values=(r['id'], r['name'], r['phone'] or ''))
        # Выдачи.
        if hasattr(self, 'admin_loans_tv'):
            self.admin_loans_tv.delete(*self.admin_loans_tv.get_children())
            con = db()
            rows = con.execute("SELECT id, book_id, reader_id, due_date FROM loans ORDER BY id").fetchall(); con.close()
            for r in rows:
                self.admin_loans_tv.insert('', 'end', values=(r['id'], r['book_id'], r['reader_id'], r['due_date'] or '—'))
    def admin_del_book(self):
        sel = self.admin_books_tv.selection()
        if not sel:
            messagebox.showwarning("Библиотека", "Выберите книгу.", parent=self)
            return
        bid = self.admin_books_tv.item(sel[0])['values'][0]
        delete_book(bid)
        self.refresh_all(); self.refresh_admin_panel()

    def admin_del_reader(self):
        sel = self.admin_readers_tv.selection()
        if not sel:
            messagebox.showwarning("Библиотека", "Выберите читателя.", parent=self)
            return
        rid = self.admin_readers_tv.item(sel[0])['values'][0]
        con = db()
        con.execute("DELETE FROM loans WHERE reader_id=?", (rid,))
        con.execute("DELETE FROM readers WHERE id=?", (rid,))
        con.commit(); con.close()
        self.refresh_all(); self.refresh_admin_panel()

    def admin_del_loan(self):
        sel = self.admin_loans_tv.selection()
        if not sel:
            messagebox.showwarning("Библиотека", "Выберите выдачу.", parent=self)
            return
        lid = self.admin_loans_tv.item(sel[0])['values'][0]
        con = db()
        # Удалить выдачу и при необходимости вернуть книге статус «в наличии».
        row = con.execute("SELECT book_id FROM loans WHERE id=?", (lid,)).fetchone()
        if row:
            con.execute("UPDATE books SET status='в наличии', updated_at=? WHERE id=?",
                        (utc_now(), row['book_id']))
        con.execute("DELETE FROM loans WHERE id=?", (lid,))
        con.commit(); con.close()
        self.refresh_all(); self.refresh_admin_panel()

    def start_sync(self, show_result=False):
        if self.sync_running:
            return
        if not (os.getenv("GOOGLE_SHEETS_API_URL") or os.getenv("GOOGLE_SHEET_ID") or os.path.isfile(SYNC_CONFIG_FILE) or DEFAULT_API_URL):
            if show_result:
                url = simpledialog.askstring(
                    "Настройка синхронизации",
                    "Вставьте URL опубликованного Apps Script (заканчивается на /exec):",
                    parent=self,
                )
                if url and url.strip().startswith("https://"):
                    config_dir = os.path.dirname(SYNC_CONFIG_FILE)
                    os.makedirs(config_dir, exist_ok=True)
                    with open(SYNC_CONFIG_FILE, "w", encoding="utf-8") as file:
                        file.write(url.strip())
                else:
                    messagebox.showerror(
                        "Синхронизация",
                        "Нужен корректный HTTPS URL Apps Script.",
                        parent=self,
                    )
                    return
            else:
                return
        self.sync_running = True

        def worker():
            try:
                count = sync_books_with_google()
                self.after(0, lambda: self.sync_finished(None, count, show_result))
            except Exception as exc:
                self.after(0, lambda: self.sync_finished(exc, None, show_result))

        threading.Thread(target=worker, daemon=True).start()

    def sync_finished(self, error, count, show_result=False):
        self.sync_running = False
        if error:
            if show_result:
                messagebox.showerror("Синхронизация", str(error), parent=self)
            return
        self.refresh_all()
        if show_result:
            messagebox.showinfo("Синхронизация", f"Синхронизировано записей: {count}.", parent=self)

    def auto_sync(self):
        if not self.sync_running:
            self.start_sync()
        self.after(SYNC_INTERVAL_MS, self.auto_sync)

    def refresh_all(self):
        self.refresh_books()
        self.refresh_readers()
        self.refresh_issue()
        self.refresh_history()

if __name__ == "__main__":
    init_db()
    LibraryApp().mainloop()
