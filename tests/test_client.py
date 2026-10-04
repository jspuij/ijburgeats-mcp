import json
from pathlib import Path

import httpx
import pytest

from ijburgeats_mcp import server
from ijburgeats_mcp.client import BASE_URL, Account, Customer, IJburgEatsClient, IJburgEatsError

FIXTURES = Path(__file__).parent / "fixtures"
OK = {"Status": "Ok", "Msg": "", "RedirectURL": ""}
CUSTOMER = Customer(
    first_name="Jan",
    last_name="Test",
    email="jan@example.nl",
    phone="0612345678",
    street="Teststraat",
    house_number="1",
    postcode="1087 gk",
    city="Amsterdam",
)


class FakeShop:
    """Records requests and answers like ijburgeats.nl does."""

    def __init__(self, checkout_response=None):
        self.requests: list[httpx.Request] = []
        self.checkout_response = checkout_response or {"Status": "Redirect", "RedirectURL": "https://pay.example/abc"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and request.headers.get("content-length") is None:
            return httpx.Response(411, text="Length Required")
        if path == "/":
            return httpx.Response(200, text="<html></html>")
        if path.startswith("/FindStoreByZipCode/"):
            return httpx.Response(200, json={"Status": "Redirect", "RedirectURL": "/menu"})
        if path == "/CartJSON":
            return httpx.Response(200, text=(FIXTURES / "cart.json").read_text())
        if path == "/checkout-json":
            return httpx.Response(200, text=(FIXTURES / "checkout.json").read_text())
        if path == "/UpdateOrderDateTime":
            asap = json.loads(request.content)["DateTime"] == "ASAP"
            return httpx.Response(
                200,
                json={"Status": "OK", "IsAsap": asap, "CartDisplayTime": None if asap else "ma 5 okt, 18:00 uur",
                      "GewensteTijdValue": "ASAP" if asap else "2026-10-05 18:00"},
            )
        if path == "/processCheckout":
            return httpx.Response(200, json=self.checkout_response)
        if path == "/user/login" and request.method == "GET":
            return httpx.Response(200, text="<form id='loginform'></form>")
        if path == "/user/login":
            form = dict(httpx.QueryParams(request.content.decode()))
            if form == {"email": "jan@example.nl", "password": "geheim"}:
                return httpx.Response(303, headers={"Location": "/", "Set-Cookie": "_ncfa=token; path=/"})
            return httpx.Response(200, text="<form id='loginform'></form>")
        if path == "/user/orders":
            if "_ncfa" not in request.headers.get("cookie", ""):
                return httpx.Response(303, headers={"Location": "/login?returnUrl=/user/orders"})
            return httpx.Response(200, text=(FIXTURES / "order_history.html").read_text())
        if path == "/login":
            return httpx.Response(200, text="<form id='loginform'></form>")
        if path == "/menu":
            return httpx.Response(200, text=(FIXTURES / "menu.html").read_text())
        if path == "/menu/product/1ef0b7db8fd545618bd35dc50cdc31d5/choices":
            return httpx.Response(200, text=(FIXTURES / "choices_single_addon.html").read_text())
        if path in ("/AddToCart",) or path.startswith(("/UpdateCart/", "/DeleteFromCart/")):
            return httpx.Response(200, json=OK)
        return httpx.Response(404)

    def find(self, path: str) -> httpx.Request:
        return next(r for r in self.requests if r.url.path == path)


def make_client(shop: FakeShop, account: Account | None = None) -> IJburgEatsClient:
    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(shop), follow_redirects=True)
    return IJburgEatsClient(CUSTOMER, http, account=account)


ACCOUNT = Account(email="jan@example.nl", password="geheim")


def test_session_uses_normalised_postcode():
    shop = FakeShop()
    make_client(shop).cart()

    assert any(r.url.path == "/FindStoreByZipCode/1087GK" for r in shop.requests)


