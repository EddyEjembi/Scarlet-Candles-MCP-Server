"""Google Sheets access and an in-memory store used by tests.

Writes go through one process-wide lock. Google Sheets is still not a
transaction: a crash between two cell updates can leave rows half-changed.
"""

import json
import os
import threading
from typing import Protocol

import gspread
from google.oauth2.service_account import Credentials

from core.config import require_env
from core.errors import ToolFailure

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

INVENTORY_TAB = "Inventory"
ORDERS_TAB = "Orders"
RESELLERS_TAB = "Resellers"

TAB_HEADERS: dict[str, list[str]] = {
    INVENTORY_TAB: ["sku", "name", "price", "on_hand", "reserved"],
    # payment_url is stored so create_payment_link can return the same link.
    ORDERS_TAB: [
        "order_id",
        "reseller_id",
        "sku",
        "qty",
        "status",
        "locked_until",
        "address",
        "city",
        "delivery_fee",
        "total",
        "payment_ref",
        "payment_url",
        "created_at",
    ],
    RESELLERS_TAB: ["reseller_id", "name", "whatsapp_number", "email", "status", "lifetime_total"],
}

# Serializes sheet writes in this process only.
write_lock = threading.Lock()


class SheetError(ToolFailure):
    """Sheets could not be read or written. retryable so the webhook can be resent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class SheetStore(Protocol):
    """The three tabs, with every cell kept as a string."""

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        """Return every data row for a tab."""

    def append_row(self, tab: str, row: dict[str, str]) -> None:
        """Append one row."""

    def update_row(self, tab: str, key_field: str, key_value: str, updates: dict[str, str]) -> None:
        """Update the first row whose key field matches."""

    def replace_tab(self, tab: str, rows: list[dict[str, str]]) -> None:
        """Replace every data row. Used by the seed script."""


class MemorySheetStore:
    """Sheet stand-in for tests. Same string cells as the live spreadsheet."""

    def __init__(self) -> None:
        self.tabs: dict[str, list[dict[str, str]]] = {name: [] for name in TAB_HEADERS}

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        """Return copies of the rows so callers cannot change the store by accident."""
        self._require_headers(tab)
        return [dict(row) for row in self.tabs[tab]]

    def append_row(self, tab: str, row: dict[str, str]) -> None:
        """Append one row, filling any missing header with a blank cell."""
        headers = self._require_headers(tab)
        self.tabs[tab].append({header: str(row.get(header, "")) for header in headers})

    def update_row(self, tab: str, key_field: str, key_value: str, updates: dict[str, str]) -> None:
        """Update one row matched by key. Raises when that row does not exist."""
        self._require_headers(tab)
        for row in self.tabs[tab]:
            if row.get(key_field, "") == key_value:
                for key, value in updates.items():
                    row[key] = str(value)
                return
        raise SheetError("No " + tab + " row with " + key_field + " " + key_value + ".")

    def replace_tab(self, tab: str, rows: list[dict[str, str]]) -> None:
        """Wipe a tab and write the given rows."""
        headers = self._require_headers(tab)
        self.tabs[tab] = [{header: str(row.get(header, "")) for header in headers} for row in rows]

    def _require_headers(self, tab: str) -> list[str]:
        """Return the known headers, or raise for a tab this project does not use."""
        headers = TAB_HEADERS.get(tab)
        if headers is None:
            raise SheetError("Unknown tab " + tab + ".")
        return headers


class GoogleSheetStore:
    """Live spreadsheet. Cell values are normalized to strings on the way in."""

    def __init__(self, spreadsheet: gspread.Spreadsheet) -> None:
        self._spreadsheet = spreadsheet

    @classmethod
    def connect_from_env(cls) -> "GoogleSheetStore":
        """Open the spreadsheet named by SHEET_ID using the service account."""
        info = _load_service_account(require_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
        sheet_id = require_env("SHEET_ID")
        try:
            creds = Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key(sheet_id)
        except ToolFailure:
            raise
        except Exception as exc:
            raise SheetError("Could not open the Google spreadsheet. " + _google_reason(exc)) from exc
        return cls(spreadsheet)

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        """Read one tab. A missing tab or a failed request is an error, not empty stock."""
        try:
            return self._read_tab(tab)
        except SheetError:
            raise
        except Exception as exc:
            raise SheetError("Could not read the " + tab + " tab in Google Sheets. Nothing was changed.") from exc

    def append_row(self, tab: str, row: dict[str, str]) -> None:
        """Append one row using the header names already on the sheet."""
        try:
            self._append_row(tab, row)
        except SheetError:
            raise
        except Exception as exc:
            raise SheetError("Could not append a row to the " + tab + " tab. Nothing else was changed.") from exc

    def update_row(self, tab: str, key_field: str, key_value: str, updates: dict[str, str]) -> None:
        """Rewrite one matched row."""
        try:
            self._update_row(tab, key_field, key_value, updates)
        except SheetError:
            raise
        except Exception as exc:
            raise SheetError("Could not update the " + tab + " tab. Nothing else was changed.") from exc

    def replace_tab(self, tab: str, rows: list[dict[str, str]]) -> None:
        """Clear a tab and write headers plus rows. Creates the tab when it is missing."""
        try:
            self._replace_tab(tab, rows)
        except SheetError:
            raise
        except Exception as exc:
            raise SheetError("Could not rewrite the " + tab + " tab.") from exc

    def _worksheet(self, tab: str, create: bool) -> gspread.Worksheet:
        """Open a worksheet by its tab name."""
        _require_known_tab(tab)
        try:
            return self._spreadsheet.worksheet(tab)
        except gspread.WorksheetNotFound:
            if create:
                return self._spreadsheet.add_worksheet(title=tab, rows=200, cols=len(TAB_HEADERS[tab]))
            raise SheetError("The " + tab + " tab was not found in the spreadsheet.")

    def _read_tab(self, tab: str) -> list[dict[str, str]]:
        """Read rows by header name. A header-only tab is a real empty list."""
        worksheet = self._worksheet(tab, create=False)
        values = worksheet.get_all_values()
        if not values:
            raise SheetError("The " + tab + " tab has no header row.")
        headers = [cell.strip() for cell in values[0]]
        missing = [header for header in TAB_HEADERS[tab] if header not in headers]
        if missing:
            raise SheetError("The " + tab + " tab is missing columns: " + ", ".join(missing) + ".")
        rows: list[dict[str, str]] = []
        for raw in values[1:]:
            padded = raw + [""] * (len(headers) - len(raw))
            if not any(cell.strip() for cell in padded):
                continue
            rows.append({headers[index]: padded[index].strip() for index in range(len(headers))})
        return rows

    def _append_row(self, tab: str, row: dict[str, str]) -> None:
        """Append values in the sheet's current header order."""
        worksheet = self._worksheet(tab, create=False)
        values = worksheet.get_all_values()
        if not values:
            raise SheetError("The " + tab + " tab has no header row.")
        headers = [cell.strip() for cell in values[0]]
        missing = [header for header in TAB_HEADERS[tab] if header not in headers]
        if missing:
            raise SheetError("The " + tab + " tab is missing columns: " + ", ".join(missing) + ".")
        ordered = [str(row.get(header, "")) for header in headers]
        worksheet.append_row(ordered)

    def _update_row(self, tab: str, key_field: str, key_value: str, updates: dict[str, str]) -> None:
        """Find a row by key and write the whole row back."""
        worksheet = self._worksheet(tab, create=False)
        values = worksheet.get_all_values()
        if not values:
            raise SheetError("The " + tab + " tab has no header row.")
        headers = [cell.strip() for cell in values[0]]
        if key_field not in headers:
            raise SheetError("The " + tab + " tab has no column " + key_field + ".")
        key_index = headers.index(key_field)
        found: tuple[int, list[str]] | None = None
        for offset, raw in enumerate(values[1:], start=2):
            padded = list(raw) + [""] * (len(headers) - len(raw))
            if padded[key_index].strip() == key_value:
                found = (offset, padded)
                break
        if found is None:
            raise SheetError("No " + tab + " row with " + key_field + " " + key_value + ".")
        row_number, padded = found
        current = {headers[index]: padded[index].strip() for index in range(len(headers))}
        for key, value in updates.items():
            if key not in current:
                raise SheetError("The " + tab + " tab has no column " + key + ".")
            current[key] = str(value)
        ordered = [current.get(header, "") for header in headers]
        worksheet.update([ordered], "A" + str(row_number))

    def _replace_tab(self, tab: str, rows: list[dict[str, str]]) -> None:
        """Clear the tab, then write the canonical header and the new rows."""
        headers = _require_known_tab(tab)
        worksheet = self._worksheet(tab, create=True)
        worksheet.clear()
        table: list[list[str]] = [list(headers)]
        for row in rows:
            table.append([str(row.get(header, "")) for header in headers])
        worksheet.update(table, "A1")


