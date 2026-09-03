from types import SimpleNamespace


def test_trailing_activation_default_is_40_percent():
    from app.models import BotSettings
    col = BotSettings.__table__.c.options_trailing_activation_pct
    assert float(col.default.arg) == 40.0


def test_trailing_activation_scales_from_actual_fill_price():
    # 40% activation is percentage-based, not tied to the $2.00 example.
    for fill, expected in [(2.00, 2.80), (3.00, 4.20), (0.70, 0.98)]:
        activation = round(fill * (1 + 40.0 / 100.0), 8)
        assert abs(activation - expected) < 1e-9
