# Luqman Trade — لقمان تريد

منصة MVP متعددة المستخدمين لتجربة التداول اليومي الآلي على الأسهم الأمريكية بنظام **Paper Trading أولًا**.

## الحالة الحالية بعد المراجعة
- صفحة رئيسية.
- صفحة شرح النظام والمؤشرات.
- صفحة الباقات.
- صفحة تسجيل دخول.
- صفحة إنشاء حساب.
- Dashboard مستقل لكل مستخدم.
- لوحة Admin.
- حسابات مستخدمين محفوظة في قاعدة البيانات.
- كلمات المرور مخزنة بـ PBKDF2-HMAC-SHA256 وليست كنص صريح.
- إعداد رأس المال لكل مستخدم.
- Daily Loss % من 0–100 في الواجهة، مع Hard Cap إداري افتراضي 10% للمستخدم العادي.
- Risk / Trade %.
- Max Trades.
- Profit Target.
- Allowed Stocks منفصلة لكل مستخدم.
- START يدوي لكل يوم.
- STOP يدوي.
- إيقاف تلقائي عند حد الخسارة/هدف الربح/عدد الصفقات/إغلاق السوق.
- إعادة عداد اليوم في يوم السوق الجديد، ولا يبدأ تلقائيًا في اليوم التالي.
- Trade History + in-site Alerts.
- واجهة ثنائية اللغة عربي/English ونمط RTL/LTR حسب لغة المستخدم.
- Responsive UI للجوال والتابلت واللابتوب والكمبيوتر.
- `/health` لفحص التطبيق وقاعدة البيانات وحالة إعداد Alpaca.
- Background trading loop داخل Web Process.

## الأسهم المتاحة في V1
AAPL, MSFT, NVDA, AMD, AMZN, META, AVGO, MU, UBER, INTC, ORCL, TSLA, IBM, RKLB

كل مستخدم يختار مجموعته الخاصة، والبوت لا يدخل أي سهم خارجها.

## محرك التحليل Multi-Indicator V1
في وضع `alpaca_market_paper` يتم استخدام بيانات 5 دقائق من Alpaca IEX ثم حساب:

- EMA 9
- EMA 20
- EMA 50
- RSI 14
- MACD 12/26/9
- ATR 14
- VWAP
- ADX 14
- Relative Volume
- 5-bar Momentum

ثم يتم بناء **Signal Score** شفاف. الدرجة ليست Probability إحصائية ولا ضمان ربح.

منطق الدخول الحالي Long-only Paper Validation:
- السعر فوق VWAP.
- اتجاه EMA داعم.
- MACD إيجابي.
- RSI في نطاق زخم غير متشبع.
- ADX يدعم قوة الاتجاه.
- Relative Volume يدعم السيولة والحركة.
- Momentum إيجابي بدون مطاردة حركة مفرطة.
- Score >= `MIN_SIGNAL_SCORE`.

بعد الدخول:
- يتم حساب حجم الصفقة من Risk / Trade والمسافة إلى Stop.
- Stop مبني على ATR مع حد أدنى نسبي.
- Target افتراضي يقارب 1.8R.
- في Alpaca Paper يُرسل الأمر كـBracket Order ويكون Stop Loss وTake Profit موجودين لدى الوسيط.
- تتم مزامنة حالة الأمر والـfills مع سجل Luqman Trade في الدورات اللاحقة.
- عند STOP أو Risk Limit يحاول النظام إلغاء الحماية وإغلاق مركز Alpaca Paper.
- عند 15:55 بتوقيت نيويورك يبدأ إغلاق صفقات الجلسة لتجنب بقاء Intraday positions بعد الإغلاق.

## أوضاع التشغيل
### 1) `BROKER_MODE=simulator`
للتأكد من الحسابات، الواجهة، الحفظ، START/STOP وسير النظام دون الاعتماد على بيانات خارجية.

