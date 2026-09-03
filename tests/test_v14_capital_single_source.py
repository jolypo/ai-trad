from pathlib import Path


def test_dashboard_risk_form_does_not_edit_capital():
    text = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'name="capital"' not in text
    assert 'class="primary-field capital-primary"' not in text
    assert 'risk-primary-grid single-control' in text


def test_settings_route_does_not_mutate_capital():
    text = Path("app/main.py").read_text(encoding="utf-8")
    start = text.index('@app.post("/settings")')
    end = text.index('@app.', start + 10)
    block = text[start:end]
    assert 'capital: float = Form' not in block
    assert 's.capital =' not in block
    assert 'Bot capital is managed only from Capital Control' in block
