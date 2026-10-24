"""Core tests. Sheets and Paystack are fakes. No MCP client and no live network."""

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Iterator

import pytest

from core.delivery import quote_delivery
from core.payments import confirm_payment, create_payment_link, handle_paystack_webhook
from core.orders import get_order
from core.sheets import INVENTORY_TAB, ORDERS_TAB, RESELLERS_TAB, MemorySheetStore, set_store
from core.stock import list_stock, lock_stock
from seed import seed_store
from server import build_server


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        """Return the canned Paystack body."""
        return self._payload


@pytest.fixture(autouse=True)
def sheets(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemorySheetStore]:
    """Point every test at a freshly seeded in-memory spreadsheet."""
    monkeypatch.setenv("BRAND_NAME", "Scarlet Candles")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("LOCK_MINUTES", "120")
    store = MemorySheetStore()
    seed_store(store)
    set_store(store)
    yield store
    set_store(None)


def test_lock_more_than_available(sheets: MemorySheetStore) -> None:
    """Reject a lock above available stock and leave reserved unchanged."""
    result = lock_stock("RSL-001", "SC-005", 9)
    assert result["ok"] is False
    assert "Not enough stock" in str(result["error"])
    assert sheets.read_tab(ORDERS_TAB) == []
    amber = _inventory(sheets, "SC-005")
    assert amber["on_hand"] == "8"
    assert amber["reserved"] == "0"


def test_lock_expiry_releases_reserved(sheets: MemorySheetStore) -> None:
    """A later tool call expires a lock that is past locked_until and frees reserved stock."""
    locked = lock_stock("RSL-001", "SC-001", 2)
    assert locked["ok"] is True
    order_id = str(locked["order_id"])
    sheets.update_row(ORDERS_TAB, "order_id", order_id, {"locked_until": "2000-01-01T00:00:00+00:00"})
    listed = list_stock()
    assert listed["ok"] is True
    rose = _inventory(sheets, "SC-001")
    assert rose["reserved"] == "0"
    assert rose["on_hand"] == "20"
    fetched = get_order(order_id)
    assert fetched["ok"] is True
    order = fetched["order"]
    assert isinstance(order, dict)
    assert order["status"] == "EXPIRED"


def test_unknown_city(sheets: MemorySheetStore) -> None:
    """An unknown city is refused and the order stays locked with no fee."""
    locked = lock_stock("RSL-001", "SC-001", 1)
    assert locked["ok"] is True
    order_id = str(locked["order_id"])
    quoted = quote_delivery(order_id, "12 Marina Road", "Atlantis")
    assert quoted["ok"] is False
    assert "Atlantis" in str(quoted["error"])
    assert "Lagos" in str(quoted["error"])
    fetched = get_order(order_id)
    order = fetched["order"]
    assert isinstance(order, dict)
    assert order["status"] == "LOCKED"
    assert order["city"] == ""
    assert order["delivery_fee"] is None
    assert order["total"] is None


def test_double_confirm_does_not_double_count(monkeypatch: pytest.MonkeyPatch, sheets: MemorySheetStore) -> None:
    """A second confirm of a paid order does not change stock or the lifetime total."""
    order_id, total = _lock_and_quote()
    monkeypatch.setattr("core.payments.requests.get", _success_verify(order_id, total))
    first = confirm_payment(order_id)
    second = confirm_payment(order_id)
    assert first["ok"] is True
    assert first["status"] == "PAID"
    assert second["ok"] is True
    assert second["already_paid"] is True
    rose = _inventory(sheets, "SC-001")
    assert rose["on_hand"] == "18"
    assert rose["reserved"] == "0"
    assert _lifetime(sheets, "RSL-001") == str(total)


def test_confirm_accepts_paystack_fee_on_customer(monkeypatch: pytest.MonkeyPatch, sheets: MemorySheetStore) -> None:
    """A successful charge above the order total (customer-paid fee) still marks the order PAID."""
    order_id, total = _lock_and_quote()
    # 1.5% + 100 naira fee on 14000 would land above the goods+delivery amount.
    monkeypatch.setattr("core.payments.requests.get", _success_verify(order_id, total, paid_extra_naira=315))
    result = confirm_payment(order_id)
    assert result["ok"] is True
    assert result["status"] == "PAID"
    rose = _inventory(sheets, "SC-001")
    assert rose["on_hand"] == "18"
    assert rose["reserved"] == "0"
    assert _lifetime(sheets, "RSL-001") == str(total)


def test_confirm_rejects_underpayment(monkeypatch: pytest.MonkeyPatch, sheets: MemorySheetStore) -> None:
    """A successful Paystack charge below the order total does not fulfill the order."""
    order_id, total = _lock_and_quote()
    monkeypatch.setattr("core.payments.requests.get", _success_verify(order_id, total - 1))
    result = confirm_payment(order_id)
    assert result["ok"] is False
    assert "less than this order total" in str(result["error"])
    rose = _inventory(sheets, "SC-001")
    assert rose["on_hand"] == "20"
    assert rose["reserved"] == "2"
    fetched = get_order(order_id)
    order = fetched["order"]
    assert isinstance(order, dict)
    assert order["status"] == "AWAITING_PAYMENT"


def test_unpaid_confirm_changes_nothing(monkeypatch: pytest.MonkeyPatch, sheets: MemorySheetStore) -> None:
    """An unfinished Paystack charge leaves stock, status, and lifetime total alone."""
    order_id, total = _lock_and_quote()
    monkeypatch.setattr("core.payments.requests.get", _pending_verify())
    result = confirm_payment(order_id)
    assert result["ok"] is False
    assert "not been completed" in str(result["error"])
    rose = _inventory(sheets, "SC-001")
    assert rose["on_hand"] == "20"
    assert rose["reserved"] == "2"
    fetched = get_order(order_id)
    order = fetched["order"]
    assert isinstance(order, dict)
    assert order["status"] == "AWAITING_PAYMENT"
    assert _lifetime(sheets, "RSL-001") == "0"
    assert total == 12500 * 2 + 1500


