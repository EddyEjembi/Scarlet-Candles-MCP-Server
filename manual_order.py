"""Manual end-to-end test through the MCP tools on a running server.py.

This script does not call core/ directly. It connects to http://127.0.0.1:8000/mcp
with MCP_AUTH_TOKEN and calls the same tools the agent will call.

You must start the server yourself. The webhook also lives on that server.

Setup (three terminals):

  1. python server.py
  2. ngrok http 8000
     In Paystack Dashboard (TEST mode) set the webhook URL to:
       https://YOUR-NGROK-HOST/paystack/webhook
  3. python manual_order.py

Then open the printed Paystack TEST checkout URL and pay.
If the webhook is set, the order should become PAID without --confirm.
Pass --confirm to also call the confirm_payment tool while waiting
(useful when ngrok is not set up yet).

"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import CallToolResult, TextContent


def parse_args() -> argparse.Namespace:
    """CLI flags for a one-SKU demo order through the MCP tools."""
    parser = argparse.ArgumentParser(description="Call Scarlet Candles MCP tools end to end.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp", help="MCP Streamable HTTP URL.")
    parser.add_argument("--whatsapp", default="+2348011111111", help="Reseller WhatsApp number.")
    parser.add_argument("--sku", default="SC-001", help="SKU to lock.")
    parser.add_argument("--qty", type=int, default=1, help="Quantity to lock.")
    parser.add_argument("--address", default="12 Marina Road", help="Customer delivery address.")
    parser.add_argument("--city", default="Lagos", help="City used for the delivery fee.")
    parser.add_argument(
        "--wait",
        type=int,
        default=180,
        help="Seconds to poll get_order after printing the payment link. 0 skips waiting.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Also call confirm_payment while waiting. Use when the webhook is not set up yet.",
    )
    parser.add_argument("--poll", type=int, default=5, help="Seconds between polls while waiting for PAID.")
    return parser.parse_args()


def show(label: str, payload: dict[str, object]) -> None:
    """Print one tool result. Exit when ok is false."""
    print()
    print("=== " + label + " ===")
    print(json.dumps(payload, indent=2, default=str))
    if payload.get("ok") is not True:
        raise SystemExit(1)


def parse_tool_result(result: CallToolResult) -> dict[str, object]:
    """Read the JSON dict our tools return inside the MCP text content."""
    if result.is_error:
        texts = [block.text for block in result.content if isinstance(block, TextContent)]
        raise SystemExit("Tool error: " + (" ".join(texts) if texts else "unknown"))
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if not texts:
        raise SystemExit("Tool returned no text content.")
    parsed: object = json.loads(texts[0])
    if not isinstance(parsed, dict):
        raise SystemExit("Tool returned something that is not a JSON object.")
    typed: dict[str, object] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            raise SystemExit("Tool returned a JSON object with a non-string key.")
        typed[key] = value
    return typed


async def call_tool(client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, object]:
    """Call one MCP tool and return the ok/error dict."""
    result = await client.call_tool(name, arguments or {})
    return parse_tool_result(result)


async def run(args: argparse.Namespace) -> None:
    """Connect to server.py and walk the full order path through the tools."""
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if token == "":
        raise SystemExit("MCP_AUTH_TOKEN is not set in .env")

    print("Connecting to " + args.url)
    print("server.py must already be running. Webhook needs ngrok pointed at that same server.")

    http = create_mcp_http_client(headers={"Authorization": "Bearer " + token})
    async with http:
        transport = streamable_http_client(args.url, http_client=http)
        async with Client(transport) as client:
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print("Tools: " + ", ".join(names))
            if len(names) != 7:
                raise SystemExit("Expected 7 tools from server.py.")

            reseller = await call_tool(client, "get_reseller", {"whatsapp_number": args.whatsapp})
            show("get_reseller", reseller)
            reseller_data = reseller["reseller"]
            if not isinstance(reseller_data, dict):
                raise SystemExit("get_reseller did not return a reseller object.")
            reseller_id = str(reseller_data["reseller_id"])

            show("list_stock", await call_tool(client, "list_stock"))

            locked = await call_tool(
                client,
                "lock_stock",
                {"reseller_id": reseller_id, "sku": args.sku, "qty": args.qty},
            )
            show("lock_stock", locked)
            order_id = str(locked["order_id"])

            quoted = await call_tool(
                client,
                "quote_delivery",
                {"order_id": order_id, "address": args.address, "city": args.city},
            )
            show("quote_delivery", quoted)

            payment = await call_tool(client, "create_payment_link", {"order_id": order_id})
            show("create_payment_link", payment)

            print()
            print("Open this Paystack TEST checkout URL and pay:")
            print(payment["payment_url"])
            print()
            print("order_id = " + order_id)
            print("If server.py + ngrok + Paystack webhook are set, the webhook should mark this PAID.")
            if args.confirm:
                print("This run will also call the confirm_payment tool while waiting.")
            else:
                print("Pass --confirm to also call confirm_payment while waiting.")

            if args.wait <= 0:
                print("Done. Check later with get_order or the sheet.")
                return

            deadline = time.time() + args.wait
            while time.time() < deadline:
                if args.confirm:
                    confirmed = await call_tool(client, "confirm_payment", {"order_id": order_id})
                    print()
                    print("=== confirm_payment ===")
                    print(json.dumps(confirmed, indent=2, default=str))
                    if confirmed.get("ok") is True and confirmed.get("status") == "PAID":
                        print()
                        print("Order is PAID.")
                        return

                current = await call_tool(client, "get_order", {"order_id": order_id})
                print()
                print("=== get_order ===")
                print(json.dumps(current, indent=2, default=str))
                order = current.get("order")
                if isinstance(order, dict) and order.get("status") == "PAID":
                    print()
                    print("Order is PAID.")
                    return

                remaining = int(deadline - time.time())
                print(
                    "Still waiting for PAID. "
                    + str(max(0, remaining))
                    + "s left. Sleeping "
                    + str(args.poll)
                    + "s..."
                )
                await asyncio.sleep(max(1, args.poll))

            print()
            print("Timed out waiting for PAID. Pay the link, check ngrok/webhook, or re-run with --confirm.")
            raise SystemExit(2)


def main() -> None:
    """Load .env and run the MCP client."""
    load_dotenv()
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print()
        print("Stopped.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
