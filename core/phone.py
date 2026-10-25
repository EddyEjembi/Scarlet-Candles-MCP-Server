"""Normalize Nigerian WhatsApp numbers for reseller lookup."""

from core.errors import ToolFailure


def normalize_whatsapp(raw: str) -> str:
    """Turn a Nigerian WhatsApp number into +234XXXXXXXXXX.

    Accepted input shapes (spaces and dashes are ignored):
    - +2348011111111
    - 2348011111111
    - 08011111111

    A bare 10-digit national number (8011111111) is also accepted.
    Anything else returns a clear error; we do not invent a country code.
    """
    text = raw.strip()
    if text == "":
        raise ToolFailure("A WhatsApp number is required.")

    digits = "".join(ch for ch in text if ch.isdigit())
    if digits == "":
        raise ToolFailure(
            "That WhatsApp number is not valid. Use a Nigerian number like +2348011111111, 2348011111111, or 08011111111."
        )

    # +234… / 234… → 13 digits starting with 234
    if digits.startswith("234") and len(digits) == 13:
        return "+" + digits

    # 080… → 11 digits starting with 0
    if digits.startswith("0") and len(digits) == 11:
        return "+234" + digits[1:]

    # 80… → 10-digit national number without the leading 0
    if len(digits) == 10 and digits[0] in "789":
        return "+234" + digits

    raise ToolFailure(
        "That WhatsApp number is not valid. Use a Nigerian number like +2348011111111, 2348011111111, or 08011111111."
    )