### 2) `BROKER_MODE=alpaca_market_paper`
يستخدم بيانات Alpaca IEX ويُرسل **أوامر Alpaca Paper فعلية** إلى `paper-api.alpaca.markets`. عند ظهور إشارة مؤهلة يرسل BUY Market من نوع Bracket، ويضع Take Profit وStop Loss لدى Alpaca نفسها.

> مهم: زوج API Key/Secret يمثل حساب Alpaca Paper واحدًا. لذلك يمنع V1 تشغيل أكثر من مستخدم في الوقت نفسه على نفس مفاتيح الوسيط حتى لا تختلط الصفقات. التعدد الحقيقي بحساب Broker مستقل لكل مستخدم يحتاج OAuth/Broker integration لاحقًا.

المتغيرات:
```env
BROKER_MODE=alpaca_market_paper
ALPACA_API_KEY_ID=PUT_IN_RENDER_ONLY
ALPACA_API_SECRET_KEY=PUT_IN_RENDER_ONLY
ALPACA_TRADING_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_FEED=iex
MIN_SIGNAL_SCORE=70
BOT_TICK_SECONDS=300
MIN_SECONDS_BETWEEN_TRADES=600
MAX_OPEN_POSITIONS=1
```

## تشغيل محلي
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Deploy: GitHub → Render
1. ارفع محتويات مجلد المشروع إلى GitHub. لا ترفع `.env` ولا أي API Secret.
2. في Render اختر **New > Blueprint** واربط Repository.
3. `render.yaml` ينشئ Web Service وPostgreSQL.
4. في Environment Variables أضف:
   - `ADMIN_EMAIL`
   - `ADMIN_PASSWORD`
   - `ALPACA_API_KEY_ID`
   - `ALPACA_API_SECRET_KEY`
   - غيّر `BROKER_MODE` إلى `alpaca_market_paper` بعد التأكد من المفاتيح.
5. افتح `/health` لفحص أن خدمة الويب حية، ثم `/ready` لفحص اتصال قاعدة البيانات. `/health` متعمد أن يكون خفيفًا ولا ينتظر Alpaca أو PostgreSQL.
6. افتح `/register` وأنشئ مستخدمًا.
7. ادخل Dashboard واحفظ Capital / Risk / Daily Loss / Max Trades / Profit Target.
8. اختر Allowed Stocks.
9. أثناء ساعات السوق الأمريكي اضغط START.
10. راقب Alerts وTrades في Dashboard.

## Render Free — مهم جدًا
Render Free Web Service يدخل spin-down بعد 15 دقيقة بدون طلبات inbound، لذلك الـbackground trading loop لن يكون موثوقًا إذا تُرك الموقع بدون زيارات. للاختبار المجاني يمكنك إعداد UptimeRobot Free لطلب `/health` كل 5 دقائق. هذا مناسب للتجارب فقط، وليس بنية Production مالية.

Render Free PostgreSQL ينتهي بعد 30 يومًا؛ للتجربة مناسب، لكن لا تعتمد عليه كقاعدة بيانات دائمة للإنتاج.

## الاختبارات التي تمت
```bash
pytest -q
```
النتيجة الحالية:
```text
40 passed
```

تم أيضًا تنفيذ Smoke Test فعلي لمسار:
`Home → Register → Dashboard → Save Settings → Select Stocks → START → Paper Cycle → STOP → Health → Verify DB`
ونجح، بما في ذلك حفظ المستخدم والإعدادات والأسهم والصفقة في قاعدة البيانات.

## ما لا يمكن ضمانه
- لا يوجد نظام يستطيع ضمان الربح اليومي أو ضمان صحة التوقع.
- Signal Score ليس Probability معايرة إحصائيًا.
- الاستراتيجية الحالية يجب قياسها على Backtest وWalk-Forward وPaper Trading لفترة كافية قبل التفكير في Live Money.
- لا يوجد Live Money Execution في V1؛ التنفيذ هو Alpaca **Paper** فقط.
- تعدد المستخدمين الحقيقي مع حساب Broker منفصل لكل مستخدم يحتاج OAuth/Broker integration لكل مستخدم بدل مفتاح واحد مشترك.

