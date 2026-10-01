from flask import Flask, jsonify, render_template, request, redirect, session, url_for
import sqlite3
import os
import json
import urllib.parse
import urllib.request
import threading
import time
import library_app

app = Flask(__name__, template_folder='templates')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "library-admin-secret")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.abspath(os.getenv("LIBRARY_DB_PATH", os.path.join(APP_DIR, "library.db")))
PUBLIC_BOOKS_CACHE_SECONDS = max(0, int(os.getenv("PUBLIC_BOOKS_CACHE_SECONDS", "300")))
_public_books_cache = None
_public_books_cache_expires_at = 0.0
_public_books_cache_fetcher = None
_public_books_cache_lock = threading.Lock()
library_app.DB = DB
library_app.init_db()


@app.after_request
def prevent_admin_cache(response):
    """Do not let the browser reopen a previously authorized admin page."""
    if request.path == "/admin" or request.path.startswith("/admin/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _fetch_public_books():
    global _public_books_cache, _public_books_cache_expires_at, _public_books_cache_fetcher
    fetcher = library_app.fetch_books
    with _public_books_cache_lock:
        now = time.monotonic()
        if (
            PUBLIC_BOOKS_CACHE_SECONDS
            and _public_books_cache is not None
            and _public_books_cache_fetcher is fetcher
            and now < _public_books_cache_expires_at
        ):
            return _public_books_cache

        records = fetcher()
        if PUBLIC_BOOKS_CACHE_SECONDS:
            _public_books_cache = records
            _public_books_cache_expires_at = now + PUBLIC_BOOKS_CACHE_SECONDS
            _public_books_cache_fetcher = fetcher
        return records


def _invalidate_public_books_cache():
    global _public_books_cache, _public_books_cache_expires_at, _public_books_cache_fetcher
    with _public_books_cache_lock:
        _public_books_cache = None
        _public_books_cache_expires_at = 0.0
        _public_books_cache_fetcher = None


def init_db():
    con = db()
    columns = {row["name"] for row in con.execute("PRAGMA table_info(books)")}
    if "isbn" not in columns:
        con.execute("ALTER TABLE books ADD COLUMN isbn TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_books_isbn ON books(isbn) WHERE isbn IS NOT NULL")
    con.commit()
    con.close()


def normalize_isbn(value):
    isbn = "".join(char for char in str(value or "").upper() if char.isdigit() or char == "X")
    if len(isbn) == 10:
        if not (isbn[:9].isdigit() and (isbn[9].isdigit() or isbn[9] == "X")):
            raise ValueError("ISBN-10 должен содержать 9 цифр и контрольный символ.")
        total = sum((10 - index) * int(char) for index, char in enumerate(isbn[:9]))
        total += 10 if isbn[9] == "X" else int(isbn[9])
        if total % 11:
            raise ValueError("Неверная контрольная сумма ISBN-10.")
        isbn = "978" + isbn[:9]
        isbn += str((10 - sum((index + 1) * int(char) for index, char in enumerate(isbn)) % 10) % 10)
    elif len(isbn) == 13:
        if not isbn.isdigit() or isbn[:3] not in ("978", "979"):
            raise ValueError("ISBN-13 должен начинаться с 978 или 979.")
        if sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(isbn)) % 10:
            raise ValueError("Неверная контрольная сумма ISBN-13.")
    elif isbn:
        raise ValueError("ISBN должен содержать 10 или 13 цифр.")
    return isbn or None


def get_book_info(isbn):
    isbn = normalize_isbn(isbn)
    if not isbn:
        raise ValueError("Введите ISBN.")
    url = "https://openlibrary.org/api/books?bibkeys=ISBN:%s&format=json&jscmd=data" % urllib.parse.quote(isbn)
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.load(response)
    except Exception as exc:
        raise ValueError("Не удалось получить данные по ISBN. Проверьте интернет.") from exc
    book = payload.get("ISBN:" + isbn)
    if not book:
        raise ValueError("Книга по этому ISBN не найдена.")
    authors = book.get("authors") or []
    return {
        "isbn": isbn,
        "title": (book.get("title") or "").strip(),
        "author": (authors[0].get("name") if authors else "").strip(),
        "year": book.get("publish_date", "")[:4] if book.get("publish_date") else None,
    }

