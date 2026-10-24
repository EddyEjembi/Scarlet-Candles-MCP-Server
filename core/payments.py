"""Paystack test checkout links, payment confirmation, and the webhook handler."""

import hashlib
import hmac
import json
from urllib.parse import quote

import requests

from core.config import brand_name, require_env
from core.errors import ToolFailure
from core.log import log_tool, run_tool
from core.orders import (
    STATUS_AWAITING,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_LOCKED,
    STATUS_PAID,
    find_order,
    release_expired_unlocked,
)
from core.resellers import find_reseller_by_id
from core.rows import as_int
from core.sheets import INVENTORY_TAB, ORDERS_TAB, RESELLERS_TAB, get_store, write_lock

INITIALIZE_URL = "https://api.paystack.co/transaction/initialize"
VERIFY_URL = "https://api.paystack.co/transaction/verify/"


def create_payment_link(order_id: str) -> dict[str, object]:
    """Start a Paystack test payment, or return the link already saved on the order.

    Amount is the quoted goods plus delivery, in kobo. The Paystack reference is the order id.
    """

    def body() -> dict[str, object]:
        cleaned = order_id.strip()
        if cleaned == "":
            raise ToolFailure("An order id is required.")
        draft = _prepare_payment(cleaned)
        if draft["existing_url"] != "":
            return _link_result(cleaned, str(draft["existing_url"]), str(draft["reference"]), int(draft["amount_kobo"]))
        try:
            url, reference = _initialize_paystack(draft)
        except ToolFailure:
            existing = _saved_link(cleaned)
            if existing is not None:
                return existing
            raise
        with write_lock:
            release_expired_unlocked()
            existing = _saved_link_unlocked(cleaned)
            if existing is not None:
                return existing
            order = find_order(cleaned)
            if order is None:
                raise ToolFailure("Order was not found.")
            if order.get("status", "") != STATUS_AWAITING:
                raise ToolFailure(_payment_block_message(order.get("status", "")))
            get_store().update_row(
                ORDERS_TAB,
                "order_id",
                cleaned,
                {"payment_url": url, "payment_ref": reference},
            )
        return _link_result(cleaned, url, reference, int(draft["amount_kobo"]))

    return run_tool("create_payment_link", {"order_id": order_id}, body)


def confirm_payment(order_id: str) -> dict[str, object]:
    """Verify the Paystack payment for an order and mark it paid when the charge succeeded.

    An order that is already paid returns success and does not change stock or totals.
    An unpaid charge returns an error and changes nothing.
    """
    return run_tool("confirm_payment", {"order_id": order_id}, lambda: confirm_order(order_id))


def confirm_order(order_id: str) -> dict[str, object]:
    """Confirm one order by its id, which is also the Paystack reference.

    Raises ToolFailure when Sheets or Paystack cannot be reached.
    Business results (unpaid, missing, already paid) are returned as dicts.
    """
    cleaned = order_id.strip()
    if cleaned == "":
        raise ToolFailure("An order id is required.")
    with write_lock:
        release_expired_unlocked()
        order = find_order(cleaned)
        if order is None:
            return {"ok": False, "error": "Order was not found."}
        if order.get("status", "") == STATUS_PAID:
            return {"ok": True, "order_id": cleaned, "status": STATUS_PAID, "already_paid": True}
        if order.get("status", "") != STATUS_AWAITING:
            return {"ok": False, "error": _confirm_block_message(order.get("status", ""))}
        total = as_int(order.get("total", ""), "Order total")
        expected_kobo = total * 100
        reference = order.get("payment_ref", "").strip() or cleaned
        qty = as_int(order.get("qty", ""), "Order quantity")
        sku = order.get("sku", "")
        reseller_id = order.get("reseller_id", "")
    paid_kobo, currency = _fetch_successful_payment(reference)
    if paid_kobo is None:
        return {"ok": False, "error": "Payment has not been completed for this order. Nothing was changed."}
    # Paystack may charge more than the order total when the customer pays the fee.
    # Accept any successful charge that covers the goods + delivery. Reject underpayment.
    if paid_kobo < expected_kobo:
        return {
            "ok": False,
            "error": "The Paystack amount is less than this order total. Nothing was changed.",
        }
    if currency != "" and currency.upper() != "NGN":
        return {"ok": False, "error": "The Paystack currency is not NGN. Nothing was changed."}
    with write_lock:
        release_expired_unlocked()
        order = find_order(cleaned)
        if order is None:
            return {"ok": False, "error": "Order was not found."}
        if order.get("status", "") == STATUS_PAID:
            return {"ok": True, "order_id": cleaned, "status": STATUS_PAID, "already_paid": True}
        if order.get("status", "") != STATUS_AWAITING:
            return {"ok": False, "error": _confirm_block_message(order.get("status", ""))}
        _fulfill(cleaned, sku, qty, reseller_id, total)
    return {"ok": True, "order_id": cleaned, "status": STATUS_PAID, "total": total}