## Pro Dashboard expansion (v2)

The dashboard now mirrors the connected Alpaca Paper account and adds a professional monitoring layer:

- Broker equity, cash, buying power, market exposure and day P&L.
- Live open positions from Alpaca Paper.
- Recent broker orders plus Luqman Trade's own broker-synchronized trade ledger.
- Per-symbol 5-minute indicator cards: EMA 9/20/50, RSI 14, MACD 12/26/9, ATR 14, VWAP, ADX 14, relative volume and 5-bar momentum.
- Transparent Signal Score and `QUALIFIED / WATCH / WAIT` verdict for every allowed stock.
- Portfolio performance panel: win rate, profit factor, expectancy, open positions and local realized P&L.
- Equity snapshots and an embedded equity curve without a paid chart service.
- Daily and monthly reports at `/reports`.
- Admin summary for total users, active bots and trade records.
- `/api/status` powers 30-second live account-number updates in the dashboard.

### Database upgrade

This release adds two new tables only (`portfolio_snapshots` and `stock_indicator_snapshots`). Existing Render PostgreSQL data is preserved because startup uses `Base.metadata.create_all()` to create missing tables without dropping old ones.

### Important balance terminology

`Allocated Bot Capital` is the amount Luqman Trade uses for position/risk sizing. It is not the broker account balance. `Broker Equity`, `Cash`, and `Buying Power` come directly from Alpaca Paper.

## Pro chart pages

- `/dashboard` — session controls, broker balances, open positions, indicators, ledger, alerts.
- `/portfolio` — large portfolio equity chart with 1D/1M/1Y/All ranges, owned positions sidebar, and watchlist intelligence.
- `/stock/{SYMBOL}` — TradingView-style responsive candlestick chart for one stock with 1D/5D/1M/3M ranges, open-position P&L, technical indicators, and symbol trade history.
- `/reports` — daily/monthly performance and equity history.
- `/admin` — users and platform activity.

Charts are drawn locally in the browser with SVG/JavaScript (no paid chart library). Portfolio history is read from Alpaca Paper portfolio history; stock charts use Alpaca Market Data with the configured IEX feed.

## Live dashboard + multi-position capital controls

This build keeps the existing Paper execution flow and adds dashboard-level controls without requiring new environment variables:

- `Max Position Allocation %` — caps the dollar value of one position as a percentage of Allocated Capital.
- `Maximum Open Positions` — per-user simultaneous open-position limit.
- `Cash Reserve %` — portion of Allocated Capital kept unused for later opportunities.
- Position size is the minimum of risk-based size, per-position allocation cap, and remaining usable allocated capital.
- Existing open exposure is deducted before sizing the next trade.
- Qualified cards display execution-readiness reasons such as cooldown, max open positions, insufficient available capital, or ready.
- Broker/account values and current prices refresh without a full page reload (5 seconds).
- Technical indicator cards refresh from fresh Alpaca 5-minute bars without placing orders (30 seconds).
- Stock and portfolio charts auto-refresh while the page is visible.

Existing Render PostgreSQL databases receive the three new `bot_settings` columns through an additive startup migration. Existing rows and trade history are not recreated or deleted.

---

## 2026-08-28 Professional Expansion

This build adds the approved next-stage features without removing the existing Alpaca Paper workflow.

### New user controls

- Fractional Shares toggle (default OFF).
- Manual / Scheduled / Manual+Scheduled bot start mode.
- Weekday scheduler for US regular-market sessions.
- Configurable delay after market open.
- Configurable auto-stop before market close (5-minute minimum safety floor).
- Per-user trade cooldown instead of relying only on a global environment variable.
- Risk profiles: Very Low, Low, Medium, High, Very High, Custom.
- Stop/Target mode: ATR, Fixed %, Risk/Reward.
- Manual Sell / Close button on every open Luqman Trade position.
- Existing capital allocation, max-position %, cash reserve, max-open-position and daily controls are preserved.

