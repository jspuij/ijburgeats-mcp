"""MCP server exposing ijburgeats.nl ordering as tools."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import Account, Customer, IJburgEatsClient, IJburgEatsError

CONFIG_PATH = Path(os.environ.get("IJBURGEATS_CONFIG", "~/.config/ijburgeats/config.toml")).expanduser()

mcp = MCPServer(
    "ijburgeats",
    instructions=(
        "Order food at ijburgeats.nl (IJburg, Amsterdam). Typical flow: list_restaurants -> "
        "search_menu / get_menu -> get_product_options (only when has_choices is true) -> "
        "add_to_cart -> review_order -> show the review to the user -> place_order ONLY after "
        "the user explicitly confirms. Prices are in euros. Online payments (iDEAL, card) are "
        "completed by the user in their browser via the payment link place_order returns."
    ),
)

_client: IJburgEatsClient | None = None


def client() -> IJburgEatsClient:
    global _client
    if _client is None:
        if not CONFIG_PATH.exists():
            raise IJburgEatsError(
                f"Config ontbreekt: maak {CONFIG_PATH} aan (zie config.example.toml in de repo)."
            )
        with CONFIG_PATH.open("rb") as f:
            config = tomllib.load(f)
        account = Account(**config["account"]) if config.get("account", {}).get("password") else None
        allowed = config.get("policy", {}).get("allowed_payments")
        _client = IJburgEatsClient(
            Customer(**config["customer"]),
            account=account,
            allowed_payments=frozenset(allowed) if allowed is not None else None,
        )
    return _client


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
CART = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


@mcp.tool(annotations=READ_ONLY)
def list_restaurants() -> list[dict[str, Any]]:
    """List the restaurants on IJburg Eats with a short description and their menu categories.

    Restaurant names are not available as text; identify them by description and categories.
    """
    return [asdict(r) for r in client().restaurants()]


@mcp.tool(annotations=READ_ONLY)
def get_menu(store_id: str, category: str | None = None) -> list[dict[str, Any]]:
    """Menu of one restaurant (store_id from list_restaurants), optionally one category only.

    Products with has_choices=true need get_product_options before add_to_cart.
    """
    return [
        asdict(c)
        for c in client().menu()
        if c.store_id == store_id and (category is None or category.lower() in c.name.lower())
    ]


@mcp.tool(annotations=READ_ONLY)
def search_menu(query: str) -> list[dict[str, Any]]:
    """Search all restaurants' products by name, description or category (case-insensitive)."""
    q = query.lower()
    hits = []
    for c in client().menu():
        for p in c.products:
            if q in p.name.lower() or q in p.description.lower() or q in c.name.lower():
                hits.append({**asdict(p), "category": c.name, "store_id": c.store_id})
    return hits


@mcp.tool(annotations=READ_ONLY)
def get_product_options(product_id: str) -> dict[str, Any]:
    """Choices for a product. Each addon group needs exactly one option id; toppings are optional
    extras (respect max_in_group when set). Prices are surcharges per item."""
    return client().product_choices(product_id)