def handle_paystack_webhook(raw_body: bytes, signature: str) -> int:
    """Verify the Paystack signature and confirm the order on charge.success.

    Returns 401 when the signature does not match.
    Returns 500 when Sheets or Paystack could not be reached, so Paystack retries.
    Returns 200 after every other outcome, including an unknown or already paid order.
    """
    if not _signature_matches(raw_body, signature):
        log_tool(
            "paystack_webhook",
            {"signature": "mismatch"},
            {"ok": False, "error": "Invalid Paystack signature."},
        )
        return 401
    try:
        parsed: object = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log_tool("paystack_webhook", {}, {"ok": True, "ignored": "malformed body"})
        return 200
    if not isinstance(parsed, dict) or parsed.get("event") != "charge.success":
        log_tool("paystack_webhook", {"event": _event_name(parsed)}, {"ok": True, "ignored": True})
        return 200
    data = parsed.get("data")
    reference = ""
    if isinstance(data, dict) and isinstance(data.get("reference"), str):
        reference = data["reference"].strip()
    if reference == "":
        log_tool("paystack_webhook", {"event": "charge.success"}, {"ok": True, "ignored": "missing reference"})
        return 200
    try:
        result = confirm_order(reference)
    except ToolFailure as exc:
        log_tool("paystack_webhook", {"reference": reference}, {"ok": False, "error": str(exc)})
        return 500 if exc.retryable else 200
    log_tool("paystack_webhook", {"reference": reference, "event": "charge.success"}, result)
    return 200


def _prepare_payment(order_id: str) -> dict[str, object]:
    """Read the order and reseller fields needed to call Paystack. Holds the sheet lock."""
    with write_lock:
        release_expired_unlocked()
        order = find_order(order_id)
        if order is None:
            raise ToolFailure("Order was not found.")
        existing_url = order.get("payment_url", "").strip()
        existing_ref = order.get("payment_ref", "").strip() or order_id
        # The saved URL is only reusable while the lock is still awaiting payment.
        if order.get("status", "") == STATUS_AWAITING and existing_url != "":
            total = as_int(order.get("total", ""), "Order total")
            return {
                "existing_url": existing_url,
                "reference": existing_ref,
                "amount_kobo": total * 100,
                "email": "",
                "reseller_name": "",
                "order_id": order_id,
            }
        if order.get("status", "") != STATUS_AWAITING:
            raise ToolFailure(_payment_block_message(order.get("status", "")))
        total = as_int(order.get("total", ""), "Order total")
        reseller = find_reseller_by_id(order.get("reseller_id", ""))
        if reseller is None:
            raise ToolFailure("Reseller was not found.")
        email = reseller.get("email", "").strip()
        if email == "":
            raise ToolFailure("This reseller has no email, so Paystack cannot start a payment.")
        return {
            "existing_url": "",
            "reference": order_id,
            "amount_kobo": total * 100,
            "email": email,
            "reseller_name": reseller.get("name", ""),
            "order_id": order_id,
        }


