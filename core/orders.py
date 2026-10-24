"""Order rows, lazy lock expiry, and order lookup."""

from datetime import datetime, timezone

from core.errors import ToolFailure
from core.log import run_tool
from core.rows import as_int, find_row, format_time, optional_int, parse_time
from core.sheets import INVENTORY_TAB, ORDERS_TAB, get_store, write_lock

STATUS_LOCKED = "LOCKED"
STATUS_AWAITING = "AWAITING_PAYMENT"
STATUS_PAID = "PAID"
STATUS_EXPIRED = "EXPIRED"
STATUS_CANCELLED = "CANCELLED"

# Statuses that still hold reserved stock.
HOLDING_STATUSES = (STATUS_LOCKED, STATUS_AWAITING)


def release_expired_unlocked(now: datetime | None = None) -> None:
    """Expire LOCKED and AWAITING_PAYMENT orders whose lock time has passed.

    Caller must already hold write_lock. Reserved stock is decreased.
    Paid, cancelled, and already expired orders are left alone.
    """
    current = now or datetime.now(timezone.utc)
    store = get_store()
    orders = store.read_tab(ORDERS_TAB)
    expired: list[tuple[str, str, int]] = []
    for order in orders:
        if order.get("status", "") not in HOLDING_STATUSES:
            continue
        until = parse_time(order.get("locked_until", ""))
        # A blank or unreadable time is not treated as expired.
        if until is None or until > current:
            continue
        qty = as_int(order.get("qty", ""), "Order quantity")
        expired.append((order.get("order_id", ""), order.get("sku", ""), qty))
    if not expired:
        return
    inventory = {row.get("sku", ""): row for row in store.read_tab(INVENTORY_TAB)}
    released: dict[str, int] = {}
    for _order_id, sku, qty in expired:
        released[sku] = released.get(sku, 0) + qty
    for sku, qty in released.items():
        row = inventory.get(sku)
        if row is None:
            continue
        reserved = as_int(row.get("reserved", ""), "Reserved stock for " + sku)
        store.update_row(INVENTORY_TAB, "sku", sku, {"reserved": str(max(0, reserved - qty))})
    for order_id, _sku, _qty in expired:
        if order_id == "":
            continue
        store.update_row(ORDERS_TAB, "order_id", order_id, {"status": STATUS_EXPIRED})


def find_order(order_id: str) -> dict[str, str] | None:
    """Return one order row. Caller holds write_lock when a write will follow."""
    return find_row(get_store().read_tab(ORDERS_TAB), "order_id", order_id)


def present_order(row: dict[str, str]) -> dict[str, object]:
    """Turn a sheet row into the order object returned to the agent."""
    return {
        "order_id": row.get("order_id", ""),
        "reseller_id": row.get("reseller_id", ""),
        "sku": row.get("sku", ""),
        "qty": as_int(row.get("qty", ""), "Order quantity"),
        "status": row.get("status", ""),
        "locked_until": row.get("locked_until", ""),
        "address": row.get("address", ""),
        "city": row.get("city", ""),
        "delivery_fee": optional_int(row.get("delivery_fee", ""), "Delivery fee"),
        "total": optional_int(row.get("total", ""), "Order total"),
        "payment_ref": row.get("payment_ref", ""),
        "payment_url": row.get("payment_url", ""),
        "created_at": row.get("created_at", ""),
    }


def get_order(order_id: str) -> dict[str, object]:
    """Return the current status and saved fields for one order."""

    def body() -> dict[str, object]:
        cleaned = order_id.strip()
        if cleaned == "":
            raise ToolFailure("An order id is required.")
        with write_lock:
            release_expired_unlocked()
            row = find_order(cleaned)
            if row is None:
                raise ToolFailure("Order was not found.")
            return {"ok": True, "order": present_order(row)}

    return run_tool("get_order", {"order_id": order_id}, body)


def stamp_now() -> str:
    """Current UTC time for created_at and lock deadlines."""
    return format_time(datetime.now(timezone.utc))
