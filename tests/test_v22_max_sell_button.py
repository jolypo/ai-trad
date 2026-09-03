from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/style.css").read_text(encoding="utf-8")


def test_static_stock_close_has_max_button():
    assert 'class="secondary mini max-qty-btn"' in DASH
    assert 'data-max-qty="{{p.qty}}"' in DASH
    assert "الحد الأعلى" in DASH


def test_live_stock_close_has_max_button_and_current_qty():
    assert 'data-max-qty="${x.qty}"' in DASH
    assert "const maxQty=Number(raw);" in DASH
    assert "input.value=fmtQty(maxQty);" in DASH


def test_max_button_never_submits_form():
    assert 'type="button" class="secondary mini max-qty-btn"' in DASH
    assert '<button class="danger mini">' in DASH


def test_mobile_close_controls_are_separated_and_touch_friendly():
    assert 'grid-template-columns:minmax(0,1fr) minmax(0,1fr)' in CSS
    assert 'grid-column:1 / -1' in CSS
    assert 'min-height:44px' in CSS
