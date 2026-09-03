from __future__ import annotations

from sqlalchemy.orm import Session

from .models import CustomIndicator

SAFE_BUILTINS = {
    "max": max, "min": min, "sum": sum, "len": len, "int": int, "float": float,
    "bool": bool, "isinstance": isinstance, "locals": locals,
}


def _applies(row: CustomIndicator, symbol: str) -> bool:
    raw = str(row.symbols or "*").upper()
    if raw.strip() == "*":
        return True
    return symbol.upper() in {x.strip() for x in raw.split(",") if x.strip()}


def evaluate_custom_indicators(db: Session, user_id: int, symbol: str, bars: list[dict]) -> list[dict]:
    rows = db.query(CustomIndicator).filter_by(user_id=user_id, enabled=True, compile_status="COMPLETE").all()
    out = []
    for row in rows:
        if not _applies(row, symbol) or not row.compiled_python:
            continue
        ns = {"__builtins__": SAFE_BUILTINS}
        try:
            exec(compile(row.compiled_python, f"<indicator-{row.id}>", "exec"), ns, ns)
            result = ns["evaluate"](bars)
            out.append({"id": row.id, "name": row.name, "role": row.role, "weight": float(row.weight or 0), "signal": bool(result.get("signal")), "values": result.get("values", {})})
        except Exception as exc:
            out.append({"id": row.id, "name": row.name, "role": row.role, "weight": float(row.weight or 0), "signal": False, "error": type(exc).__name__})
    return out


def apply_custom_indicator_rules(db: Session, user, symbol: str, bars: list[dict], signal):
    """Apply only validated, enabled imports to an already-qualified base signal.

    Filter/confirm rules can veto entry. Weight can strengthen an already-qualified
    signal but cannot manufacture a trade from an otherwise unqualified base setup.
    """
    results = evaluate_custom_indicators(db, user.id, symbol, bars)
    for row in results:
        if row.get("role") in {"filter", "confirm"} and not row.get("signal"):
            return None, results
    bonus = sum(float(r.get("weight") or 0) for r in results if r.get("signal"))
    if signal is not None and bonus:
        signal.score = min(100.0, float(signal.score) + bonus)
    return signal, results
