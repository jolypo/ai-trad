# FINAL REVIEW — Luqman Trade v23

v23 fixes a capital-accounting design defect present in the previous shared-current implementation. Previously, `current_bot_capital` was reduced by a realized stock loss and the next budget calculation multiplied that reduced combined balance by the allocation percentages again. With a $2,000 current target and 50/50 plan, one stock loss could therefore make both Stocks and Options appear near $999 even though Options had not traded.

The corrected architecture persists independent engine accounts: `stocks_target_capital`, `stocks_current_capital`, `options_target_capital`, and `options_current_capital`, plus engine-specific excess-profit fields. In fixed allocation plans, realized P&L is posted only to the originating engine. The combined Current Bot Capital remains a compatibility/accounting summary rather than the source used to re-split each engine every day.

Daily rollover deliberately resets daily P&L, trade counters, and runtime state but leaves the engine Current Capital balances unchanged. A $2.50 stock loss from a $1,000 stock bucket therefore remains $997.50 on the next day while a $1,000 options bucket remains $1,000.

Explicit capital-plan changes are treated differently from daily rollover: they retarget the buckets because the user explicitly requested a new allocation, while preserving existing drawdown so a plan change cannot silently erase losses. The Opportunity Pool remains intentionally shared because its product meaning is that both engines compete for one common pool.

Verification: 114/114 automated tests passed; Python compilation passed; all 15 Jinja templates parsed. The only external limitation is that the one-time PostgreSQL migration cannot be proven against the user's deployed Render database from this local environment. The migration is additive and does not recreate/drop existing tables.
