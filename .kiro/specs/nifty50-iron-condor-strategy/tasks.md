# Implementation Tasks: Nifty 50 Iron Condor Strategy

## Task List

- [x] 1. Core Data Models
  - [x] 1.1 Create `strategies/iron_condor.py` with `OptionRecord` namedtuple (timestamp_minute, close, strike_price, expiry, option_type, underlying_close, days_to_expiry, iv, delta, instrument_key)
  - [x] 1.2 Create `SelectedLegs` dataclass with all 4 leg fields (short_call/long_call/short_put/long_put strike, premium, delta, instrument_key) plus expiry and underlying_price
  - [x] 1.3 Create `IronCondorPosition` dataclass with legs, entry_net_premium, max_profit, max_loss, entry_timestamp, lot_size, spread_width, and current premium fields for all 4 legs
  - [x] 1.4 Create `RiskState` dataclass with daily_realized_pnl, weekly_realized_pnl, consecutive_losses, is_daily_halted, is_weekly_halted, is_cooldown, cooldown_skipped, current_day, current_week_start

- [x] 2. OptionsDataLoader
  - [x] 2.1 Implement `OptionsDataLoader.__init__` and `load()` — stream-parse `nifty50_options_1min.csv`, build primary index `_by_timestamp: dict[str, list[OptionRecord]]` keyed by truncated timestamp minute
  - [x] 2.2 Build secondary index `_by_contract: dict[tuple[str, str, float, str], OptionRecord]` for direct leg lookups by (timestamp_minute, expiry, strike, option_type)
  - [x] 2.3 Build expiry index `_expiries_by_timestamp: dict[str, set[str]]` for finding nearest expiry at any timestamp
  - [x] 2.4 Implement `get_options_at(timestamp, tolerance_seconds=60)` — return all OptionRecords at the given timestamp with ±1 minute fallback
  - [x] 2.5 Implement `get_option(timestamp, expiry, strike, option_type)` — direct contract lookup from secondary index
  - [x] 2.6 Implement `get_nearest_expiry(timestamp)` — return nearest future expiry date from expiry index
  - [x] 2.7 Handle missing/empty CSV gracefully — log error, set disabled flag, return empty results

- [x] 3. VIXFilter
  - [x] 3.1 Implement `VIXFilter.__init__(max_vix)` and `is_entry_allowed(options, underlying_close)` — compute ATM IV proxy from nearest-strike CE+PE average IV, compare against max_vix threshold

- [x] 4. StrikeSelector
  - [x] 4.1 Implement `StrikeSelector.__init__(delta_min, delta_max, spread_width)` and `select(options, underlying_close, expiry)` — filter OTM calls/puts, find closest delta to midpoint, compute long strikes at ±spread_width, verify all 4 contracts exist, return SelectedLegs or None

- [x] 5. Checkpoint: Verify data models, loader, VIX filter, and strike selector compile without errors

- [x] 6. PositionManager
  - [x] 6.1 Implement `PositionManager.__init__(lot_size)` with `is_open` property and `position` property
  - [x] 6.2 Implement `open_position(legs, timestamp, lot_size)` — compute entry_net_premium, max_profit, max_loss, create IronCondorPosition
  - [x] 6.3 Implement `update_premiums(timestamp, data_loader)` — look up current premiums for all 4 legs from OptionsDataLoader, fallback to last known if unavailable
  - [x] 6.4 Implement `get_current_pnl()` — return (entry_net_premium - current_net_premium) × lot_size in rupees
  - [x] 6.5 Implement `get_pnl_pct_of_max_profit()` — return current P&L as percentage of max_profit
  - [x] 6.6 Implement `close_position()` — clear position, return exit metadata dict with reason, pnl_pct, pnl_rupees, days_held, legs

- [x] 7. RiskProtection
  - [x] 7.1 Implement `RiskProtection.__init__` with all 8 gate parameters (max_total_capital, max_daily_loss, max_weekly_loss, max_loss_pct_of_capital, max_consecutive_losses, min_premium_per_unit, max_capital_per_position, lot_size)
  - [x] 7.2 Implement `check_entry_allowed(proposed_max_loss, net_premium_per_unit, is_expiry_day, current_time_ist, position_is_open)` — run 8 checks in order: capital cap → single position → daily halt → weekly halt → per-trade % → consecutive cooldown → expiry time → min premium. Return (allowed, reason) tuple.
  - [x] 7.3 Implement `on_new_day(current_date)` — reset daily_realized_pnl and is_daily_halted
  - [x] 7.4 Implement `on_new_week(current_date)` — reset weekly_realized_pnl and is_weekly_halted (when Monday detected)
  - [x] 7.5 Implement `record_trade_result(pnl_rupees)` — update daily/weekly P&L, consecutive loss counter, check if daily/weekly halt should trigger
  - [x] 7.6 Implement `should_force_close()` — return True if daily or weekly halt was just triggered with open position

