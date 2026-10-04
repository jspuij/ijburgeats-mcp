import json
from pathlib import Path

import pytest

from ijburgeats_mcp import parsing

FIXTURES = Path(__file__).parent / "fixtures"
SUSHI = "cb0423f9-8f7b-45c9-b7b4-0cd85dfa9cd4"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_menu_reads_categories_and_products():
    categories = parsing.parse_menu(fixture("menu.html"))

    boxes = categories[0]
    assert boxes.store_id == SUSHI
    assert boxes.name == "Sushi Chefs Boxes"
    first = boxes.products[0]
    assert first.id == "a3613fe18f164b9eb2d04a99cd684c3a"
    assert first.name == 'Sushi Chefs "Signature" 36 Box'
    assert first.price == 55.0
    assert first.description == "Chef's Choice Mixed Sushi"
    assert first.has_choices is False
    assert len({c.store_id for c in categories}) == 3


def test_parse_restaurants_combines_homepage_blurbs_and_categories():
    restaurants = {r.store_id: r for r in parsing.parse_restaurants(fixture("home.html"), fixture("menu.html"))}

    sushi = restaurants[SUSHI]
    assert "sushi" in sushi.description.lower()
    assert "BESTEL" not in sushi.description
    assert sushi.categories[:2] == ["Sushi Chefs Boxes", "Nigiri"]
    # Tokoh links to its own subdomain without a store_id, so it is not listed.
    assert all("Tokoh is our traditional" not in r.description for r in restaurants.values())
    assert len(restaurants) == 11


def test_parse_choices_single_addon_and_toppings():
    choices = parsing.parse_choices(fixture("choices_single_addon.html"))

    assert choices["addons"] == [
        {
            "group": "Keuze 1",
            "options": [
                {"id": "7276696f-b471-4d15-977c-a179684c7213", "name": "Little Gem Green Salad", "price": 0.0},
                {"id": "b8829fb7-a2e4-4750-9801-8fc0c9d5cbbb", "name": "Quinoa", "price": 0.0},
                {"id": "1bd86536-930e-4319-bb65-f7005265d0f7", "name": "Sushi Rice", "price": 0.0},
            ],
        }
    ]
    soy = choices["toppings"][2]
    assert soy == {
        "id": "2fd233f1-cd4a-4b2d-9d9a-981f5386ecbd",
        "name": "Zakje Sojasaus",
        "price": 0.35,
        "group": "0",
        "max_in_group": None,
    }


def test_parse_choices_multiple_addon_groups_have_named_options():
    addons = parsing.parse_choices(fixture("choices_multi_addon.html"))["addons"]

    assert len(addons) == 3
    assert all(a["options"] and all(o["name"] for o in a["options"]) for a in addons)


def test_parse_choices_strips_price_suffix_from_addon_names():
    html = """<div class="Addon"><label>KIES UW SIDEDISH</label><select>
        <option value="" Data-Price="0">Maak je keuze</option>
        <option value="rice" Data-Price="50">Jasmin Rice (+ €0,50)</option>
        <option value="none" Data-Price="0">No Rice or Noodles</option></select></div>"""

    [group] = parsing.parse_choices(html)["addons"]

    assert group == {
        "group": "KIES UW SIDEDISH",
        "options": [
            {"id": "rice", "name": "Jasmin Rice", "price": 0.5},
            {"id": "none", "name": "No Rice or Noodles", "price": 0.0},
        ],
    }


def test_parse_time_options():
    slots = parsing.parse_time_options(json.loads(fixture("time_options.json")))

    assert slots[0] == {"value": "2026-10-05T10:00:00.0000000Z", "label": "12:00 - 12:15"}


def test_parse_time_options_keeps_asap():
    data = {"Options": ["<option value='ASAP'>Z.S.M. (verwacht binnen  30 min.)</option>", "<option value=''>-</option>"]}

    assert parsing.parse_time_options(data) == [{"value": "ASAP", "label": "Z.S.M. (verwacht binnen 30 min.)"}]


def test_parse_payment_options():
    options = parsing.parse_payment_options(fixture("checkout.json"))

    assert {o["value"]: o["cost"] for o in options} == {
        "MSP_Ideal": 0.35,
        "MSP_MASTERCARD": 0.5,
        "MSP_VISA": 0.5,
        "WINKEL_Cash": 0.0,
    }


def test_parse_payment_options_reports_double_encoded_error():
    with pytest.raises(ValueError, match="store select"):
        parsing.parse_payment_options('"{\'error\':\'er ging iets mis met store select\'}"')


def test_parse_order_history():
    orders = parsing.parse_order_history(fixture("order_history.html"))

    assert [o["order_id"] for o in orders] == [
        "11111111-2222-3333-4444-555555555555",
        "66666666-7777-8888-9999-000000000000",
    ]
    first = orders[0]
    assert first["requested_time"] == "7-8-2026 om 18:16"
    assert first["address"] == "Teststraat 1"
    assert first["total"] == "€ 41,95"
    assert first["payment_status"] == "Betaald!"
    assert first["items"][0] == {"quantity": 2, "name": "Ebi Fried Shrimps Bowl", "choices": ["Quinoa", "Zakje Sojasaus"]}
    assert first["items"][1] == {"quantity": 1, "name": 'Sushi Chefs "Signature" 36 Box', "choices": []}


def test_summarize_cart():
    cart = parsing.summarize_cart(json.loads(fixture("cart.json")))

    assert cart["items"] == [
        {
            "row_id": "bdfe963d-e8bf-4f69-b994-c7ba1ff002c9",
            "product_id": "1ef0b7db8fd545618bd35dc50cdc31d5",
            "name": "Ebi Fried Shrimps Bowl",
            "quantity": 1,
            "unit_price": 15.85,
            "total": 15.85,
            "choices": ["Quinoa"],
            "extras": ["Zakje Sojasaus"],
            "sold_out": False,
        }
    ]
    assert cart["delivery_cost"] == 2.95
    assert cart["total"] == 18.8
    assert cart["minimum_reached"] is True