def test_add_to_cart_sends_compact_id_and_choices():
    shop = FakeShop()
    make_client(shop).add_to_cart("1ef0b7db-8fd5-4561-8bd3-5dc50cdc31d5", 2, ["addon-1"], ["top-1"])

    assert json.loads(shop.find("/AddToCart").content) == {
        "ProductID": "1ef0b7db8fd545618bd35dc50cdc31d5",
        "Amount": 2,
        "Addons": [{"AddonID": "addon-1"}],
        "Toppings": [{"ToppingID": "top-1"}],
    }


def test_update_quantity_zero_deletes_with_empty_body():
    shop = FakeShop()
    make_client(shop).update_quantity("row-1", 0)

    assert shop.find("/DeleteFromCart/row-1").headers["content-length"] == "0"


def test_failed_status_raises_with_site_message():
    def shop(request):
        if request.url.path == "/AddToCart":
            return httpx.Response(200, json={"Status": "Error", "Msg": "Product is uitverkocht"})
        return FakeShop()(request)

    client = IJburgEatsClient(CUSTOMER, httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(shop)))
    with pytest.raises(IJburgEatsError, match="uitverkocht"):
        client.add_to_cart("abc")


def test_submit_order_sends_checkout_form_as_json():
    shop = FakeShop()
    result = make_client(shop).submit_order("MSP_Ideal", "ASAP", note="Bel niet aan")

    body = json.loads(shop.find("/processCheckout").content)
    assert body["Payment"] == "MSP_Ideal"
    assert body["PaymentCost"] == "0,35"
    assert body["GewensteTijd"] == "ASAP"
    assert body["Opmerking"] == "Bel niet aan"
    assert body["Voornaam"] == "Jan" and body["Huisnummer"] == "1"
    assert result == {"status": "Redirect", "url": "https://pay.example/abc"}


def test_submit_order_rejects_unknown_payment():
    shop = FakeShop()
    with pytest.raises(IJburgEatsError, match="Onbekende betaalwijze"):
        make_client(shop).submit_order("PAYPAL", "ASAP")
    assert not any(r.url.path == "/processCheckout" for r in shop.requests)


def test_login_happens_before_shopping_when_account_configured():
    shop = FakeShop()
    make_client(shop, ACCOUNT).cart()

    paths = [r.url.path for r in shop.requests]
    assert paths.index("/user/login") < paths.index("/FindStoreByZipCode/1087GK")


def test_wrong_password_raises():
    shop = FakeShop()
    with pytest.raises(IJburgEatsError, match="Inloggen mislukt"):
        make_client(shop, Account(email="jan@example.nl", password="fout")).cart()


def test_order_history_without_account_explains():
    with pytest.raises(IJburgEatsError, match="Geen account"):
        make_client(FakeShop()).order_history()


def test_order_history_logs_in_again_when_session_expired():
    shop = FakeShop()
    client = make_client(shop, ACCOUNT)
    client.cart()
    client.http.cookies.delete("_ncfa")

    assert len(client.order_history()) == 2


def test_reorder_matches_items_and_choices_by_name():
    shop = FakeShop()
    result = make_client(shop, ACCOUNT).reorder("11111111-2222-3333-4444-555555555555")

    added = [json.loads(r.content) for r in shop.requests if r.url.path == "/AddToCart"]
    assert added == [
        {
            "ProductID": "1ef0b7db8fd545618bd35dc50cdc31d5",
            "Amount": 2,
            "Addons": [{"AddonID": "b8829fb7-a2e4-4750-9801-8fc0c9d5cbbb"}],  # Quinoa
            "Toppings": [{"ToppingID": "2fd233f1-cd4a-4b2d-9d9a-981f5386ecbd"}],  # Zakje Sojasaus
        },
        {"ProductID": "a3613fe18f164b9eb2d04a99cd684c3a", "Amount": 1, "Addons": [], "Toppings": []},
    ]
    # The payment fee line is dropped silently; unknown dishes are reported.
    assert [s["name"] for s in result["skipped"]] == ["Discontinued Dish"]
    assert result["needs_choice"] == []
    assert result["added"][0]["unmatched_choices"] == []


