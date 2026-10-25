"""Reseller lookup by WhatsApp number and by id."""

from core.errors import ToolFailure
from core.log import run_tool
from core.orders import release_expired_unlocked
from core.phone import normalize_whatsapp
from core.rows import as_int, find_row
from core.sheets import RESELLERS_TAB, get_store, write_lock

ACTIVE = "active"


def find_reseller_by_id(reseller_id: str) -> dict[str, str] | None:
    """Return one reseller row. Caller holds write_lock when a write will follow."""
    return find_row(get_store().read_tab(RESELLERS_TAB), "reseller_id", reseller_id)


def find_reseller_by_whatsapp(whatsapp_number: str) -> dict[str, str] | None:
    """Find a reseller whose stored WhatsApp matches after Nigerian number normalization."""
    wanted = normalize_whatsapp(whatsapp_number)
    for row in get_store().read_tab(RESELLERS_TAB):
        stored = row.get("whatsapp_number", "").strip()
        if stored == "":
            continue
        try:
            if normalize_whatsapp(stored) == wanted:
                return row
        except ToolFailure:
            # Skip sheet rows that are not a valid Nigerian number.
            continue
    return None


def present_reseller(row: dict[str, str]) -> dict[str, object]:
    """Turn a sheet row into the reseller object returned to the agent."""
    return {
        "reseller_id": row.get("reseller_id", ""),
        "name": row.get("name", ""),
        "whatsapp_number": row.get("whatsapp_number", ""),
        "email": row.get("email", ""),
        "status": row.get("status", ""),
        "lifetime_total": as_int(row.get("lifetime_total", ""), "Reseller lifetime total"),
    }


def get_reseller(whatsapp_number: str) -> dict[str, object]:
    """Look up a reseller by WhatsApp number.

    Accepts +234…, 234…, or 0… Nigerian forms. Matching uses the normalized
    +234XXXXXXXXXX form against the sheet, so the stored cell can use any of those shapes.

    Returns the reseller when they are registered and active.
    Returns an error when the number is not registered or the reseller is inactive.
    """

    def body() -> dict[str, object]:
        # Normalize first so bad formats fail before we touch Sheets.
        normalize_whatsapp(whatsapp_number)
        with write_lock:
            release_expired_unlocked()
            row = find_reseller_by_whatsapp(whatsapp_number)
            if row is None:
                raise ToolFailure("This WhatsApp number is not registered.")
            if row.get("status", "").strip().casefold() != ACTIVE:
                raise ToolFailure("This reseller is inactive.")
            return {"ok": True, "reseller": present_reseller(row)}

    return run_tool("get_reseller", {"whatsapp_number": whatsapp_number}, body)