@app.route("/")
def index():
    return render_template(
        "index.html",
        books=public_books(),
        registration_message=session.pop("registration_message", None),
        registration_error=session.pop("registration_error", None),
    )


def public_books():
    def clean_row(row):
        if row is None:
            return None
        title = str(row.get("title") or "").strip()
        author = str(row.get("author") or "").strip()
        if not title or not author:
            return None
        inventory_id = str(row.get("inventory_id") or "").strip()
        book_id = inventory_id or str(row.get("id") or "").strip()
        status = str(row.get("status") or "в наличии").strip().lower()
        return {
            "id": book_id,
            "title": title,
            "author": author,
            "year": row.get("year", ""),
            "status": "выдан" if status == "выдана" else "в наличии",
        }

    def identity(book):
        title = " ".join(str(book.get("title") or "").split()).lower()
        author = " ".join(str(book.get("author") or "").split()).lower()
        year = str(book.get("year") or "").strip()
        return f"title:{title}|author:{author}|year:{year}"

    merged = {}
    con = db()
    rows = con.execute(
        "SELECT id, inventory_id, title, author, year, status FROM books WHERE TRIM(COALESCE(title, '')) <> '' AND TRIM(COALESCE(author, '')) <> '' ORDER BY title"
    ).fetchall()
    con.close()

    for row in rows:
        book = clean_row(dict(row))
        if book is None:
            continue
        merged[identity(book)] = book

    try:
        for row in _fetch_public_books():
            if row.get("deleted"):
                continue
            book = clean_row(row)
            if book is not None:
                merged.setdefault(identity(book), book)
    except Exception:
        pass

    return sorted(merged.values(), key=lambda item: str(item.get("title", "")).lower())


@app.route("/register", methods=["POST"])
def register_reader():
    try:
        library_app.add_reader(request.form.get("name", ""), request.form.get("phone", ""))
        try:
            library_app.sync_readers_with_google()
            session["registration_message"] = "Регистрация завершена. Вы добавлены в базу читателей."
        except Exception:
            session["registration_message"] = "Вы зарегистрированы в приложении. Синхронизация с таблицей будет выполнена позже."
    except Exception as exc:
        session["registration_error"] = str(exc)
    return redirect(url_for("index"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == library_app.ADMIN_PASSWORD:
            session.clear()
            session["admin"] = True
        else:
            return render_template(
                "admin.html",
                admin_logged_in=False,
                error="Неверный пароль",
                books=[],
                readers=[],
                loans=[],
            )

    if not session.get("admin"):
        return render_template("admin.html", admin_logged_in=False, error=None, books=[], readers=[], loans=[])

    books = library_app.books_list()
    readers = library_app.readers_list()
    loans = library_app.active_loans()
    return render_template("admin.html", admin_logged_in=True, error=None, books=books, readers=readers, loans=loans)


@app.route("/admin/issue", methods=["POST"])
def admin_issue():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    book_id = request.form.get("book_id")
    reader_id = request.form.get("reader_id")
    due_date = request.form.get("due_date")
    try:
        message = library_app.issue_book(int(book_id), int(reader_id), due_date)
        if message:
            session["admin_message"] = message
        else:
            session["admin_message"] = "Книга выдана"
    except Exception as exc:
        session["admin_message"] = str(exc)
    return redirect(url_for("admin"))


@app.route("/admin/return", methods=["POST"])
def admin_return():
    if not session.get("admin"):
        return redirect(url_for("admin"))

    loan_id = request.form.get("loan_id")
    book_id = request.form.get("book_id")
    try:
        library_app.return_book(int(loan_id), int(book_id))
        session["admin_message"] = "Книга возвращена"
    except Exception as exc:
        session["admin_message"] = str(exc)
    return redirect(url_for("admin"))


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin"))


@app.route("/api/books", methods=['GET'])
def api_books():
    return jsonify(public_books())


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap():
    return render_template("sitemap.xml", base_url=request.url_root.rstrip("/")), 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.route("/api/sync", methods=["POST"])
def api_sync():
    try:
        count = library_app.sync_books_with_google()
        _invalidate_public_books_cache()
        return jsonify({"message": "Синхронизация завершена", "count": count})
    except Exception as exc:
        return jsonify({"message": str(exc)}), 502




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