_store: SheetStore | None = None


def get_store() -> SheetStore:
    """Return the active store, connecting to Google Sheets on first use."""
    global _store
    if _store is None:
        _store = GoogleSheetStore.connect_from_env()
    return _store


def set_store(store: SheetStore | None) -> None:
    """Install a store. Tests pass an in-memory store. None reconnects next time."""
    global _store
    _store = store


def _google_reason(exc: Exception) -> str:
    """Use Google's own error text. gspread often hides it on an empty PermissionError."""
    cause = exc.__cause__
    if cause is not None and str(cause).strip() != "":
        return str(cause).strip()
    if str(exc).strip() != "":
        return str(exc).strip()
    return "Check the service account and SHEET_ID."


def _require_known_tab(tab: str) -> list[str]:
    """Return headers for a tab this project knows about."""
    headers = TAB_HEADERS.get(tab)
    if headers is None:
        raise SheetError("Unknown tab " + tab + ".")
    return headers


def _load_service_account(raw: str) -> dict[str, object]:
    """Load service-account JSON from a file path or from the raw JSON string."""
    parsed: object
    if os.path.isfile(raw):
        try:
            with open(raw, encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON could not be read.") from exc
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SheetError(
                "GOOGLE_SERVICE_ACCOUNT_JSON must be a path to the service account file, or the JSON itself."
            ) from exc
    if not isinstance(parsed, dict):
        raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object.")
    info: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object.")
        info[key] = value
    return info
