"""Обмен записями книг с Google Apps Script по HTTP."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except Exception:
    Credentials = None
    build = None
    HttpError = Exception


class GoogleSheetsSyncError(RuntimeError):
    """Ошибка подключения или проверки ответа Google Sheets."""


DEFAULT_API_URL = "https://script.google.com/macros/s/AKfycbyEQRaDPcvGWs4GdKUhrNUBnKOLqAEIgd3EoMKt0jaea-FsEv9ByXkTbsD9lPWLS8zKeQ/exec"


def _api_url():
    url = os.getenv("GOOGLE_SHEETS_API_URL", "").strip()
    if not url:
        config_file = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "LibraryApp", "google_sheets_url.txt")
        try:
            with open(config_file, encoding="utf-8") as file:
                url = file.read().lstrip("\ufeff").strip()
        except OSError:
            pass
    if not url:
        url = DEFAULT_API_URL
    if not url.startswith("https://"):
        raise GoogleSheetsSyncError("URL Google Sheets API должен начинаться с https://")
    return url


def _credentials_file():
    return os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "stoked-cosine-508816-g2-a1cca168506a.json"),
    )


def _sheet_id():
    return os.getenv("GOOGLE_SHEET_ID", "").strip()


def _sheet_tab(service):
    configured = os.getenv("GOOGLE_SHEET_TAB", "Книги").strip()
    metadata = service.spreadsheets().get(
        spreadsheetId=_sheet_id(), fields="sheets.properties.title"
    ).execute()
    titles = [item["properties"]["title"] for item in metadata.get("sheets", [])]
    if configured in titles:
        return configured
    if titles:
        return titles[0]
    raise GoogleSheetsSyncError("В Google Таблице нет вкладок.")


def _ensure_sheet_tab(service, title):
    titles = [item["properties"]["title"] for item in service.spreadsheets().get(
        spreadsheetId=_sheet_id(), fields="sheets.properties.title"
    ).execute().get("sheets", [])]
    if title not in titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=_sheet_id(),
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
    return title


def _direct_read_tab(title, headers):
    service = _sheets_service()
    if title == "Читатели":
        sheet_title = _ensure_sheet_tab(service, "Читатели")
        values = service.spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range=f"'{sheet_title}'!A1:D"
        ).execute().get("values", [])
        if not values:
            return []
        header_aliases = {
            "ID читателя": "id", "id": "id",
            "Имя": "name", "name": "name",
            "Телефон": "phone", "phone": "phone",
            "Дата изменения": "updated_at", "updated_at": "updated_at",
        }
        actual_headers = [header_aliases.get(str(header).strip(), str(header).strip()) for header in values[0]]
        return [dict(zip(actual_headers, row + [""] * (len(actual_headers) - len(row)))) for row in values[1:]]
    title = _ensure_sheet_tab(service, title)
    values = service.spreadsheets().values().get(
        spreadsheetId=_sheet_id(), range=f"'{title}'!A1:Z"
    ).execute().get("values", [])
    if not values:
        return []
    actual_headers = values[0]
    return [dict(zip(actual_headers, row + [""] * (len(actual_headers) - len(row)))) for row in values[1:]]


def _direct_write_tab(title, headers, records):
    service = _sheets_service()
    if title == "Читатели":
        sheet_title = _ensure_sheet_tab(service, "Читатели")
        display_headers = ["ID читателя", "Имя", "Телефон", "Дата изменения"]
        values = [display_headers] + [[record.get(header, "") for header in headers] for record in records]
        service.spreadsheets().values().clear(
            spreadsheetId=_sheet_id(), range=f"'{sheet_title}'!A1:D"
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=_sheet_id(), range=f"'{sheet_title}'!A1:D",
            valueInputOption="RAW", body={"values": values},
        ).execute()
        return
    title = _ensure_sheet_tab(service, title)
    values = [headers] + [[record.get(header, "") for header in headers] for record in records]
    sheet_range = f"'{title}'!A1:{chr(64 + len(headers))}"
    service.spreadsheets().values().clear(spreadsheetId=_sheet_id(), range=f"'{title}'!A1:Z").execute()
    service.spreadsheets().values().update(
        spreadsheetId=_sheet_id(), range=sheet_range,
        valueInputOption="RAW", body={"values": values},
    ).execute()


def _direct_enabled():
    return bool(_sheet_id() and os.path.isfile(_credentials_file()) and Credentials and build)


def _sheets_service():
    try:
        credentials = Credentials.from_service_account_file(
            _credentials_file(),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise GoogleSheetsSyncError(f"Не удалось открыть Google-ключ: {exc}") from exc


def _direct_fetch_books():
    try:
        service = _sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range=f"'{_sheet_tab(service)}'!A1:Z"
        ).execute()
    except HttpError as exc:
        raise GoogleSheetsSyncError(f"Google Sheets API вернул ошибку: {exc}") from exc
    values = result.get("values", [])
    if not values:
        return []
    header_aliases = {
        "ID": "inventory_id", "inventory_id": "inventory_id",
        "Название": "title", "title": "title",
        "Автор": "author", "author": "author",
        "Год": "year", "year": "year",
        "ISBN": "isbn", "isbn": "isbn",
        "Статус": "status", "status": "status",
        "Дата изменения": "updated_at", "updated_at": "updated_at",
        "Удалена": "deleted", "deleted": "deleted",
        "reader_id": "reader_id", "reader_name": "reader_name",
        "issue_date": "issue_date", "due_date": "due_date",
        "return_date": "return_date",
        "Кому выдано": "reader_name", "Читатель": "reader_name",
        "Дата выдачи": "issue_date", "Срок возврата": "due_date",
        "Дата возврата": "return_date",
    }
    headers = [header_aliases.get(str(header).strip(), str(header).strip()) for header in values[0]]
    return [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in values[1:]]


def _direct_save_books(records):
    headers = [
        "inventory_id", "title", "author", "year", "isbn", "status", "updated_at",
        "issue_date", "due_date", "return_date",
    ]
    display_headers = [
        "ID", "Название", "Автор", "Год", "ISBN", "Статус", "Дата изменения",
        "Дата выдачи", "Срок возврата", "Дата возврата",
    ]
    values = [headers] + [
        [
            record.get(header, "")
            for header in headers
        ]
        for record in records
    ]
    values[0] = display_headers
    try:
        service = _sheets_service()
        sheet_range = f"'{_sheet_tab(service)}'!A1:Z"
        service.spreadsheets().values().clear(
            spreadsheetId=_sheet_id(), range=sheet_range
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=_sheet_id(), range=sheet_range,
            valueInputOption="RAW", body={"values": values},
        ).execute()
    except HttpError as exc:
        raise GoogleSheetsSyncError(f"Google Sheets API вернул ошибку: {exc}") from exc
    return {"saved": len(records), "mode": "direct"}


def _request(method, payload=None, timeout=15):
    token = os.getenv("GOOGLE_SHEETS_SYNC_TOKEN", "").strip()
    url = _api_url()
    if method == "GET" and token:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode({'token': token})}"
    if method != "GET" and token:
        payload = dict(payload or {})
        payload["token"] = token
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleSheetsSyncError(f"Google Sheets API вернул HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GoogleSheetsSyncError(f"Не удалось связаться с Google Sheets: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise GoogleSheetsSyncError(str(result.get("error", "Некорректный ответ Google Sheets API")))
    return result

def fetch_books():
    """Получить проверенный список записей книг из удалённой таблицы."""
    if _direct_enabled():
        return _direct_fetch_books()
    result = _request("GET")
    records = result.get("records")
    if not isinstance(records, list):
        raise GoogleSheetsSyncError("Ответ Google Sheets не содержит список records.")
    return records


def save_books(records):
    """Передать записи книг в Apps Script для объединения по inventory_id."""
    if not isinstance(records, list):
        raise GoogleSheetsSyncError("Для отправки нужен список записей.")
    if _direct_enabled():
        return _direct_save_books(records)
    return _request("POST", {"action": "upsert", "records": records})


def fetch_readers():
    """Получить читателей из Google Sheets."""
    if _direct_enabled():
        return _direct_read_tab("Читатели", ["id", "name", "phone", "updated_at"])
    result = _request("GET")
    records = result.get("readers", [])
    if not isinstance(records, list):
        raise GoogleSheetsSyncError("Ответ Google Sheets содержит некорректный список readers.")
    return records


def save_readers(records):
    """Сохранить читателей в Google Sheets."""
    if not isinstance(records, list):
        raise GoogleSheetsSyncError("Для отправки нужен список читателей.")
    if not _direct_enabled():
        return _request("POST", {"action": "readers_upsert", "records": records})
    _direct_write_tab("Читатели", ["id", "name", "phone", "updated_at"], records)
    return {"saved": len(records), "mode": "direct"}
