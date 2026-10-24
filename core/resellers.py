"""Reseller lookup by WhatsApp number and by id."""

from core.errors import ToolFailure
from core.log import run_tool
from core.orders import release_expired_unlocked
from core.rows import as_int, find_row
from core.sheets import RESELLERS_TAB, get_store, write_lock

ACTIVE = "active"


def find_reseller_by_id(reseller_id: str) -> dict[str, str] | None:
    """Return one reseller row. Caller holds write_lock when a write will follow."""
    return find_row(get_store().read_tab(RESELLERS_TAB), "reseller_id", reseller_id)


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

    Returns the reseller when they are registered and active.
    Returns an error when the number is not registered or the reseller is inactive.
    """

    def body() -> dict[str, object]:
        number = whatsapp_number.strip()
        if number == "":
            raise ToolFailure("A WhatsApp number is required.")
        with write_lock:
            release_expired_unlocked()
            rows = get_store().read_tab(RESELLERS_TAB)
            row = find_row(rows, "whatsapp_number", number)
            if row is None:
                raise ToolFailure("This WhatsApp number is not registered.")
            if row.get("status", "").strip().casefold() != ACTIVE:
                raise ToolFailure("This reseller is inactive.")
            return {"ok": True, "reseller": present_reseller(row)}

    return run_tool("get_reseller", {"whatsapp_number": whatsapp_number}, body)
