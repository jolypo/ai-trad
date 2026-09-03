# Luqman Trade — Senior Final Review

## Overall assessment

The codebase is internally coherent for its intended Paper Trading stage. The final review focused on broker truth, exact quantities, capital ownership, live synchronization, fractional-share execution, option contract accounting, bilingual dynamic rendering, and Render/PostgreSQL stability.

## 1. Broker truth vs local state

Alpaca remains authoritative for current account, positions and broker orders. Luqman's database is the strategy/audit ledger. Live endpoints continuously reconcile the UI from both layers rather than assuming the locally stored trade is the broker truth.

Manual exits are intentionally defensive: the requested quantity is validated against the Luqman open trade and the current Alpaca position; old protective orders must be removed before a new manual sell is sent; a pending order remains pending and is reconciled later instead of being falsely labeled filled.

## 2. Fractional shares

Fractional shares are now a real execution path rather than a display-only setting. When enabled, risk sizing may return a decimal quantity. Before submitting, the broker asset is checked for `fractionable=true`. Luqman submits a DAY market order with decimal quantity, then waits for a broker-confirmed fill before creating the open trade. The broker-reported filled quantity and average fill price become the stored trade values.

Whole-share positions continue to use broker-native bracket protection. Fractional positions use Luqman's frequent realtime protection cycle because the project deliberately avoids pretending that the exact same protection model applies to all fractional order combinations.

## 3. Capital engine

There is one capital source of truth. The broker's Buying Power is not treated as permission to spend. The hard internal ceiling is the user's Allocated Capital.

Calculation order:

1. Allocated Capital.
2. Deduct configured Cash Reserve.
3. Deduct current open exposure.
4. Deduct pending capital reservations.
5. Apply broker cash ceiling.
6. Apply the chosen stock/options allocation plan.
7. Apply per-position and risk-per-trade limits.
8. Atomically reserve capital before the broker request.

The new exclusive presets remove ambiguity:
- Stocks Only: 100/0.
- Options Only: 0/100.

Manual allocation must equal exactly 100%, preventing unexplained unassigned percentages. Opportunity Pool remains a deliberately shared pool; its stock and option availability values represent the same pool, not two independent pools.

## 4. Options logic

Options are whole contracts and all exposure/P&L calculations use the contract multiplier of 100. Automatic entries require a fresh, active/tradable contract and sufficient option allocation. A zero option allocation permits monitoring but blocks execution with an explicit budget reason.

The free Alpaca `indicative` options feed may not equal OPRA quality. The UI exposes the feed and quote age so stale/indicative data is not disguised as official OPRA realtime data.

## 5. SPX

SPX is no longer represented as SPY. The UI/analysis layer uses an S&P 500 cash-index reference (`^GSPC`) for the displayed index value and recent reference bars. Option contracts and broker execution remain sourced from Alpaca. This separation avoids using an ETF price as if it were the index level.

## 6. Realtime UI synchronization

Dashboard, trade ledger, broker orders, system alerts, capital state, positions and options capital update without a full page reload. Dynamic broker status labels are localized on every refresh; Arabic pages no longer revert to English `filled/canceled/market/buy/sell` after the first live update.

The reporting equity chart now uses the same chart engine as Portfolio: value axis, date/time axis, range selection, hover/touch details, high/low/change, and periodic refresh.

## 7. Database connection stability

The previous Render error `QueuePool limit ... connection timed out` is consistent with database sessions remaining checked out while live endpoints wait on external Alpaca/market-data HTTP.

The final code reduces this risk by:
- bounding the PostgreSQL QueuePool to 5 persistent + 5 overflow connections with an 8-second checkout timeout and 300-second recycle, so a saturated pool fails fast instead of hanging for 30 seconds;
- preserving explicit `db.close()` lifecycle;
- using `expire_on_commit=False` so loaded values remain usable after releasing a transaction;
- explicitly committing/releasing transactions before slow external HTTP calls in the heaviest live endpoints;
- keeping `/health` independent from PostgreSQL and Alpaca;
- keeping `/ready` as the database-specific readiness probe.

This is a structural fix rather than merely increasing the pool size.

## 8. UI and responsive review

A rendered browser matrix tested 12 pages across 9 viewport sizes (108 combinations): 320, 360, 390, 430, 768, 1024, 1280, 1440 and 1920 px. No page-level horizontal overflow was detected and the mobile hamburger/sidebar interaction passed.

## 9. Validation results

- 52/52 pytest tests passed from a clean, dedicated test database.
- 17 Python source modules parsed successfully.
- 15 Jinja templates parsed successfully.
- Rendered inline JavaScript and external chart JavaScript passed Node syntax checking.
- Arabic and English route smoke tests passed.
- Stocks Only and Options Only persistence/calculation smoke tests passed.
- Secret scan returned zero findings.

## 10. Remaining limitations

No software review can truthfully guarantee zero future defects, zero broker rejection, or profitable results. This delivery remains Paper-only. Fractional-share tests validate the exact Alpaca-compatible code path using broker mocks and official API semantics; an actual fractional Paper fill should still be tested once after deployment with the user's current Alpaca Paper account before relying on it operationally.


## 11. Test isolation and deployment safety

The regression suite now forces a dedicated SQLite database before importing the app. Each test receives a clean schema, and the test harness never drops the database configured in Render. This fixed the earlier false failures caused by stale local test rows while also preventing an accidental production-database destructive test run.

The final artifact excludes local SQLite files, pytest caches, bytecode caches, and real `.env` files.
