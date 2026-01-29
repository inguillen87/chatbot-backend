from decimal import Decimal

from utils.money_ar import format_ars, parse_ars


def test_parse_ars_handles_thousands_and_decimals():
    assert parse_ars("15.620,00") == Decimal("15620.00")
    assert parse_ars("5.207") == Decimal("5207")
    assert parse_ars("$ 2.603,33") == Decimal("2603.33")


def test_format_ars_uses_argentina_separators():
    assert format_ars(parse_ars("5.207"), decimals=2) == "5.207,00"
