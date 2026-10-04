"""HTTP client for the ijburgeats.nl (Cashdesk) webshop.

All state (delivery postcode, cart, chosen time) lives server-side in a session
cookie, so one client instance equals one shopping session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from . import parsing

BASE_URL = "https://ijburgeats.nl"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


class IJburgEatsError(Exception):
    pass


@dataclass
class Customer:
    first_name: str
    last_name: str
    email: str
    phone: str
    street: str
    house_number: str
    postcode: str
    city: str
    company: str = ""


@dataclass
class Account:
    email: str
    password: str


# Cart line the site adds for the online-payment fee; it shows up in order history
# but is recalculated at checkout, so it must never be re-added as a product.
PAYMENT_FEE_ITEM = "transactie kosten"
AUTH_COOKIE = "_ncfa"


class IJburgEatsClient:
    def __init__(
        self,
        customer: Customer,
        http: httpx.Client | None = None,
        account: Account | None = None,
        allowed_payments: frozenset[str] | None = None,
    ):
        self.customer = customer
        self.account = account
        # None allows every method the site offers. Restricting to online methods means
        # money only moves once a person pays the link, which is the confirmation step.
        self.allowed_payments = allowed_payments
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=30, follow_redirects=True)
        self.http.headers.update({"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"})
        self._session_ready = False
        self._menu_html: str | None = None
        self._home_html: str | None = None

    # -- session -----------------------------------------------------------------

    def _login(self) -> None:
        """Form login; on success the site sets the auth cookie and redirects to /."""
        if self.account is None:
            raise IJburgEatsError("Geen account geconfigureerd: voeg een [account] sectie toe aan de config.")
        self.http.get("/user/login").raise_for_status()
        self.http.post(
            "/user/login?returnUrl=",
            data={"email": self.account.email, "password": self.account.password},
        ).raise_for_status()
        if AUTH_COOKIE not in self.http.cookies:
            raise IJburgEatsError("Inloggen mislukt: controleer e-mail en wachtwoord in de config.")

    def _ensure_session(self) -> None:
        if self._session_ready:
            return
        self.http.get("/").raise_for_status()
        if self.account is not None:
            # Logged-in orders show up in the account's order history.
            self._login()
        postcode = self.customer.postcode.replace(" ", "").upper()
        result = self._json(self.http.get(f"/FindStoreByZipCode/{postcode}"))
        if result.get("Status") not in ("Ok", "Redirect"):
            raise IJburgEatsError(result.get("Msg") or f"Postcode {postcode} niet geaccepteerd")
        self._session_ready = True

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise IJburgEatsError(f"Onverwacht antwoord van {response.url}: {response.text[:200]}") from e

    def _post_status(self, path: str, **kwargs: Any) -> dict[str, Any]:
        # Cashdesk's IIS front rejects body-less POSTs with HTTP 411, hence content=b"".
        if "json" not in kwargs and "data" not in kwargs:
            kwargs["content"] = b""
        result = self._json(self.http.post(path, **kwargs))
        if result.get("Status") != "Ok":
            raise IJburgEatsError(result.get("Msg") or f"{path} mislukt: {result}")
        return result

    # -- menu --------------------------------------------------------------------

    def _menu(self) -> str:
        if self._menu_html is None:
            self._ensure_session()
            response = self.http.get("/menu")
            response.raise_for_status()
            self._menu_html = response.text
        return self._menu_html

    def restaurants(self) -> list[parsing.Restaurant]:
        if self._home_html is None:
            response = self.http.get("/")
            response.raise_for_status()
            self._home_html = response.text
        return parsing.parse_restaurants(self._home_html, self._menu())

    def menu(self) -> list[parsing.Category]:
        return parsing.parse_menu(self._menu())

    def product_choices(self, product_id: str) -> dict[str, Any]:
        self._ensure_session()
        response = self.http.get(f"/menu/product/{_compact(product_id)}/choices")
        response.raise_for_status()
        return parsing.parse_choices(response.text)

    # -- cart --------------------------------------------------------------------

    def add_to_cart(
        self,
        product_id: str,
        quantity: int = 1,
        addon_ids: list[str] | None = None,
        topping_ids: list[str] | None = None,
    ) -> None:
        self._ensure_session()
        self._post_status(
            "/AddToCart",
            json={
                "ProductID": _compact(product_id),
                "Amount": quantity,
                "Addons": [{"AddonID": a} for a in addon_ids or []],
                "Toppings": [{"ToppingID": t} for t in topping_ids or []],
            },
        )

    def cart(self) -> dict[str, Any]:
        self._ensure_session()
        return parsing.summarize_cart(self._json(self.http.get("/CartJSON")))

    def update_quantity(self, row_id: str, quantity: int) -> None:
        self._ensure_session()
        if quantity <= 0:
            self._post_status(f"/DeleteFromCart/{row_id}")
        else:
            self._post_status(f"/UpdateCart/{row_id}/{quantity}")

    def clear_cart(self) -> None:
        for item in self.cart()["items"]:
            self.update_quantity(item["row_id"], 0)

    # -- account -----------------------------------------------------------------

    def order_history(self) -> list[dict[str, Any]]:
        self._ensure_session()
        response = self.http.get("/user/orders")
        response.raise_for_status()
        if "login" in response.url.path:  # session expired: log in again once
            self._login()
            response = self.http.get("/user/orders")
            response.raise_for_status()
        return parsing.parse_order_history(response.text)

    def reorder(self, order_id: str) -> dict[str, Any]:
        """Put the items of a past order back in the cart.

        The site's own "Bestel opnieuw" drops items and re-adds the payment fee as a
        product, so instead each item is matched by name against the current menu and
        its choices by option name.
        """
        order = next((o for o in self.order_history() if o["order_id"] == order_id), None)
        if order is None:
            raise IJburgEatsError(f"Bestelling {order_id} niet gevonden in de bestelhistorie.")

        products: dict[str, list[tuple[parsing.Product, str]]] = {}
        for category in self.menu():
            for product in category.products:
                products.setdefault(parsing.normalize_name(product.name), []).append((product, category.store_id))

        added, skipped, needs_choice = [], [], []
        for item in order["items"]:
            key = parsing.normalize_name(item["name"])
            if key == PAYMENT_FEE_ITEM:
                continue
            if key not in products:
                skipped.append({**item, "reason": "staat niet (meer) op het menu"})
                continue
            product, store_id = products[key][0]
            addon_ids, topping_ids, missing = [], [], []
            wanted = {parsing.normalize_name(c): c for c in item["choices"]}
            if product.has_choices:
                choices = self.product_choices(product.id)
                for group in choices["addons"]:
                    match = next((o for o in group["options"] if parsing.normalize_name(o["name"]) in wanted), None)
                    if match is None and len(group["options"]) == 1:
                        match = group["options"][0]
                    if match is None:
                        missing.append(group)
                        continue
                    addon_ids.append(match["id"])
                    wanted.pop(parsing.normalize_name(match["name"]), None)
                for topping in choices["toppings"]:
                    if wanted.pop(parsing.normalize_name(topping["name"]), None) is not None:
                        topping_ids.append(topping["id"])
            if missing:
                needs_choice.append(
                    {"product_id": product.id, "name": product.name, "quantity": item["quantity"],
                     "addon_ids_matched": addon_ids, "topping_ids_matched": topping_ids, "open_groups": missing}
                )
                continue
            self.add_to_cart(product.id, item["quantity"], addon_ids, topping_ids)
            added.append(
                {"name": product.name, "quantity": item["quantity"], "store_id": store_id,
                 "choices": item["choices"], "unmatched_choices": list(wanted.values())}
            )
        return {"added": added, "skipped": skipped, "needs_choice": needs_choice, "cart": self.cart()}

    # -- checkout ----------------------------------------------------------------

    def delivery_times(self, day: date | None = None) -> list[dict[str, str]]:
        self._ensure_session()
        day = day or date.today()
        return parsing.parse_time_options(self._json(self.http.get(f"/GetTimeOptions/{day.isoformat()}")))

    def set_delivery_time(self, value: str) -> dict[str, Any]:
        """value is "ASAP" or a slot value from delivery_times()."""
        self._ensure_session()
        result = self._json(self.http.post("/UpdateOrderDateTime", json={"DateTime": value}))
        if result.get("Status") != "OK":
            raise IJburgEatsError(result.get("Msg") or result.get("Message") or "Tijd kon niet worden gezet")
        return {
            "asap": result["IsAsap"],
            "display": "Zo snel mogelijk" if result["IsAsap"] else result.get("CartDisplayTime"),
            "value": result["GewensteTijdValue"],
        }

    def payment_options(self) -> list[dict[str, Any]]:
        self._ensure_session()
        response = self.http.get("/checkout-json")
        response.raise_for_status()
        options = parsing.parse_payment_options(response.text)
        if self.allowed_payments is not None:
            options = [o for o in options if o["value"] in self.allowed_payments]
        return options

    def submit_order(self, payment: str, desired_time: str, note: str = "") -> dict[str, Any]:
        """POST /processCheckout exactly like the checkout page does.

        Returns {"status", "url"}: for online payment the url is the payment page,
        for cash it is the order confirmation page.
        """
        if self.allowed_payments is not None and payment not in self.allowed_payments:
            raise IJburgEatsError(
                f"Betaalwijze {payment} is uitgeschakeld in deze installatie; "
                f"toegestaan: {sorted(self.allowed_payments)}"
            )
        options = {o["value"]: o for o in self.payment_options()}
        if payment not in options:
            raise IJburgEatsError(f"Onbekende betaalwijze {payment!r}; kies uit {sorted(options)}")
        c = self.customer
        data = {
            "Voornaam": c.first_name,
            "Achternaam": c.last_name,
            "Email": c.email,
            "Telefoon": c.phone,
            "Opmerking": note,
            "Nieuwsbrief": "False",
            "OntvangSms": "False",
            "Plaats": c.city,
            "Bedrijf": c.company,
            "Postcode": c.postcode,
            "Straat": c.street,
            "Huisnummer": c.house_number,
            "Verdieping": "",
            "GewensteTijd": desired_time,
            "Payment": payment,
            # The page sends the option's data-cost attribute, a Dutch decimal string.
            "PaymentCost": f"{options[payment]['cost']:.2f}".replace(".", ","),
            "Password": "",
            "PasswordConfirm": "",
            "CouponDetails": [],
            "TerminalId": "",
        }
        result = self._json(self.http.post("/processCheckout", json=data))
        status = result.get("Status")
        if status == "Redirect":
            return {"status": status, "url": result.get("RedirectURL")}
        if status == "AppRedirect":
            return {"status": status, "url": result.get("AppRedirectURL") or result.get("RedirectURL")}
        if status == "Ok":
            return {"status": status, "url": None}
        raise IJburgEatsError(result.get("Msg") or f"Bestellen mislukt: {result}")


def _compact(product_id: str) -> str:
    """The site uses product GUIDs without dashes."""
    return product_id.replace("-", "").lower()