### Signal Lifecycle Engine

Each symbol can expose the recent signal lifecycle derived from stored scans:

- STRONG
- STABLE
- WEAKENING
- LOST
- RE_ENTRY_WATCH

The lifecycle includes direction and consecutive qualified scans. It is a descriptive state, not a probability of profit.

### Indicator Studio

`/indicators` is a dedicated per-user library for storing 50+ custom indicator definitions/references, assigning symbols, timeframe, role and score weight.

Important limitation: arbitrary TradingView Pine Script is **not** interpreted or executed automatically by this Python project. Pine source/reference text can be stored when the user has the right to use it. A custom indicator must be ported and tested in the Luqman Trade strategy engine before it can safely affect live order decisions. Protected/Invite-only TradingView source is not bypassed.

### Fractional-order safety model

Alpaca supports fractional quantities for eligible assets with DAY orders. This build checks the Alpaca `fractionable` asset flag and uses a fractional market order when fractional sizing is enabled and the calculated quantity is below one whole share.

The current whole-share path remains broker-native bracket orders. Fractional positions use Luqman Trade's local stop/target monitor, checked approximately every 30 seconds while the service is healthy. Therefore fractional mode is OFF by default and should remain Paper-only until it has been thoroughly validated. Broker-native bracket protection is still stronger against application outages.

### Dashboard / localization

- Live dashboard remains no-refresh for broker/account/position values.
- Portfolio chart adds 1D / 5D / 1M / 3M / 1Y / All ranges, left-side value axis, high/low/change values and automatic refresh.
- Main authenticated pages have Arabic/English language-specific labels with RTL/LTR layout.
- Common trading status/reason codes are localized for Arabic display.

### Render stability

`GET /health` is intentionally dependency-free and does not query Alpaca or PostgreSQL. Use `GET /ready` when you explicitly want a database readiness check.

### Environment variables

No new secret environment variables are required for these features. Existing Render variables remain valid.

## V4 Options Trading
Professional V4 adds an Alpaca Paper options workspace at `/options`. Users can select supported underlyings (including SPX when the broker actually returns tradable contracts), Call/Put/Auto direction, DTE window, target delta, maximum contracts, options allocation, premium stop-loss and take-profit. Option capital shares the same Unified Capital Engine as stocks and index ETFs.


## Final Paper Delivery Verification

The final delivery baseline includes the latest broker-reconciliation, explicit-quantity manual sell, unified capital, Options/SPX, dynamic portfolio chart, live UI polling, responsive navigation, and bilingual UI fixes.

Final recorded verification:
- `pytest -q`: **52/52 PASS** on a clean database.
- `python -m compileall -q app`: **PASS**.
- Responsive public/authenticated page matrix: **108/108 PASS** across 1920px through 320px.
- Route/form smoke audit: **PASS**.
- Static secret-pattern scan: **0 findings**.

See `CHECKLIST_V6.md`, `REVIEW_V6.md`, and `UI_AUDIT_V6.json` for the final release audit.

## Final Paper Delivery Validation — 2026-08-29

- 52/52 automated regression tests passed from a clean database.
- 108/108 responsive browser matrix checks passed across 320–1920 px.
- Fractional shares: decimal sizing + `fractionable` verification + broker-confirmed fill path tested.
- Capital plans: Opportunity, Stocks Only, Balanced, Stocks Focus, Options Focus, Options Only, Manual.
- Dynamic Arabic broker statuses and alerts remain localized after live refresh.
- Dashboard/Options/Capital share one capital source of truth.
- PostgreSQL connection-hold time reduced around external API calls.
- This package remains Paper Trading only.

## v8 capital, execution, reconciliation hardening

This delivery separates **Target Bot Capital** (`capital`) from persistent **Current Bot Capital** and **Excess Realized Profit**. Realized losses reduce Current Bot Capital. Realized gains restore Current only up to Target; any remainder is stored as excess and is not recycled automatically. Cash reserve, per-position sizing and engine allocation are calculated from Current Bot Capital.

