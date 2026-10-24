"""Wipe the three tabs and load demo candles and resellers."""

from dotenv import load_dotenv

from core.errors import ToolFailure
from core.sheets import (
    INVENTORY_TAB,
    ORDERS_TAB,
    RESELLERS_TAB,
    GoogleSheetStore,
    SheetStore,
)


def inventory_rows() -> list[dict[str, str]]:
    """The six Scarlet Candles. Prices are demo wholesale naira. Nothing is reserved.

    The business notes name the range and do not list wholesale prices, so these
    prices are placeholders until Scarlet replaces them. Amber & Oud is the
    smallest batch. Vanilla Noir is the largest, because it is the usual seller.
    """
    raw = [
        ("SC-001", "Scarlet Rose", "12500", "20", "0"),
        ("SC-002", "Vanilla Noir", "11000", "30", "0"),
        ("SC-003", "Lagos Nights", "13500", "14", "0"),
        ("SC-004", "Coconut Cloud", "10000", "16", "0"),
        ("SC-005", "Amber & Oud", "18000", "8", "0"),
        ("SC-006", "Clean Linen", "9500", "22", "0"),
    ]
    rows: list[dict[str, str]] = []
    for sku, name, price, on_hand, reserved in raw:
        rows.append(
            {"sku": sku, "name": name, "price": price, "on_hand": on_hand, "reserved": reserved}
        )
    return rows


def reseller_rows() -> list[dict[str, str]]:
    """Three made-up resellers. The third is inactive so lookup can refuse them."""
    raw = [
        ("RSL-001", "Ada Okonkwo", "+2348011111111", "ada@example.com", "active", "0"),
        ("RSL-002", "Tunde Bakare", "+2348022222222", "tunde@example.com", "active", "15000"),
        ("RSL-003", "Chioma Eze", "+2348033333333", "chioma@example.com", "inactive", "0"),
    ]
    rows: list[dict[str, str]] = []
    for reseller_id, name, whatsapp_number, email, status, lifetime_total in raw:
        rows.append(
            {
                "reseller_id": reseller_id,
                "name": name,
                "whatsapp_number": whatsapp_number,
                "email": email,
                "status": status,
                "lifetime_total": lifetime_total,
            }
        )
    return rows


def seed_store(store: SheetStore) -> None:
    """Replace Inventory, Orders, and Resellers. Orders starts empty."""
    store.replace_tab(INVENTORY_TAB, inventory_rows())
    store.replace_tab(ORDERS_TAB, [])
    store.replace_tab(RESELLERS_TAB, reseller_rows())


def main() -> None:
    """Connect to Google Sheets and re-seed the demo data."""
    load_dotenv()
    try:
        store = GoogleSheetStore.connect_from_env()
        seed_store(store)
    except ToolFailure as exc:
        raise SystemExit(str(exc)) from exc
    print("Seeded Inventory, Orders, and Resellers.")


if __name__ == "__main__":
    main()