def _initialize_paystack(draft: dict[str, object]) -> tuple[str, str]:
    """Call Paystack initialize. Returns the checkout URL and the reference."""
    secret = require_env("PAYSTACK_SECRET_KEY")
    brand = brand_name()
    reseller_name = str(draft["reseller_name"])
    order_id = str(draft["order_id"])
    body = {
        "email": str(draft["email"]),
        "amount": int(draft["amount_kobo"]),
        "reference": order_id,
        "currency": "NGN",
        "metadata": {
            "reseller_name": reseller_name,
            "brand_name": brand,
            "order_id": order_id,
            "description": brand + " order for " + reseller_name,
        },
    }
    try:
        response = requests.post(
            INITIALIZE_URL,
            json=body,
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ToolFailure("Could not reach Paystack to create a payment link. Nothing was changed.", retryable=True) from exc
    payload = _read_json(response)
    if response.status_code in (401, 403):
        raise ToolFailure("Paystack rejected the secret key. Nothing was changed.")
    if response.status_code >= 500:
        raise ToolFailure("Paystack could not create a payment link. Nothing was changed.", retryable=True)
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise ToolFailure(_paystack_message(payload) + " Nothing was changed.")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ToolFailure("Paystack returned an unreadable payment link. Nothing was changed.")
    url = data.get("authorization_url")
    reference = data.get("reference")
    if not isinstance(url, str) or url.strip() == "":
        raise ToolFailure("Paystack returned an unreadable payment link. Nothing was changed.")
    saved_reference = reference.strip() if isinstance(reference, str) and reference.strip() != "" else order_id
    return url.strip(), saved_reference


def _fetch_successful_payment(reference: str) -> tuple[int | None, str]:
    """Return kobo amount and currency when Paystack reports success, else (None, "")."""
    secret = require_env("PAYSTACK_SECRET_KEY")
    url = VERIFY_URL + quote(reference, safe="")
    try:
        response = requests.get(url, headers={"Authorization": "Bearer " + secret}, timeout=30)
    except requests.RequestException as exc:
        raise ToolFailure(
            "Could not reach Paystack to verify the payment. Nothing was changed.",
            retryable=True,
        ) from exc
    if response.status_code in (401, 403):
        raise ToolFailure("Paystack rejected the secret key. Nothing was changed.")
    if response.status_code >= 500:
        raise ToolFailure("Paystack could not verify the payment. Nothing was changed.", retryable=True)
    payload = _read_json(response)
    if not isinstance(payload, dict) or payload.get("status") is not True:
        return None, ""
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("status") != "success":
        return None, ""
    amount = _as_kobo(data.get("amount"))
    if amount is None:
        raise ToolFailure("Paystack returned an unreadable amount. Nothing was changed.")
    currency = data.get("currency")
    currency_text = currency.strip() if isinstance(currency, str) else ""
    return amount, currency_text


def _fulfill(order_id: str, sku: str, qty: int, reseller_id: str, total: int) -> None:
    """Decrease stock and add the total to the reseller. Caller holds write_lock.

    Status is written last. Sheets cannot do this in one transaction, so a crash
    after the stock update and before the status update can apply the stock change
    again on retry. The happy path checks status first and will not double-count
    an order that is already PAID.
    """
    store = get_store()
    inventory_rows = store.read_tab(INVENTORY_TAB)
    item = None
    for row in inventory_rows:
        if row.get("sku", "") == sku:
            item = row
            break
    if item is None:
        raise ToolFailure("SKU was not found while confirming payment. Nothing was changed.")
    on_hand = as_int(item.get("on_hand", ""), "On-hand stock")
    reserved = as_int(item.get("reserved", ""), "Reserved stock")
    if on_hand < qty or reserved < qty:
        raise ToolFailure("Reserved stock for this order is no longer held. Nothing was changed.")
    reseller = find_reseller_by_id(reseller_id)
    if reseller is None:
        raise ToolFailure("Reseller was not found while confirming payment. Nothing was changed.")
    lifetime = as_int(reseller.get("lifetime_total", ""), "Reseller lifetime total")
    store.update_row(
        INVENTORY_TAB,
        "sku",
        sku,
        {"on_hand": str(on_hand - qty), "reserved": str(reserved - qty)},
    )
    store.update_row(RESELLERS_TAB, "reseller_id", reseller_id, {"lifetime_total": str(lifetime + total)})
    store.update_row(ORDERS_TAB, "order_id", order_id, {"status": STATUS_PAID})


def _saved_link(order_id: str) -> dict[str, object] | None:
    """Return the saved payment link when one exists. Holds the sheet lock."""
    with write_lock:
        release_expired_unlocked()
        return _saved_link_unlocked(order_id)


def _saved_link_unlocked(order_id: str) -> dict[str, object] | None:
    """Return the saved payment link. Caller holds write_lock."""
    order = find_order(order_id)
    if order is None or order.get("status", "") != STATUS_AWAITING:
        return None
    if order.get("payment_url", "").strip() == "":
        return None
    total = as_int(order.get("total", ""), "Order total")
    reference = order.get("payment_ref", "").strip() or order_id
    return _link_result(order_id, order["payment_url"].strip(), reference, total * 100)


def _link_result(order_id: str, url: str, reference: str, amount_kobo: int) -> dict[str, object]:
    """Shape returned by create_payment_link."""
    return {
        "ok": True,
        "order_id": order_id,
        "payment_url": url,
        "reference": reference,
        "amount_kobo": amount_kobo,
    }


def _signature_matches(raw_body: bytes, signature: str) -> bool:
    """True when x-paystack-signature is the HMAC-SHA512 of the raw body."""
    secret = require_env("PAYSTACK_SECRET_KEY").encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    given = signature.strip().lower()
    if given == "":
        return False
    return hmac.compare_digest(digest, given)


def _read_json(response: requests.Response) -> object:
    """Parse a Paystack response body. A bad body is an error, not a guess."""
    try:
        parsed: object = response.json()
    except ValueError as exc:
        raise ToolFailure("Paystack returned an unreadable response. Nothing was changed.") from exc
    return parsed


def _paystack_message(payload: object) -> str:
    """Use Paystack's message when it is plain text."""
    if isinstance(payload, dict) and isinstance(payload.get("message"), str) and payload["message"].strip() != "":
        return payload["message"].strip()
    return "Paystack could not create a payment link."


def _as_kobo(value: object) -> int | None:
    """Read a Paystack subunit amount. Returns None when it is not a whole number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _event_name(payload: object) -> str:
    """Event name for the log, or a blank string."""
    if isinstance(payload, dict) and isinstance(payload.get("event"), str):
        return payload["event"]
    return ""


def _payment_block_message(status: str) -> str:
    """Why a payment link cannot be created for this status."""
    if status == STATUS_LOCKED:
        return "Quote delivery before creating a payment link."
    if status == STATUS_EXPIRED:
        return "This order has expired. Lock the stock again."
    if status == STATUS_PAID:
        return "This order is already paid."
    if status == STATUS_CANCELLED:
        return "This order is cancelled."
    return "This order is " + status + " and cannot be paid."


def _confirm_block_message(status: str) -> str:
    """Why payment confirmation will not change this order."""
    if status == STATUS_LOCKED:
        return "Quote delivery before confirming payment. Nothing was changed."
    if status == STATUS_EXPIRED:
        return "This order has expired, so stock was not changed. If the customer was charged, refund the payment in Paystack."
    if status == STATUS_CANCELLED:
        return "This order is cancelled. Nothing was changed."
    return "This order is " + status + " and cannot be confirmed. Nothing was changed."