@mcp.tool(annotations=CART)
def add_to_cart(
    product_id: str,
    quantity: int = 1,
    addon_ids: list[str] | None = None,
    topping_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Add a product to the cart and return the updated cart. Nothing is ordered yet."""
    client().add_to_cart(product_id, quantity, addon_ids, topping_ids)
    return client().cart()


@mcp.tool(annotations=READ_ONLY)
def view_cart() -> dict[str, Any]:
    """Current cart: items (with row_id), delivery cost, total and minimum-order status."""
    return client().cart()


@mcp.tool(annotations=CART)
def update_cart_item(row_id: str, quantity: int) -> dict[str, Any]:
    """Change the quantity of a cart line (row_id from view_cart); 0 removes it."""
    client().update_quantity(row_id, quantity)
    return client().cart()


@mcp.tool(annotations=CART)
def clear_cart() -> dict[str, Any]:
    """Remove everything from the cart."""
    client().clear_cart()
    return client().cart()


@mcp.tool(annotations=READ_ONLY)
def get_order_history() -> list[dict[str, Any]]:
    """Past orders of the logged-in account (newest first): order_id, time, total, status and
    items with the choices made. "Transactie kosten" is the old online-payment fee, not food."""
    return client().order_history()


@mcp.tool(annotations=CART)
def reorder(order_id: str) -> dict[str, Any]:
    """Add the items of a past order (order_id from get_order_history) to the cart, matched by name
    against the current menu. Reports items no longer on the menu (skipped) and items whose
    required choice could not be matched (needs_choice: ask the user, then use add_to_cart with
    the matched ids plus their pick). Nothing is ordered; continue with review_order."""
    return client().reorder(order_id)


@mcp.tool(annotations=READ_ONLY)
def get_delivery_times(day: str | None = None) -> list[dict[str, str]]:
    """Available delivery slots for a day (YYYY-MM-DD, default today). Pass a slot's value as
    delivery_time to review_order / place_order; "ASAP" means as soon as possible."""
    return client().delivery_times(date.fromisoformat(day) if day else None)


def _review(payment: str, delivery_time: str) -> dict[str, Any]:
    c = client()
    time = c.set_delivery_time(delivery_time)
    cart = c.cart()
    options = c.payment_options()
    chosen = next((o for o in options if o["value"] == payment), None)
    problems = []
    if not cart["items"]:
        problems.append("De winkelwagen is leeg.")
    if not cart["minimum_reached"]:
        problems.append(f"Minimum niet gehaald: nog €{cart['amount_needed_for_minimum']:.2f} nodig.")
    if any(i["sold_out"] for i in cart["items"]):
        problems.append("Er zit een uitverkocht product in de winkelwagen.")
    if chosen is None:
        problems.append(f"Onbekende betaalwijze {payment!r}.")
    payment_cost = chosen["cost"] if chosen else 0.0
    cu = c.customer
    return {
        "items": cart["items"],
        "delivery_cost": cart["delivery_cost"],
        "payment_method": chosen["name"] if chosen else payment,
        "payment_cost": payment_cost,
        "total_to_pay": round(cart["total"] + payment_cost, 2),
        "delivery_time": time["display"],
        "deliver_to": f"{cu.first_name} {cu.last_name}, {cu.street} {cu.house_number}, "
        f"{cu.postcode} {cu.city}",
        "payment_options": options,
        "problems": problems,
        "_gewenste_tijd": time["value"],
    }


@mcp.tool(annotations=CART)
def review_order(payment: str = "MSP_Ideal", delivery_time: str = "ASAP") -> dict[str, Any]:
    """Prepare checkout without ordering: sets the delivery time and returns items, address,
    payment method and total_to_pay. Show this to the user and get explicit confirmation before
    calling place_order. payment is a value from payment_options (e.g. MSP_Ideal, WINKEL_Cash)."""
    review = _review(payment, delivery_time)
    review.pop("_gewenste_tijd")
    return review


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
)
def place_order(
    payment: str, expected_total: float, delivery_time: str = "ASAP", note: str = ""
) -> dict[str, Any]:
    """PLACE A REAL ORDER that costs money. Only call after the user explicitly confirmed the
    review_order result. expected_total must equal review_order's total_to_pay; the order is
    refused if the total changed. For iDEAL/card, give the user the returned payment_url to pay
    in their browser; the order is only final once paid. note is an optional message for the
    restaurant/driver."""
    review = _review(payment, delivery_time)
    if review["problems"]:
        raise IJburgEatsError("Niet besteld: " + " ".join(review["problems"]))
    if abs(review["total_to_pay"] - expected_total) > 0.005:
        raise IJburgEatsError(
            f"Niet besteld: totaal is nu €{review['total_to_pay']:.2f}, verwacht €{expected_total:.2f}. "
            "Doe opnieuw review_order en laat de gebruiker bevestigen."
        )
    result = client().submit_order(payment, review["_gewenste_tijd"], note)
    online = payment.startswith("MSP_")
    return {
        "status": result["status"],
        "payment_url" if online else "confirmation_url": result["url"],
        "total": review["total_to_pay"],
        "delivery_time": review["delivery_time"],
        "next_step": (
            "Open payment_url in the browser and pay; the order is sent to the restaurant after payment."
            if online
            else "Order placed; pay on delivery."
        ),
    }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
