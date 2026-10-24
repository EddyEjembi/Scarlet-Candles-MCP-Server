"""MCP server for Candle Chord reseller orders.

mcp 2 renamed FastMCP to MCPServer. This file uses that class from the official SDK.
Streamable HTTP is served at /mcp and requires Authorization: Bearer MCP_AUTH_TOKEN.
POST /paystack/webhook is a custom route, so the SDK does not apply that bearer check.
The webhook is authenticated with the Paystack signature instead.
"""

import asyncio

from dotenv import load_dotenv
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from core import delivery, orders, payments, resellers, stock
from core.config import listen_host, listen_port, public_base_url, require_env
from core.errors import ToolFailure
from core.log import log_tool

INSTRUCTIONS = (
    "You take reseller orders for Scarlet Candles, one Nigerian candle brand. "
    "Only a registered, active reseller can order. Anyone else must be registered by the human team first. "
    "Each order is one SKU. To sell two scents, lock and pay them as two orders. "
    "Look up the reseller by WhatsApp number, list live stock, lock that stock, quote delivery from the city, "
    "create one Paystack link for the candles plus delivery, then confirm payment. "
    "The checkout URL is saved on the order. If it was lost in chat and the order is still awaiting payment, "
    "call create_payment_link again and send that same URL. "
    "Do not invent stock, prices, or a delivery fee. If the city is not supported, ask for a supported city "
    "or the nearest one. If they are unsure, hand the chat to the Scarlet team. "
    "Do not promise custom scents, private label, bulk pricing, discounts, refunds, or a change of address after dispatch. "
    "Nothing ships from these tools. Payment must be confirmed first, and dispatch is handled by the team. "
    "A lock lasts LOCK_MINUTES. It is released on the next tool call after that time, not by a background job. "
    "If confirm_payment says the order is already paid, leave stock alone."
)


class StaticBearerVerifier:
    """Accept only the shared MCP_AUTH_TOKEN."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token when the bearer value matches, otherwise None."""
        import hmac

        if not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(token=token, client_id="candle-chord-agent", scopes=[])


def build_server() -> MCPServer:
    """Create the MCP server, register the seven tools, and add the Paystack webhook."""
    token = require_env("MCP_AUTH_TOKEN")
    base = public_base_url()
    # validate_token_resource is false because this is a shared bearer token, not an OAuth audience.
    auth = AuthSettings(
        issuer_url=base,
        resource_server_url=base,
        required_scopes=[],
        validate_token_resource=False,
    )
    mcp = MCPServer(
        "Scarlet Candles",
        instructions=INSTRUCTIONS,
        version="1.0.0",
        auth=auth,
        token_verifier=auth_verifier(token),
    )
    _register_tools(mcp)
    _register_ops_routes(mcp)
    return mcp


def auth_verifier(token: str) -> TokenVerifier:
    """Build the bearer checker. Separated so the type matches TokenVerifier."""
    return StaticBearerVerifier(token)


def _register_tools(mcp: MCPServer) -> None:
    """Register the seven order tools. Each one is a thin wrapper over a core function."""

    @mcp.tool(structured_output=False)
    def get_reseller(whatsapp_number: str) -> dict[str, object]:
        """Look up one reseller by WhatsApp number.

        Call this before locking stock. Returns reseller_id, name, email, status, and
        lifetime_total when the reseller is registered and active. Returns ok false when
        the number is not registered or the reseller is inactive.
        """
        return resellers.get_reseller(whatsapp_number)

    @mcp.tool(structured_output=False)
    def list_stock() -> dict[str, object]:
        """List candle SKUs with name, price in naira, and available quantity.

        Available quantity is on_hand minus reserved. Do not invent a SKU or a quantity
        when this returns ok false.
        """
        return stock.list_stock()

    @mcp.tool(structured_output=False)
    def lock_stock(reseller_id: str, sku: str, qty: int) -> dict[str, object]:
        """Lock stock for one SKU and create an order with status LOCKED.

        The lock lasts LOCK_MINUTES (default 120). Fails when qty is below 1, the reseller
        is unknown or inactive, the SKU is unknown, or qty is above the available quantity.
        """
        return stock.lock_stock(reseller_id, sku, qty)

    @mcp.tool(structured_output=False)
    def quote_delivery(order_id: str, address: str, city: str) -> dict[str, object]:
        """Quote delivery for a locked order and set status to AWAITING_PAYMENT.

        The city must be one of the supported cities. An unknown city returns ok false and
        does not save a fee. Never guess a delivery fee.
        """
        return delivery.quote_delivery(order_id, address, city)

    @mcp.tool(structured_output=False)
    def create_payment_link(order_id: str) -> dict[str, object]:
        """Create a Paystack checkout link for an order that is awaiting payment.

        Amount is goods plus delivery, in kobo. The Paystack reference is the order id.
        If a link was already created and the order is still awaiting payment, the same link is returned.
        """
        return payments.create_payment_link(order_id)

    @mcp.tool(structured_output=False)
    def confirm_payment(order_id: str) -> dict[str, object]:
        """Verify the Paystack payment and mark the order PAID when the charge succeeded.

        On success, on_hand and reserved decrease and the reseller lifetime total increases.
        An already paid order returns ok true and does not change stock or totals again.
        An unpaid order returns ok false and changes nothing.
        """
        return payments.confirm_payment(order_id)

    @mcp.tool(structured_output=False)
    def get_order(order_id: str) -> dict[str, object]:
        """Return the current status and saved details for one order."""
        return orders.get_order(order_id)


def _register_ops_routes(mcp: MCPServer) -> None:
    """Public health check and Paystack webhook. Both sit outside bearer auth."""

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health_check(_request: Request) -> Response:
        """Simple liveness probe for Render and load balancers."""
        return JSONResponse({"status": "ok", "service": "scarlet-candles-mcp"})

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def root(_request: Request) -> Response:
        """Human-readable tip when someone opens the service URL in a browser."""
        return PlainTextResponse(
            "Scarlet Candles MCP. Tools at /mcp. Health at /health. Paystack webhook at /paystack/webhook.\n"
        )

    @mcp.custom_route("/paystack/webhook", methods=["POST"], include_in_schema=False)
    async def paystack_webhook(request: Request) -> Response:
        """Verify the Paystack signature and confirm the matching order."""
        raw = await request.body()
        signature = request.headers.get("x-paystack-signature", "")
        try:
            status = await asyncio.to_thread(payments.handle_paystack_webhook, raw, signature)
        except Exception as exc:
            log_tool("paystack_webhook", {}, {"ok": False, "error": str(exc)})
            return Response(status_code=500)
        return Response(status_code=status)


def main() -> None:
    """Load .env and serve Streamable HTTP. Host and port come from the environment."""
    load_dotenv()
    try:
        host = listen_host()
        port = listen_port()
        server = build_server()
    except ToolFailure as exc:
        raise SystemExit(str(exc)) from exc
    print("Listening on http://" + host + ":" + str(port) + "  public=" + public_base_url(), flush=True)
    server.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
