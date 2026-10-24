# otCandle Chord order tools

Python backend for Scarlet Candles reseller orders. An agent calls seven tools to look up a reseller, list stock, lock stock, quote delivery, create a Paystack payment link, and confirm the payment. Google Sheets holds the data.

This project uses `MCPServer`. Streamable HTTP is still served at `/mcp`.

## Layout

- `core/` plain worker functions.
- `server.py` thin MCP wrappers, bearer auth, and `POST /paystack/webhook`.
- `seed.py` wipes and reloads the three tabs.
- `data/delivery_zones.json` delivery fees in naira. Unknown cities are refused.
- `tests/` call the core functions with an in-memory sheet and a fake Paystack.



## Sheet

One spreadsheet, three tabs.

Inventory: `sku`, `name`, `price`, `on_hand`, `reserved`. Available = on_hand - reserved. Prices are naira. Seed stock is the real Scarlet range (SC-001 Scarlet Rose through SC-006 Clean Linen). The prices are demo wholesale amounts.

Orders: `order_id`, `reseller_id`, `sku`, `qty`, `status`, `locked_until`, `address`, `city`, `delivery_fee`, `total`, `payment_ref`, `payment_url`, `created_at`.

`payment_url` is an extra column to make the transaction idempotent. So a second `create_payment_link` call can return the same Paystack URL. Paystack will not mint that URL again for the same reference.

Resellers: `reseller_id`, `name`, `whatsapp_number`, `email`, `status`, `lifetime_total`.

Status moves `LOCKED` to `AWAITING_PAYMENT` to `PAID`. A lock past `locked_until` becomes `EXPIRED` the next time any tool runs, and reserved stock is released. Orders still waiting for payment expire the same way, because they still hold reserved stock. There is no background job.

Writes in this process share one `threading.Lock`. Google Sheets is not transactional. A crash between two cell updates can leave a row half-updated.

## Paystack

The charge is `(goods + delivery) * 100` kobo, currency NGN, reference = order id. The reseller email is the customer email. Metadata includes the reseller name and `BRAND_NAME`. Confirmation accepts a Paystack amount that is at least the order total, so customer-paid transaction fees still mark the order PAID. An underpayment is refused.

`POST /paystack/webhook` checks `x-paystack-signature` as HMAC-SHA512 of the raw body. A bad signature returns 401. `charge.success` calls the same confirm path as `confirm_payment`. Unknown references and orders that are already paid return 200 and change nothing. Sheets or Paystack being unreachable returns 500 so Paystack retries.

The webhook is a custom route, so the MCP bearer token is not required there.

## Setup

```text
copy .env.example .env
```

Fill in:

- `GOOGLE_SERVICE_ACCOUNT_JSON` path to the service account file, or the JSON itself
- `SHEET_ID`
- `PAYSTACK_SECRET_KEY`
- `BRAND_NAME`
- `MCP_AUTH_TOKEN`
- `LOCK_MINUTES` optional, default 120

The spreadsheet must already exist. `seed.py` creates any missing tab.

```text
.venv\Scripts\python.exe seed.py
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe server.py
```

The server listens on `http://127.0.0.1:8000/mcp`. Callers send `Authorization: Bearer <MCP_AUTH_TOKEN>`.

## Manual end-to-end test (before the agent)

`manual_order.py` is an MCP client. It talks to a running `server.py` and calls the same tools the agent will call. It does not bypass the tools.

```text
python server.py

python manual_order.py
```

Open the printed checkout URL and pay with a Paystack TEST card. Pass `--confirm` if you want the script to also call `confirm_payment` while waiting (useful before ngrok is set up).

Demo resellers after seeding:

- Ada Okonkwo, `+2348011111111`, active
- Tunde Bakare, `+2348022222222`, active
- Chioma Eze, `+2348033333333`, inactive

Supported delivery cities: Lagos, Abuja, Port Harcourt, Ibadan, Kano, Enugu.