- [x] 8. Checkpoint: Verify PositionManager and RiskProtection compile without errors

- [x] 9. IronCondorStrategy Main Class
  - [x] 9.1 Implement `IronCondorStrategy.__init__` with all configurable parameters (max_vix, delta range, spread_width, profit_target_pct, stop_loss_multiplier, DTE range, all risk params) and constructor validation (raise ValueError for invalid params)
  - [x] 9.2 Implement `on_start()` — instantiate OptionsDataLoader, VIXFilter, StrikeSelector, PositionManager, RiskProtection; load options CSV; load persisted state from JSON if exists
  - [x] 9.3 Implement `on_candle(candle)` entry path — detect new day/week for risk resets, check risk gates, check VIX filter, check DTE window, select strikes, validate premium, open position, return TradeAction(Signal.SELL) with full metadata
  - [x] 9.4 Implement `on_candle(candle)` exit path — update leg premiums, check profit target (50%), check stop-loss (2x), check expiry day EOD (15:15 IST), check daily/weekly halt force-close, return TradeAction(Signal.EXIT) with metadata
  - [x] 9.5 Implement `on_end()` — save state to JSON, log final summary
  - [x] 9.6 Implement `get_position()` — return "flat" or "short" based on PositionManager.is_open
  - [x] 9.7 Set class attributes: name="iron_condor", description, default_instrument, default_lot_size=25, default_candle_interval="1min", requires_option_data=True, brokerage_per_trade=500.0

- [x] 10. State Persistence for Multi-Day Positions
  - [x] 10.1 Implement `_save_state(filepath)` — serialize IronCondorPosition (if open), RiskState, and strategy config to JSON file (`iron_condor_state.json`)
  - [x] 10.2 Implement `_load_state(filepath)` — deserialize JSON back into IronCondorPosition and RiskState, restore open position and risk tracking across process restarts
  - [x] 10.3 Call `_save_state()` after every entry, exit, and at `on_end()` (market close each day)
  - [x] 10.4 Call `_load_state()` in `on_start()` to resume from where we left off

- [x] 11. Multi-Day LiveTrader Enhancement
  - [x] 11.1 Add `--daemon` flag to `run.py` CLI for multi-day mode
  - [x] 11.2 Implement daemon loop in `live_trader.py` — after market close, save state, sleep until next trading day 9:14 AM IST, then resume the trading loop. Continue Monday-Friday.
  - [x] 11.3 Add graceful shutdown handler (SIGINT/SIGTERM) — save state before exiting so position survives restart
  - [x] 11.4 Add `caffeinate` integration hint in CLI help text for macOS users

- [x] 12. Checkpoint: Verify full strategy compiles, CLI discovers it via `--list`, and `--strategy iron_condor` is accepted

- [x] 13. CLI Integration and Backtest Support
  - [x] 13.1 Update `run.py` `run_backtest()` to auto-load options CSV path for strategies with `requires_option_data=True` and pass it via strategy constructor or environment
  - [x] 13.2 Ensure `Backtester` handles Iron Condor TradeActions correctly — Signal.SELL for entry (direction="short"), Signal.EXIT for close, P&L = entry_price - exit_price per unit
  - [x] 13.3 Implement `compute_iron_condor_metrics(result: BacktestResult)` helper — extract from Trade.metadata: avg premium collected, avg days held, % exits by reason (profit_target/stop_loss/expiry_eod/daily_halt/weekly_halt), avg P&L per trade

- [x] 14. Iron Condor Backtest Report Enhancement
  - [x] 14.1 Add Iron Condor-specific summary section to `report_generator.py` — show avg premium, exit reason breakdown, avg days held, risk protection trigger counts (only when strategy_name == "iron_condor")

- [ ]* 15. Tests
  - [ ]* 15.1 Unit tests for OptionsDataLoader — load fixture CSV, verify indexing, verify missing file handling
  - [ ]* 15.2 Unit tests for VIXFilter — test with known ATM IV above/below threshold
  - [ ]* 15.3 Unit tests for StrikeSelector — test with known option chain, verify delta selection, verify missing long strike handling
  - [ ]* 15.4 Unit tests for PositionManager — open/close lifecycle, P&L arithmetic with hand-calculated values
  - [ ]* 15.5 Unit tests for RiskProtection — test each of 8 gates individually with boundary values
  - [ ]* 15.6 Unit tests for state persistence — save state, load state, verify round-trip
  - [ ]* 15.7 Property-based tests (Hypothesis) for P&L computation correctness (Property 6)
  - [ ]* 15.8 Property-based tests (Hypothesis) for risk protection state machines (Properties 17-19)
  - [ ]* 15.9 Property-based tests (Hypothesis) for constructor validation (Property 23)
  - [ ]* 15.10 Integration test — feed small candle sequence through IronCondorStrategy, verify entry/exit TradeActions
