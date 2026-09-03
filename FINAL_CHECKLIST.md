# FINAL CHECKLIST — Luqman Trade v23

| Check | Result | Notes |
|---|---|---|
| Independent Stocks Target/Current capital persisted | PASS | PostgreSQL additive columns added |
| Independent Options Target/Current capital persisted | PASS | PostgreSQL additive columns added |
| Stock loss affects stocks bucket only | PASS | Regression test with $1,000/$1,000 split |
| Options loss affects options bucket only | PASS | Dedicated regression test |
| Daily reset does not re-split Current Capital | PASS | Buckets survive new session/day |
| Profit restores only same engine | PASS | No cross-engine drawdown healing |
| Engine excess profit separated | PASS | Stocks/Options excess stored independently |
| Combined Current remains accounting summary | PASS | Synced from engine buckets for fixed plans |
| Explicit plan change preserves drawdown | PASS | 50/50 → 70/30 regression covered |
| Stocks Only / Options Only hard caps retained | PASS | Existing regression retained |
| Opportunity Pool shared behavior retained | PASS | Intentional legacy/shared mode |
| Broker cash remains secondary cap | PASS | Existing min(internal, broker cash) behavior retained |
| Existing capital/risk/reservation regressions | PASS | Full suite passed |
| Python compile | PASS | compileall |
| Jinja templates | PASS | 15/15 parsed |
| Tests | PASS | 114/114 |
| Real Alpaca Paper end-to-end migration | NOT TESTABLE | Requires deployment against user's live PostgreSQL/Paper account |

## Migration behavior

v23 adds independent engine capital columns without deleting existing data. For an upgraded database, the additive migration uses historical realized Trade P&L by engine, when available, to assign an existing combined drawdown to the engine that produced it. If history is insufficient, it uses a proportional fallback. Future P&L is then tracked natively per engine.