Manual allocation is persisted by the backend and the UI uses a single Stocks slider with Options calculated as `100 - Stocks`. The same capital engine feeds Dashboard, Capital Control and Options.

Whole-share Alpaca Paper entries use bracket orders and Luqman verifies nested TP/SL child legs before declaring broker-side protection. The local trade quantity and entry price are taken from broker `filled_qty` and `filled_avg_price`. Fractional entries require Alpaca `fractionable=true`; because fractional trading has different order constraints, this version labels fractional protection as **Luqman-managed** rather than claiming bracket protection.

Restart safety disables entry engines on process startup. Before a new START in Alpaca Paper mode, broker positions/orders are reconciled with Luqman. Unexplained mismatches are persisted as `broker_reconciliation_required` and block new entries instead of silently adopting exposure.

Paper-mode runtime safety rejects the default web secret/admin password and the broker client refuses any trading host other than `paper-api.alpaca.markets`. Admin-only `/admin/diagnostics` exposes coarse process/DB-pool health without credentials.

Alpaca references used for this review:
- Orders / bracket orders: https://docs.alpaca.markets/docs/orders-at-alpaca
- Create order / supported order classes: https://docs.alpaca.markets/reference/postorder
- Options chain and feed selection: https://docs.alpaca.markets/reference/optionchain
- Market data plans / Indicative vs OPRA: https://docs.alpaca.markets/docs/about-market-data-api

## v9 — Fractional broker-side stop protection

Fractional equity entries now arm a verified Alpaca broker-side `DAY` stop after the actual fill. Luqman continues to manage the take-profit because fractional OCO/bracket semantics are not assumed. A Luqman-managed exit cancels and confirms cancellation of the fractional stop before closing, preventing competing sell orders. Fractional remainders after partial closes are re-protected with a new broker-side stop when possible.

## v11 — ATM-centered lightweight contract browser
- Options contract browser now centers on the nearest strike to the current underlying price for SPX, stocks, and ETFs.
- The nearest ATM contract is highlighted and automatically scrolled into view.
- Heavy option-chain data is requested only when the chain itself is refreshed; the 5-second live card refresh no longer re-downloads the option chain.
- Initial page render no longer duplicates the chain request; the live endpoint loads it once with the already-fetched underlying price.
- No full CALL/PUT chain table was added, preserving the lightweight/free-plan design.

## v13 — Stocks / Options P&L separation

- Added persistent daily realized P&L buckets for stock trades and option trades.
- Stock Trading shows stock-only realized P&L.
- Options Trading shows options-only realized P&L.
- Capital Control replaces the former excess-profit card with a Stocks / Options P&L summary showing stocks, options, and combined total.
- Partial and full exits attribute realized P&L using `Trade.engine`, so refresh/restart does not mix the two engines.

## v15 — Independent Stock / Options Engines

- Stock and Options bots can run independently or simultaneously for the same user.
- Options now has its own Manual / Scheduled / Manual + Scheduled mode, weekdays, start delay and stop-before-close settings.
- Scheduled stock stop closes stock trades only; scheduled options stop closes options trades only.
- Options daily trade counter/cooldown is independent from stocks.
- Stock and options pages show engine-specific capital buckets from the same unified Capital Engine.
- Example: Current Bot Capital $500, Manual allocation 10%/90% => Stocks $50, Options $450; total authority remains $500.
- Options auto-entry rejects stale/one-sided quotes and extremely wide spreads, and uses theta from the existing snapshot as a quality-ranking input without extra API calls.

## v17 — Options-only trailing profit protection

