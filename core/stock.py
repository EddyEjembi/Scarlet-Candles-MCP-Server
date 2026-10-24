"""Stock listing and stock locks."""

import uuid
from datetime import datetime, timedelta, timezone

from core.config import lock_minutes
from core.errors import ToolFailure
from core.log import run_tool
from core.orders import STATUS_LOCKED, release_expired_unlocked
from core.resellers import ACTIVE, find_reseller_by_id
from core.rows import as_int, find_row, format_time
from core.sheets import INVENTORY_TAB, ORDERS_TAB, get_store, write_lock


def list_stock() -> dict[str, object]:
    """List each SKU with its name, naira price, and quantity still available.

    Available quantity is on_hand minus reserved. A failed sheet read returns
    an error. An empty inventory tab returns an empty list, not made-up stock.
    """

    def body() -> dict[str, object]:
        with write_lock:
            release_expired_unlocked()
            rows = get_store().read_tab(INVENTORY_TAB)
            items: list[dict[str, object]] = []
            for row in rows:
                sku = row.get("sku", "").strip()
                if sku == "":
                    raise ToolFailure("An inventory row has no SKU. No stock was invented.")
                on_hand = as_int(row.get("on_hand", ""), "On-hand stock for " + sku)
                reserved = as_int(row.get("reserved", ""), "Reserved stock for " + sku)
                price = as_int(row.get("price", ""), "Price for " + sku)
                items.append(
                    {
                        "sku": sku,
                        "name": row.get("name", ""),
                        "price": price,
                        "available": max(0, on_hand - reserved),
                    }
                )
        items.sort(key=lambda item: str(item["sku"]))
        return {"ok": True, "items": items}

    return run_tool("list_stock", {}, body)


def lock_stock(reseller_id: str, sku: str, qty: int) -> dict[str, object]:
    """Lock stock for one SKU and create an order in LOCKED status.

    Rejects a quantity below 1, an unknown or inactive reseller, an unknown SKU,
    and a quantity above the available stock. The lock lasts LOCK_MINUTES.
    """

    def body() -> dict[str, object]:
        cleaned_reseller = reseller_id.strip()
        cleaned_sku = sku.strip()
        if cleaned_reseller == "":
            raise ToolFailure("A reseller id is required.")
        if cleaned_sku == "":
            raise ToolFailure("A SKU is required.")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ToolFailure("Quantity must be greater than zero.")
        with write_lock:
            release_expired_unlocked()
            return _lock_unlocked(cleaned_reseller, cleaned_sku, qty)

    return run_tool(
        "lock_stock",
        {"reseller_id": reseller_id, "sku": sku, "qty": qty},
        body,
    )


def _lock_unlocked(reseller_id: str, sku: str, qty: int) -> dict[str, object]:
    """Create the order and increase reserved. Caller holds write_lock."""
    reseller = find_reseller_by_id(reseller_id)
    if reseller is None:
        raise ToolFailure("Reseller was not found.")
    if reseller.get("status", "").strip().casefold() != ACTIVE:
        raise ToolFailure("This reseller is inactive.")
    store = get_store()
    item = find_row(store.read_tab(INVENTORY_TAB), "sku", sku)
    if item is None:
        raise ToolFailure("SKU was not found.")
    on_hand = as_int(item.get("on_hand", ""), "On-hand stock for " + sku)
    reserved = as_int(item.get("reserved", ""), "Reserved stock for " + sku)
    available = max(0, on_hand - reserved)
    if qty > available:
        raise ToolFailure("Not enough stock. Available quantity is " + str(available) + ".")
    deadline = datetime.now(timezone.utc) + timedelta(minutes=lock_minutes())
    order_id = "ord_" + uuid.uuid4().hex[:12]
    created_at = format_time(datetime.now(timezone.utc))
    locked_until = format_time(deadline)
    # Reserve first, then append the order. Roll the reserve back if the append fails.
    store.update_row(INVENTORY_TAB, "sku", sku, {"reserved": str(reserved + qty)})
    order = {
        "order_id": order_id,
        "reseller_id": reseller_id,
        "sku": sku,
        "qty": str(qty),
        "status": STATUS_LOCKED,
        "locked_until": locked_until,
        "address": "",
        "city": "",
        "delivery_fee": "",
        "total": "",
        "payment_ref": "",
        "payment_url": "",
        "created_at": created_at,
    }
    try:
        store.append_row(ORDERS_TAB, order)
    except ToolFailure:
        store.update_row(INVENTORY_TAB, "sku", sku, {"reserved": str(reserved)})
        raise
    return {
        "ok": True,
        "order_id": order_id,
        "status": STATUS_LOCKED,
        "sku": sku,
        "qty": qty,
        "locked_until": locked_until,
        "available_after": available - qty,
    }
