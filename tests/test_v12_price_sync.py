from app.options_engine import _contract_price


def test_contract_price_prefers_quote_midpoint():
    bid, ask, mid = _contract_price({"latestQuote": {"bp": 2.0, "ap": 2.4}, "latestTrade": {"p": 9.9}})
    assert bid == 2.0
    assert ask == 2.4
    assert mid == 2.2


def test_contract_price_fallbacks_are_safe():
    assert _contract_price({"latestQuote": {"ap": 1.75}})[2] == 1.75
    assert _contract_price({"latestTrade": {"p": 1.5}})[2] == 1.5