def test_reorder_reports_required_choice_it_cannot_match():
    shop = FakeShop()
    result = make_client(shop, ACCOUNT).reorder("66666666-7777-8888-9999-000000000000")

    assert not any(r.url.path == "/AddToCart" for r in shop.requests)
    [open_item] = result["needs_choice"]
    assert open_item["name"] == "Ebi Fried Shrimps Bowl"
    assert [o["name"] for o in open_item["open_groups"][0]["options"]] == ["Little Gem Green Salad", "Quinoa", "Sushi Rice"]


def test_reorder_unknown_order():
    with pytest.raises(IJburgEatsError, match="niet gevonden"):
        make_client(FakeShop(), ACCOUNT).reorder("00000000-0000-0000-0000-000000000000")


ONLINE_ONLY = frozenset({"MSP_Ideal", "MSP_MASTERCARD", "MSP_VISA"})


def test_allowed_payments_hides_cash_from_options():
    client = make_client(FakeShop())
    client.allowed_payments = ONLINE_ONLY

    assert {o["value"] for o in client.payment_options()} == ONLINE_ONLY


def test_allowed_payments_refuses_cash_before_contacting_checkout():
    shop = FakeShop()
    client = make_client(shop)
    client.allowed_payments = ONLINE_ONLY

    with pytest.raises(IJburgEatsError, match="WINKEL_Cash is uitgeschakeld"):
        client.submit_order("WINKEL_Cash", "ASAP")
    assert not any(r.url.path == "/processCheckout" for r in shop.requests)


def test_place_order_refuses_disallowed_payment(monkeypatch):
    shop = FakeShop()
    client = make_client(shop)
    client.allowed_payments = ONLINE_ONLY
    monkeypatch.setattr(server, "_client", client)

    with pytest.raises(IJburgEatsError, match="WINKEL_Cash"):
        server.place_order("WINKEL_Cash", expected_total=18.80)
    assert not any(r.url.path == "/processCheckout" for r in shop.requests)


def test_server_reads_payment_policy_from_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[customer]\nfirst_name="a"\nlast_name="b"\nemail="c"\nphone="d"\nstreet="e"\n'
        'house_number="1"\npostcode="1087GK"\ncity="f"\n'
        '[policy]\nallowed_payments=["MSP_Ideal"]\n'
    )
    monkeypatch.setattr(server, "CONFIG_PATH", config)
    monkeypatch.setattr(server, "_client", None)

    assert server.client().allowed_payments == frozenset({"MSP_Ideal"})
    assert server.client().account is None


@pytest.fixture
def fake_server(monkeypatch):
    shop = FakeShop()
    monkeypatch.setattr(server, "_client", make_client(shop))
    return shop


def test_review_order_totals_include_payment_cost(fake_server):
    review = server.review_order("MSP_Ideal", "ASAP")

    assert review["total_to_pay"] == 19.15  # 18.80 cart + 0.35 iDEAL
    assert review["delivery_time"] == "Zo snel mogelijk"
    assert review["problems"] == []
    assert not any(r.url.path == "/processCheckout" for r in fake_server.requests)


def test_place_order_refuses_when_total_changed(fake_server):
    with pytest.raises(IJburgEatsError, match="totaal is nu €19.15"):
        server.place_order("MSP_Ideal", expected_total=18.80)
    assert not any(r.url.path == "/processCheckout" for r in fake_server.requests)


def test_place_order_returns_payment_url(fake_server):
    result = server.place_order("MSP_Ideal", expected_total=19.15, delivery_time="2026-10-05T16:00:00.0000000Z")

    assert result["payment_url"] == "https://pay.example/abc"
    assert json.loads(fake_server.find("/processCheckout").content)["GewensteTijd"] == "2026-10-05 18:00"


def test_place_order_cash_returns_confirmation_url(monkeypatch):
    shop = FakeShop(checkout_response={"Status": "Redirect", "RedirectURL": "/bedankt"})
    monkeypatch.setattr(server, "_client", make_client(shop))

    result = server.place_order("WINKEL_Cash", expected_total=18.80)

    assert result["confirmation_url"] == "/bedankt"
