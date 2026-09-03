# Luqman Trade — Final Paper Delivery Checklist

Final review date: 2026-08-29

## Trading / broker synchronization

- [x] Alpaca Paper remains the only executable broker mode in this delivery.
- [x] Exact manual sell quantity is respected; the UI exposes quantity only.
- [x] Manual close validates Luqman quantity and Alpaca position quantity before submission.
- [x] Protective orders are canceled and verified gone before a manual sell is submitted.
- [x] Accepted/pending close orders are not reported as filled.
- [x] Pending closes are reconciled later and update the trade ledger when filled.
- [x] Stock and option fills, exits, P&L, orders, positions and alerts refresh without a page reload.
- [x] Exit orders do not increment the daily-entry counter.
- [x] Options exposure and P&L use the 100x contract multiplier.
- [x] Stale option quotes are rejected by the automatic contract selector.
- [x] SPX analysis uses an S&P 500 cash-index reference and is not labeled as SPY.

## Fractional shares

- [x] User setting `Allow Fractional Shares` is implemented.
- [x] Position sizing retains decimal quantities when enabled.
- [x] Alpaca `fractionable` is checked before submission.
- [x] Fractional buy payload preserves decimal `qty`, uses `market`, and `time_in_force=day`.
- [x] Fractional order must report a real fill before Luqman creates the open trade.
- [x] Filled fractional quantity and fill price are stored from the broker response.
- [x] Fractional local stop/target monitoring remains isolated from whole-share bracket protection.
- [x] Automated tests cover successful fractional fill and non-fractionable rejection.

## Capital source of truth

- [x] Allocated Capital is the hard Luqman ceiling even when Alpaca buying power is larger.
- [x] Cash reserve is removed before calculating usable trading capital.
- [x] Open exposure + pending reservations are deducted before any new order.
- [x] Stock and option engines use one shared capital engine and reservation table.
- [x] Database locking protects capital reservation from concurrent double spending.
- [x] Opportunity Pool is supported.
- [x] Stocks Only 100/0 is supported.
- [x] Balanced 50/50 is supported.
- [x] Stocks Focus 70/30 is supported.
- [x] Options Focus 30/70 is supported.
- [x] Options Only 0/100 is supported.
- [x] Manual mode requires Stocks + Options = exactly 100%.
- [x] Stocks Only with $100 capital and 35% reserve resolves to Stocks $65 / Options $0.
- [x] Options Only with the same values resolves to Stocks $0 / Options $65.
- [x] Stock entry sizing uses the stock engine's actual available bucket.
- [x] Option entry sizing uses the option engine's actual available bucket.
- [x] Options bot can monitor with a zero option allocation, but execution is blocked with a clear reason.

## Arabic / English live UI

- [x] Live broker order side/type/status remain translated after refresh.
- [x] `filled`, `canceled`, `partially_filled`, etc. have Arabic labels.
- [x] Options bot alert titles are translated.
- [x] Common options/manual alert bodies are translated.
- [x] Alert rows have visible separators.
- [x] English/Latin digits are preserved in both interface languages.
- [x] Money formatting uses `$ ` with a consistent space.

## Dashboard / reporting

- [x] Main dashboard prioritizes Allocated / In Positions / Reserved / Available capital.
- [x] Broker cash, buying power and equity are secondary details rather than bot-budget KPIs.
- [x] Expectancy was removed from the main Performance Engine.
- [x] Trade ledger refreshes dynamically.
- [x] Recent broker orders refresh dynamically.
- [x] Alerts refresh dynamically.
- [x] Portfolio/report chart has Y-axis equity values.
- [x] Portfolio/report chart has X-axis date/time labels.
- [x] Mouse hover and touch expose timestamp, equity, change and percentage.
- [x] Portfolio chart refreshes without a page reload.

## Database / Render stability

- [x] SQLAlchemy sessions close in `get_db()`.
- [x] `expire_on_commit=False` permits deliberate connection release before slow network I/O.
- [x] Live dashboard, capital, options, indicator and chart endpoints release DB transactions before external Alpaca/market-data calls where practical.
- [x] `/health` remains dependency-free.
- [x] `/ready` checks PostgreSQL independently.
- [x] PostgreSQL pool is bounded (5 + 5 overflow), has an 8s timeout, pre-ping, and 5-minute recycle to avoid 30-second QueuePool stalls.
- [x] Dashboard/status/options/capital flows release DB transactions before slow Alpaca HTTP where practical.

## Automated verification

- [x] Python compileall: PASS.
- [x] Pytest regression suite: **52/52 PASS**.
- [x] Capital POST persistence: Stocks Only 100/0 recalculates live budget immediately.
- [x] Capital POST persistence: Options Only 0/100 recalculates live budget immediately.
- [x] Fractional-shares setting persists through the real `/settings` route.
- [x] Common Alpaca live order states remain translated after dynamic refresh.
- [x] Test suite is isolated to a dedicated SQLite test database and cannot drop a production PostgreSQL schema.
- [x] Arabic route smoke: PASS.
- [x] English route smoke: PASS.
- [x] Capital persistence smoke: Stocks Only + Options Only PASS.
- [x] Jinja templates parsed: **15/15 PASS**.
- [x] Python source AST parsed: **17/17 PASS**.
- [x] Rendered inline JavaScript syntax check: PASS.
- [x] Responsive browser matrix: **108/108 PASS**.
- [x] Tested widths: 320, 360, 390, 430, 768, 1024, 1280, 1440, 1920 px.
- [x] Horizontal page overflow in matrix: **0**.
- [x] Mobile sidebar/hamburger in matrix: PASS.
- [x] Secret-pattern scan: **0 findings**.
- [x] Generated SQLite/test DB files excluded from final ZIP.
- [x] `__pycache__`, `.pyc`, `.pytest_cache` excluded from final ZIP.

## Production boundary

This is the final **Paper Trading** delivery. Passing tests does not guarantee profitable trading or eliminate every possible runtime/broker/network failure. Before any Live conversion, repeat broker-account validation with Live credentials, live-market data entitlements, compliance controls, and a limited-capital pilot.
