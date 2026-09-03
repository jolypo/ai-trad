from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_stock_manual_close_uses_responsive_two_column_then_stack_css():
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    assert ".partial-close-form.manual-qty-form" in css
    assert "grid-template-columns:minmax(112px,1fr) auto" in css
    assert "min-height:44px" in css
    assert "font-size:16px" in css
    assert "grid-template-columns:1fr" in css


def test_stock_manual_close_markup_has_separate_qty_and_button_controls():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'class="partial-close-form manual-qty-form"' in html
    assert 'name="qty"' in html
    assert '<button class="danger mini">' in html


def test_options_page_has_no_manual_qty_close_form_to_overlap():
    html = (ROOT / "app/templates/options.html").read_text(encoding="utf-8")
    assert "manual-qty-form" not in html
