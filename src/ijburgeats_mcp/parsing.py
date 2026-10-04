"""Parsers for the HTML and JSON that the Cashdesk webshop serves.

ijburgeats.nl has no public API; the menu is server-rendered HTML and the choices
for a product come as an HTML fragment. Everything here is pure (no I/O) so it can
be tested against captured pages in tests/fixtures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

STORE_ID_RE = re.compile(r"store_id=([0-9a-f-]{36})")
RESTAURANT_CLASS_RE = re.compile(r"^restaurant_([0-9a-f-]{36})$")
PRICE_SUFFIX_RE = re.compile(r"\s*\(\+\s*€\s*[\d.,]+\)$")


@dataclass
class Product:
    id: str
    name: str
    price: float
    description: str
    has_choices: bool


@dataclass
class Category:
    store_id: str
    name: str
    products: list[Product] = field(default_factory=list)


@dataclass
class Restaurant:
    store_id: str
    description: str
    categories: list[str] = field(default_factory=list)


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _cents(value: str | None) -> float:
    try:
        return int(value or 0) / 100
    except ValueError:
        return 0.0


def parse_menu(html: str) -> list[Category]:
    """All categories with their products, for every restaurant on the menu page."""
    soup = BeautifulSoup(html, "html.parser")
    categories: list[Category] = []
    for group in soup.select("div.js-productgroep"):
        store_id = next(
            (m.group(1) for c in group.get("class", []) if (m := RESTAURANT_CLASS_RE.match(c))),
            None,
        )
        if store_id is None:
            continue
        category = Category(store_id=store_id, name=_text(group.select_one(".categorie-title")))
        for card in group.select("div.js-productkaart"):
            category.products.append(
                Product(
                    id=card["id"],
                    name=" ".join(card.get("data-title", "").split()),
                    price=_cents(card.get("data-base-price")),
                    description=_text(card.select_one(".prod-description")),
                    has_choices=card.get("data-has-choices") == "1",
                )
            )
        categories.append(category)
    return categories


def parse_restaurants(home_html: str, menu_html: str) -> list[Restaurant]:
    """Restaurants listed on the homepage, with their menu categories.

    The site shows restaurant names only as logos, so the homepage blurb and the
    category names are what identifies a restaurant.
    """
    soup = BeautifulSoup(home_html, "html.parser")
    restaurants: dict[str, Restaurant] = {}
    for card in soup.select("div.restaurant-card"):
        link = card.find("a", href=STORE_ID_RE)
        if link is None:
            continue  # e.g. Tokoh, which is a separate webshop on its own subdomain
        store_id = STORE_ID_RE.search(link["href"]).group(1)
        inner = card.select_one(".restaurant-card-inner") or card
        for a in inner.find_all("a"):
            a.decompose()
        restaurants.setdefault(store_id, Restaurant(store_id=store_id, description=_text(inner)))

    for category in parse_menu(menu_html):
        restaurant = restaurants.setdefault(
            category.store_id, Restaurant(store_id=category.store_id, description="")
        )
        restaurant.categories.append(category.name)
    return list(restaurants.values())


def parse_choices(html: str) -> dict[str, Any]:
    """Option groups for a product with choices.

    "addons" are required single-choice dropdowns (pick exactly one option per group);
    "toppings" are optional extras (checkboxes). Prices are the surcharge per item.
    """
    soup = BeautifulSoup(html, "html.parser")
    addons = []
    for i, block in enumerate(soup.select("div.Addon"), start=1):
        select = block.find("select")
        if select is None:
            continue
        options = [
            {
                "id": opt["value"],
                # Dropdown labels carry the surcharge, e.g. "Jasmin Rice (+ €0,50)"; the
                # price is in its own field and order history uses the bare name.
                "name": PRICE_SUFFIX_RE.sub("", _text(opt)),
                "price": _cents(opt.get("data-price")),
            }
            for opt in select.find_all("option")
            if opt.get("value")
        ]
        addons.append({"group": _text(block.find("label")) or f"Keuze {i}", "options": options})

    toppings = []
    for box in soup.select('input[type="checkbox"]'):
        label = soup.find("label", attrs={"for": box.get("id")})
        content = label.select_one(".topping-checkbox-content") if label else None
        if content is not None:
            for small in content.find_all("small"):
                small.decompose()
        max_per_group = int(box.get("data-topping-max") or 0)
        toppings.append(
            {
                "id": box["value"],
                "name": _text(content),
                "price": _cents(box.get("data-price")),
                "group": box.get("data-topping-group", "0"),
                "max_in_group": max_per_group or None,
            }
        )
    return {"addons": addons, "toppings": toppings}


def parse_time_options(data: dict[str, Any]) -> list[dict[str, str]]:
    """Delivery time slots from /GetTimeOptions/{date}: value is what the server expects back."""
    slots = []
    for option_html in data.get("Options") or []:
        opt = BeautifulSoup(option_html, "html.parser").find("option")
        if opt is None or not opt.get("value") or opt.has_attr("disabled"):
            continue
        slots.append({"value": opt["value"], "label": _text(opt)})
    return slots


def parse_payment_options(raw: str) -> list[dict[str, Any]]:
    """Enabled payment methods from /checkout-json (sometimes served double-encoded)."""
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data.replace("'", '"'))
    if "PaymentOptionsList" not in data:
        raise ValueError(data.get("error") or "Geen betaalopties gevonden")
    return [
        {"value": o["Value"], "name": o["Name"], "group": g["GroupName"], "cost": o["Cost"]}
        for g in data["PaymentOptionsList"]
        for o in g["Options"]
        if o.get("Enabled", True)
    ]


ORDER_ID_RE = re.compile(r"/user/orders/([0-9a-f-]{36})")
ITEM_RE = re.compile(r"^(\d+)\s*x\s+(.+)$")


def normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def parse_order_history(html: str) -> list[dict[str, Any]]:
    """Past orders from /user/orders, newest first as the site lists them.

    Items only carry names (no product ids); each choice the customer made is a
    " - <option name>" line under the item.
    """
    soup = BeautifulSoup(html, "html.parser")
    orders = []
    for entry in soup.select("div.my-list-item"):
        form = entry.find("form", action=ORDER_ID_RE)
        if form is None:
            continue
        facts = {}
        for li in entry.select("ul.list-unstyled li"):
            key, _, value = _text(li).partition(":")
            facts[key.strip()] = value.strip()
        when, _, address = _text(entry.select_one("p.mb-0")).removeprefix("Gewenst:").partition(" - afleveradres ")

        items = []
        for li in entry.select(".show-full-order li"):
            lines = [" ".join(s.split()) for s in li.get_text("\n").split("\n")]
            lines = [s for s in lines if s]
            m = ITEM_RE.match(lines[0]) if lines else None
            if m is None:
                continue
            items.append(
                {
                    "quantity": int(m.group(1)),
                    "name": m.group(2).strip(),
                    "choices": [s.lstrip("-").strip() for s in lines[1:]],
                }
            )
        orders.append(
            {
                "order_id": ORDER_ID_RE.search(form["action"]).group(1),
                "type": _text(entry.find("h3")),
                "requested_time": when.strip(),
                "address": address.strip(),
                "total": facts.get("Totaal", ""),
                "payment_status": facts.get("Betaal status", ""),
                "order_status": facts.get("Order status", ""),
                "items": items,
            }
        )
    return orders


def summarize_cart(data: dict[str, Any]) -> dict[str, Any]:
    """The useful part of /CartJSON."""
    cart = data["ShoppingCart"]
    delivery = cart["DeliveryData"]
    return {
        "items": [
            {
                "row_id": p["RowID"],
                "product_id": p["ProductId"].replace("-", ""),
                "name": p["Name"].strip(),
                "quantity": p["Count"],
                "unit_price": p["RowPrice"],
                "total": p["TotalPrice"],
                "choices": [a["Name"] for a in p.get("Addons", [])],
                "extras": [t["Name"] for t in p.get("Toppings", [])],
                "sold_out": p.get("Soldout", False),
            }
            for p in cart["Products"]
        ],
        "products_total": cart.get("ProductTotal"),
        "delivery_cost": delivery["DeliveryCost"],
        "surcharges": cart.get("SurchargeTotal", 0.0),
        "total": data["Total"],
        "minimum_order": delivery["Minimum"],
        "minimum_reached": cart["MinimumReached"],
        "amount_needed_for_minimum": cart["AmountNeededToReachMinimum"],
        "asap": cart.get("IsAsapOrder"),
        "order_time": cart.get("OrderDate"),
    }
