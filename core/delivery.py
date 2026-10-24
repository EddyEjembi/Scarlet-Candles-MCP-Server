"""Delivery quotes from data/delivery_zones.json. Fees are naira. Unknown cities are refused."""

import json
from pathlib import Path

from core.errors import ToolFailure
from core.log import run_tool
from core.orders import STATUS_AWAITING, STATUS_LOCKED, find_order, release_expired_unlocked
from core.rows import as_int, find_row
from core.sheets import INVENTORY_TAB, ORDERS_TAB, get_store, write_lock


def quote_delivery(order_id: str, address: str, city: str) -> dict[str, object]:
    """Price delivery for a locked order and move it to AWAITING_PAYMENT.

    The city must be listed in delivery_zones.json. An unknown city returns an
    error and does not save a fee. The fee is never guessed.
    """

    def body() -> dict[str, object]:
        cleaned = order_id.strip()
        if cleaned == "":
            raise ToolFailure("An order id is required.")
        cleaned_address = address.strip()
        if cleaned_address == "":
            raise ToolFailure("An address is required.")
        with write_lock:
            release_expired_unlocked()
            return _quote_unlocked(cleaned, cleaned_address, city)

    return run_tool(
        "quote_delivery",
        {"order_id": order_id, "address": address, "city": city},
        body,
    )


def _quote_unlocked(order_id: str, address: str, city: str) -> dict[str, object]:
    """Save the quote onto the order. Caller holds write_lock."""
    order = find_order(order_id)
    if order is None:
        raise ToolFailure("Order was not found.")
    status = order.get("status", "")
    payment_url = order.get("payment_url", "").strip()
    if status == STATUS_LOCKED or (status == STATUS_AWAITING and payment_url == ""):
        pass
    elif status == STATUS_AWAITING and payment_url != "":
        raise ToolFailure("A payment link already exists for this order.")
    elif status == "EXPIRED":
        raise ToolFailure("This order has expired. Lock the stock again.")
    elif status == "PAID":
        raise ToolFailure("This order is already paid.")
    else:
        raise ToolFailure("This order is " + status + " and cannot be quoted.")
    if city.strip() == "":
        raise ToolFailure(
            "A city is required. Ask for a supported city. Do not guess a fee. Supported cities: "
            + supported_city_list()
            + "."
        )
    found = find_zone(city)
    if found is None:
        raise ToolFailure(
            "City \""
            + city.strip()
            + "\" is not supported. Ask for a supported city, or the nearest one, and pass it to the team if they are unsure. Do not guess a fee. Supported cities: "
            + supported_city_list()
            + "."
        )
    canonical, fee = found
    store = get_store()
    item = find_row(store.read_tab(INVENTORY_TAB), "sku", order.get("sku", ""))
    if item is None:
        raise ToolFailure("SKU was not found. Nothing was changed.")
    price = as_int(item.get("price", ""), "Price")
    qty = as_int(order.get("qty", ""), "Order quantity")
    goods = price * qty
    total = goods + fee
    store.update_row(
        ORDERS_TAB,
        "order_id",
        order_id,
        {
            "address": address,
            "city": canonical,
            "delivery_fee": str(fee),
            "total": str(total),
            "status": STATUS_AWAITING,
        },
    )
    return {
        "ok": True,
        "order_id": order_id,
        "city": canonical,
        "address": address,
        "delivery_fee": fee,
        "goods_total": goods,
        "total": total,
        "status": STATUS_AWAITING,
    }


def load_zones() -> dict[str, int]:
    """Read city delivery fees in naira from data/delivery_zones.json."""
    path = Path(__file__).resolve().parent.parent / "data" / "delivery_zones.json"
    try:
        with path.open(encoding="utf-8") as handle:
            parsed: object = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolFailure("delivery_zones.json could not be read.") from exc
    if not isinstance(parsed, dict):
        raise ToolFailure("delivery_zones.json must be a JSON object of cities and fees.")
    zones: dict[str, int] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or key.strip() == "":
            raise ToolFailure("delivery_zones.json has an invalid city.")
        zones[key.strip()] = _fee(value)
    return zones


def find_zone(city: str) -> tuple[str, int] | None:
    """Return the canonical city name and naira fee, or None when the city is not listed."""
    needle = city.strip().casefold()
    if needle == "":
        return None
    for name, fee in load_zones().items():
        if name.casefold() == needle:
            return name, fee
    return None


def supported_city_list() -> str:
    """Comma-separated city names the quote tool will accept."""
    return ", ".join(sorted(load_zones()))


def _fee(value: object) -> int:
    """Accept a whole-number fee from the JSON file."""
    if isinstance(value, bool):
        raise ToolFailure("delivery_zones.json has an invalid fee.")
    if isinstance(value, int):
        fee = value
    elif isinstance(value, float) and value.is_integer():
        fee = int(value)
    else:
        raise ToolFailure("delivery_zones.json has an invalid fee.")
    if fee < 0:
        raise ToolFailure("delivery_zones.json has an invalid fee.")
    return fee
