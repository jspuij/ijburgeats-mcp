# ijburgeats-mcp

An [MCP](https://modelcontextprotocol.io) server for ordering food delivery from
[IJburg Eats](https://ijburgeats.nl) in Amsterdam. It lets an AI assistant (Claude
Code, Claude Desktop, OpenClaw, or any MCP client) browse the restaurants, build a
cart, reorder from your order history and place the order. You pay through a
link you open yourself.

```
You:     Order the same as last Friday, as soon as possible, with iDEAL.
Claude:  I put that order back in the cart: Spicy Tuna Roll, Edamame Beans and
         a Poké bowl with quinoa. Delivery ASAP to your address, total €38.15
         incl. €0.35 iDEAL fee. Shall I place it?
You:     Yes.
Claude:  Order placed. Pay here to send it to the kitchen: https://pay.multisafepay.com/…
```

> **Unofficial.** IJburg Eats has no public API. This server uses the same
> internal endpoints as the website's own JavaScript, which runs on the
> [Cashdesk](https://www.cashdesk.nl) webshop platform. The endpoints are
> undocumented and can change at any time. This project is not affiliated with
> IJburg Eats or Cashdesk. Use it for your own orders, at a human pace.

## Features

- **Browse:** every IJburg Eats restaurant, its menu, and full-text search across all menus
- **Product options:** required choices (side dish, base) and optional extras, with surcharges
- **Cart:** add, change quantities, remove, clear
- **Order history and reorder** (with an account): your past orders, and one call to put a past order
  back in the cart. Items are matched by name against today's menu, so dishes that disappeared are reported
  instead of guessed.
- **Delivery time:** as soon as possible or a specific slot
- **Checkout with guard rails:**
  - `review_order` shows exactly what will be ordered
  - `place_order` only succeeds at the total that was reviewed
  - payment methods can be restricted in the config

## How payment works

Ordering itself can be automated, but paying can't: iDEAL and card payments go
through a MultiSafepay page. `place_order` returns that payment link. You open
it in your browser and pay with your bank app, which is your final confirmation.
The order is sent to the restaurant once paid.

Cash on delivery (`WINKEL_Cash`) needs no payment step at all, so it would let
the assistant place an order nobody confirmed. If the server is reachable by
more people or reads untrusted content (web search, messages), restrict it:

```toml
[policy]
allowed_payments = ["MSP_Ideal", "MSP_MASTERCARD", "MSP_VISA"]
```

A disallowed method is refused by the server before checkout is contacted, and
it is hidden from the payment options the model sees.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/jspuij/ijburgeats-mcp.git
cd ijburgeats-mcp
uv sync
```

### Configuration

```bash
mkdir -p ~/.config/ijburgeats
cp config.example.toml ~/.config/ijburgeats/config.toml
chmod 600 ~/.config/ijburgeats/config.toml
```

Fill in your details. Set `IJBURGEATS_CONFIG` to use a different path.

| Section | Required | Used for |
|---|---|---|
| `[customer]` | yes | Name, contact details and delivery address for the checkout form; the postcode decides which restaurants deliver |
| `[account]` | no | Your ijburgeats.nl login: orders land in your account, and `get_order_history` and `reorder` work |
| `[policy]` | no | `allowed_payments`, see above |

### Claude Code

```bash
claude mcp add ijburgeats -s user -- uv --directory /path/to/ijburgeats-mcp run ijburgeats-mcp
```

### Claude Desktop and other MCP clients

```json
{
  "mcpServers": {
    "ijburgeats": {
      "command": "uv",
      "args": ["--directory", "/path/to/ijburgeats-mcp", "run", "ijburgeats-mcp"]
    }
  }
}
```

### OpenClaw

```bash
openclaw mcp set ijburgeats '{"command":"/path/to/venv/bin/ijburgeats-mcp","env":{"IJBURGEATS_CONFIG":"/path/to/config.toml"}}'
```

OpenClaw prefixes the tool names (`ijburgeats__place_order`).

## Tools

| Tool | What it does |
|---|---|
| `list_restaurants` | Restaurants with their description and menu categories. The site only shows restaurant names as logos, so these identify them. |
| `get_menu` | One restaurant's products (id, price, description, `has_choices`), optionally one category |
| `search_menu` | Search products across all restaurants |
| `get_product_options` | `addons` (pick exactly one per group) and `toppings` (optional extras) for a product |
| `add_to_cart` | Add a product with its chosen options |
| `view_cart` | Items, delivery cost, total, minimum-order status |
| `update_cart_item` | Change a line's quantity; 0 removes it |
| `clear_cart` | Empty the cart |
| `get_order_history` | Past orders of the logged-in account |
| `reorder` | Put a past order back in the cart; reports `skipped` (no longer on the menu) and `needs_choice` (a required option didn't match) |
| `get_delivery_times` | Delivery slots for a day; `ASAP` is always accepted |
| `review_order` | Sets the delivery time and returns items, address, payment method and `total_to_pay`. Orders nothing. |
| `place_order` | Places the order. Needs `expected_total` equal to the reviewed total and returns the payment link. Marked destructive, so clients that ask for approval will ask. |

## Limitations

- The cart lives in the server's web session, not in your account. A cart built
  here is not visible on the website, and it is lost when the server restarts.
- One server process means one cart: every user of the same server shares it.
- Not supported: pickup, coupons, loyalty points, and Tokoh (a separate webshop
  at tokoh.ijburgeats.nl).
- The website's own "Bestel opnieuw" button is not used. In testing it dropped
  items and re-added the old payment fee as a product, so `reorder` rebuilds
  orders itself.

## How it works

All state (delivery postcode, cart, delivery time) is kept server-side in a
session cookie, so the client is a cookie-holding `httpx` session against these
endpoints:

| Purpose | Request |
|---|---|
| Delivery postcode | `GET /FindStoreByZipCode/{postcode}` |
| Menu, all restaurants | `GET /menu` (HTML) |
| Product options | `GET /menu/product/{id}/choices` (HTML fragment) |
| Add to cart | `POST /AddToCart` `{ProductID, Amount, Addons:[{AddonID}], Toppings:[{ToppingID}]}` |
| Cart | `GET /CartJSON`, `POST /UpdateCart/{row}/{n}`, `POST /DeleteFromCart/{row}` (empty body, or HTTP 411) |
| Delivery time | `GET /GetTimeOptions/{yyyy-mm-dd}`, `POST /UpdateOrderDateTime {DateTime}` |
| Payment methods | `GET /checkout-json` |
| Place order | `POST /processCheckout` (JSON) → `{Status: "Redirect", RedirectURL}` |
| Login | `POST /user/login` form `email`, `password` → 303 and an auth cookie |
| Order history | `GET /user/orders` (HTML: item names and choices, no product ids) |

HTML parsing is isolated in [`parsing.py`](src/ijburgeats_mcp/parsing.py), so a
site change usually means fixing one function.

## Development

```bash
uv run pytest
```

The tests run offline. Parsers are tested against pages captured from the
public site in `tests/fixtures` (the order history fixture is synthetic), and
the client and tools are tested against a fake shop on `httpx.MockTransport`.
No test places an order.

## License

[MIT](LICENSE)