- Added an options-only Luqman-managed trailing stop. Stock exit logic is unchanged.
- Default options exit mode is `trailing`: initial option stop remains 30%, trailing activation defaults to +40%, and trailing distance defaults to 20%.
- Example: $2.00 option entry -> initial SL $1.40 -> at $3.00 the trade is not sold; trailing activates and the effective stop becomes $2.40 -> if the option reaches $4.00 the stop rises to $3.20.
- The high-water mark and trailing stop are persistent database fields, so a normal restart resumes from the last committed state.
- Once trailing is active, the stop can only move upward and is never allowed below entry/breakeven.
- Fixed Take Profit mode remains available as a user-selectable fallback.
- Trailing checks run through the fast options position-management loop (default realtime sync: 5 seconds), not the slower strategy scan.
- Alpaca does not provide a native `trailing_stop` order type for options in the documented options order support. Therefore this is Luqman-managed protection and requires the server to remain running. This release does not claim broker-native trailing protection for options.

References:
- https://docs.alpaca.markets/us/reference/postorder
- https://docs.alpaca.markets/us/docs/options-trading-overview
- https://docs.alpaca.markets/us/docs/orders-at-alpaca

## v18 — Independent Stock & Options Risk

v18 separates engine-specific risk while keeping one shared Current Bot Capital source of truth.

Stock risk settings no longer size or lock Options trades. Options now has independent Risk/Trade, Daily Loss, Max Open Positions, Trade Cooldown, Max Trades, Max Allocation, Stop Loss, and profit-exit controls on the Options page.

Both engines may run simultaneously for the same user. Their schedules and start/stop states remain independent. Capital reservations remain shared so the combined engines cannot exceed Current Bot Capital.

## v19 — Scheduled restart recovery

Render/process restarts no longer require a manual START for engines configured as `scheduled` or `both`.
On startup Luqman first disables stale in-memory active flags, resets the daily session when required, and reconciles PostgreSQL with Alpaca. Only after a clean reconciliation does each engine independently evaluate its own schedule window. If the current New York market time is inside that engine's configured day/start-delay/pre-close window, it resumes automatically. `manual` mode never auto-resumes.

The schedule window now has an explicit end boundary as well as a start boundary, preventing a server restart after the configured pre-close stop from accidentally restarting an engine near the close.

Important hosting limitation: this recovery runs when the service process starts. If a hosting provider has fully suspended the service, no Python scheduler can run while the process is asleep; an external uptime/wake mechanism or an always-on service is required for uninterrupted unattended trading.

## v20 — Broker-native Stock Trailing Stop
- Whole-share stocks can use an Alpaca-native GTC trailing stop from immediately after confirmed entry fill.
- Stock trailing distance is independently configurable from the stock risk form.
- In trailing mode there is no fixed stock take-profit; the broker trailing stop lets profits run and protects retracements.
- Fixed bracket mode remains available as a fallback.
- Fractional shares retain the broker fixed-stop fallback because Alpaca does not document native fractional trailing-stop support.


## v21 responsive manual-sell refinement

- Stock manual partial-sell quantity input and Sell button use a two-control responsive layout.
- <=760px: controls stack vertically with >=44px touch targets and clear spacing.
- >760px: input and action remain aligned side-by-side with a fixed gap.
- Verified with Chromium at 320/360/390/430/768/1024/1280/1440/1920 CSS px.
- Options page currently has no manual quantity-close form, so the stock overlap issue does not apply there.

## v23 — Independent persistent stock/options capital buckets

Fixed allocation plans now persist separate Target and Current capital for Stocks and Options. Realized P&L is applied only to the engine that produced it. A stock loss no longer reduces the options budget on the next session, and an options profit cannot automatically refill a stock drawdown. Daily reset clears daily P&L/counters only; it does not rebalance engine Current Capital. Explicit allocation changes preserve the existing combined drawdown while retargeting the buckets. Legacy Opportunity Pool remains intentionally shared.

Example: Target $2,000 with Balanced 50/50 starts at Stocks $1,000 / Options $1,000. A $2.50 stock loss results in Stocks Current $997.50 / Options Current $1,000 / Total Current $1,997.50, including after the next daily reset.