def test_create_payment_link_returns_the_same_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second call returns the saved URL and does not call Paystack again."""
    order_id, total = _lock_and_quote()
    calls = {"count": 0}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls["count"] += 1
        return FakeResponse(
            {
                "status": True,
                "message": "Authorization URL created",
                "data": {
                    "authorization_url": "https://checkout.paystack.com/demo",
                    "reference": order_id,
                },
            }
        )

    monkeypatch.setattr("core.payments.requests.post", fake_post)
    first = create_payment_link(order_id)
    second = create_payment_link(order_id)
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["payment_url"] == "https://checkout.paystack.com/demo"
    assert second["payment_url"] == first["payment_url"]
    assert first["amount_kobo"] == total * 100
    assert calls["count"] == 1


def test_expired_order_does_not_hand_out_the_old_link(sheets: MemorySheetStore) -> None:
    """After the lock expires, the saved checkout URL is not sent again."""
    order_id, _total = _lock_and_quote()
    sheets.update_row(
        ORDERS_TAB,
        "order_id",
        order_id,
        {
            "payment_url": "https://checkout.paystack.com/old",
            "payment_ref": order_id,
            "locked_until": "2000-01-01T00:00:00+00:00",
        },
    )
    result = create_payment_link(order_id)
    assert result["ok"] is False
    assert "expired" in str(result["error"]).lower()
    rose = _inventory(sheets, "SC-001")
    assert rose["reserved"] == "0"


def test_webhook_rejects_a_bad_signature(sheets: MemorySheetStore) -> None:
    """A bad Paystack signature is rejected and does not change the sheet."""
    raw = b'{"event":"charge.success","data":{"reference":"ord_missing"}}'
    status = handle_paystack_webhook(raw, "not-the-signature")
    assert status == 401
    assert sheets.read_tab(ORDERS_TAB) == []


def test_webhook_success_is_idempotent(monkeypatch: pytest.MonkeyPatch, sheets: MemorySheetStore) -> None:
    """charge.success confirms the order once. A retry does not double-count."""
    order_id, total = _lock_and_quote()
    monkeypatch.setattr("core.payments.requests.get", _success_verify(order_id, total))
    raw = json.dumps({"event": "charge.success", "data": {"reference": order_id}}).encode("utf-8")
    signature = _sign(raw)
    assert handle_paystack_webhook(raw, signature) == 200
    assert handle_paystack_webhook(raw, signature) == 200
    rose = _inventory(sheets, "SC-001")
    assert rose["on_hand"] == "18"
    assert rose["reserved"] == "0"
    assert _lifetime(sheets, "RSL-001") == str(total)
    fetched = get_order(order_id)
    order = fetched["order"]
    assert isinstance(order, dict)
    assert order["status"] == "PAID"


def test_server_lists_seven_tools() -> None:
    """The MCP server exposes the seven order tools and can list stock through one of them."""
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = sorted(tool.name for tool in tools)
    assert names == [
        "confirm_payment",
        "create_payment_link",
        "get_order",
        "get_reseller",
        "list_stock",
        "lock_stock",
        "quote_delivery",
    ]
    result = asyncio.run(server.call_tool("list_stock", {}))
    assert result.is_error is False
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    assert "Scarlet Rose" in text
    assert '"ok": true' in text


def _lock_and_quote() -> tuple[str, int]:
    """Lock two Scarlet Rose candles and quote Lagos delivery. Returns order id and naira total."""
    locked = lock_stock("RSL-001", "SC-001", 2)
    assert locked["ok"] is True
    order_id = locked["order_id"]
    assert isinstance(order_id, str)
    quoted = quote_delivery(order_id, "12 Marina Road", "Lagos")
    assert quoted["ok"] is True
    total = quoted["total"]
    assert isinstance(total, int)
    return order_id, total


def _success_verify(order_id: str, total: int, paid_extra_naira: int = 0):
    """Build a Paystack verify fake that reports a successful charge for this total.

    paid_extra_naira simulates a customer-paid Paystack fee on top of the order.
    """

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        assert order_id in url
        return FakeResponse(
            {
                "status": True,
                "data": {
                    "status": "success",
                    "amount": (total + paid_extra_naira) * 100,
                    "currency": "NGN",
                    "reference": order_id,
                },
            }
        )

    return fake_get


def _pending_verify():
    """Build a Paystack verify fake for a charge the customer did not finish."""

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse({"status": True, "data": {"status": "abandoned", "amount": 0, "currency": "NGN"}})

    return fake_get


def _sign(raw: bytes) -> str:
    """HMAC-SHA512 of a webhook body, using the test secret."""
    secret = os.environ["PAYSTACK_SECRET_KEY"].encode("utf-8")
    return hmac.new(secret, raw, hashlib.sha512).hexdigest()


def _inventory(store: MemorySheetStore, sku: str) -> dict[str, str]:
    """Return one inventory row from the memory store."""
    for row in store.read_tab(INVENTORY_TAB):
        if row["sku"] == sku:
            return row
    raise AssertionError("Missing SKU " + sku)


def _lifetime(store: MemorySheetStore, reseller_id: str) -> str:
    """Return the lifetime_total cell for one reseller."""
    for row in store.read_tab(RESELLERS_TAB):
        if row["reseller_id"] == reseller_id:
            return row["lifetime_total"]
    raise AssertionError("Missing reseller " + reseller_id)
