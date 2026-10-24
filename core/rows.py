"""Small helpers for sheet cells and timestamps."""

from datetime import datetime, timezone

from core.errors import ToolFailure


def find_row(rows: list[dict[str, str]], field: str, value: str) -> dict[str, str] | None:
    """Return the first row whose field equals value."""
    for row in rows:
        if row.get(field, "") == value:
            return row
    return None


def as_int(value: str, label: str) -> int:
    """Parse a sheet cell as a whole number. Never invent a value."""
    text = value.strip()
    try:
        number = float(text)
    except ValueError as exc:
        raise ToolFailure(label + " is not a whole number.") from exc
    if not number.is_integer():
        raise ToolFailure(label + " is not a whole number.")
    return int(number)


def optional_int(value: str, label: str) -> int | None:
    """Parse a cell that may be blank. Blank becomes None."""
    if value.strip() == "":
        return None
    return as_int(value, label)


def parse_time(value: str) -> datetime | None:
    """Parse an ISO timestamp. Returns None when the cell is blank or invalid."""
    text = value.strip()
    if text == "":
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: datetime) -> str:
    """Format a timestamp in UTC for a sheet cell."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
