# Requirements Document

## Introduction

This document specifies the requirements for a Nifty 50 Iron Condor options strategy plugin for the existing Python trading framework. An Iron Condor is a 4-leg, defined-risk options strategy that profits from low volatility by simultaneously selling an OTM call spread and an OTM put spread. The strategy targets weekly Nifty 50 expiries, uses delta-based strike selection, and manages positions at 50% of max profit. It integrates with the existing `BaseStrategy` interface, backtester, order executors, and the pre-collected 1-minute options OHLCV data with Greeks.

## Glossary

- **Iron_Condor_Engine**: The strategy class (`IronCondorStrategy`) that extends `BaseStrategy`, encapsulating all entry/exit logic, position tracking, and multi-leg management for the Iron Condor.
- **Position_Manager**: The internal component of Iron_Condor_Engine responsible for tracking the 4 option legs as a single composite position, computing combined P&L, and managing position lifecycle.
- **Strike_Selector**: The internal component of Iron_Condor_Engine responsible for choosing the 4 strike prices based on delta values and spread width from the options data.
- **VIX_Filter**: The internal component of Iron_Condor_Engine that evaluates India VIX levels to determine whether market conditions are suitable for entry.
- **Options_Data_Loader**: The module responsible for loading and indexing the pre-collected Nifty 50 options 1-minute CSV data (with IV, Greeks, moneyness) for use during backtesting.
- **Multi_Leg_Backtester**: An extension or adapter for the existing `Backtester` that handles 4-leg Iron Condor positions, tracking combined P&L across all legs rather than single entry/exit trades.
- **Spread_Width**: The distance in strike price points between the short and long option on the same side (e.g., 50 points between sold 24500 CE and bought 24550 CE).
- **Max_Profit**: The net premium collected from selling the Iron Condor (sum of short leg premiums minus sum of long leg premiums).
- **Max_Loss**: The maximum possible loss on one side, equal to Spread_Width minus Max_Profit.
- **Short_Strike_Delta**: The absolute delta value used to select the short call and short put strikes (target: 0.15–0.16).
- **OTM**: Out-of-the-money; for calls, strike above current underlying price; for puts, strike below current underlying price.
- **Leg**: One of the four individual option contracts in the Iron Condor (short call, long call, short put, long put).
- **Premium**: The price (LTP) of an option contract, representing the cost to buy or income from selling.
- **India_VIX**: The India Volatility Index, measuring expected near-term volatility of the Nifty 50 index.

## Requirements

### Requirement 1: Strategy Registration and Framework Integration

**User Story:** As a trader, I want the Iron Condor strategy to plug into the existing framework, so that I can run it using the same CLI, backtester, and execution modes as other strategies.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL extend `BaseStrategy` and set `name` to `"iron_condor"`, `description` to `"Nifty 50 Iron Condor (weekly expiry)"`, `default_instrument` to `"NSE_INDEX|Nifty 50"`, `default_lot_size` to `25`, `default_candle_interval` to `"1min"`, `requires_option_data` to `True`, and `brokerage_per_trade` to `500.0`.
2. WHEN the CLI discovers strategies in the `strategies/` directory, THE Iron_Condor_Engine SHALL be discoverable and selectable via `--strategy iron_condor`.
3. THE Iron_Condor_Engine SHALL implement `on_candle`, `on_start`, `on_end`, and `get_position` methods as defined by `BaseStrategy`.

### Requirement 2: Options Data Loading for Backtesting

**User Story:** As a trader, I want the strategy to load pre-collected Nifty 50 options data with Greeks, so that I can backtest the Iron Condor against historical option prices.

#### Acceptance Criteria

1. WHEN `on_start` is called, THE Options_Data_Loader SHALL load `nifty50_options_1min.csv` from the `data/` directory and index the records by timestamp, expiry, strike_price, and option_type.
2. THE Options_Data_Loader SHALL parse each row to extract: timestamp, open, high, low, close, volume, strike_price, expiry, option_type (CE/PE), underlying_close, days_to_expiry, IV, delta, gamma, theta, vega, and moneyness.
3. IF the options CSV file is missing or empty, THEN THE Options_Data_Loader SHALL log an error message and set the strategy to a disabled state that returns `None` from `on_candle`.
4. WHEN a candle timestamp is received in `on_candle`, THE Options_Data_Loader SHALL provide all option contracts available at that timestamp within a 1-minute tolerance window.

### Requirement 3: VIX-Based Entry Filter

**User Story:** As a trader, I want the strategy to only enter positions when India VIX is below a configurable threshold, so that I avoid selling premium in high-volatility environments where Iron Condors underperform.

#### Acceptance Criteria

1. THE VIX_Filter SHALL use a configurable `max_vix` threshold with a default value of 13.0.
2. WHEN evaluating entry conditions, THE VIX_Filter SHALL derive the VIX proxy from the average implied volatility of ATM options (nearest strike to underlying_close) available at the current timestamp.
3. WHILE the derived VIX proxy exceeds `max_vix`, THE Iron_Condor_Engine SHALL not open new Iron Condor positions.
4. WHEN the derived VIX proxy drops to or below `max_vix` and no position is open, THE Iron_Condor_Engine SHALL permit entry evaluation to proceed.

### Requirement 4: Delta-Based Strike Selection

**User Story:** As a trader, I want strikes selected based on option delta values, so that the short strikes have a consistent probability of expiring OTM regardless of market conditions.

#### Acceptance Criteria

1. THE Strike_Selector SHALL use a configurable `short_strike_delta` range with default values of 0.15 (minimum) and 0.16 (maximum) for absolute delta.
2. WHEN selecting the short call strike, THE Strike_Selector SHALL choose the CE option whose absolute delta is closest to the midpoint of the `short_strike_delta` range from the available OTM call options at the current timestamp.
3. WHEN selecting the short put strike, THE Strike_Selector SHALL choose the PE option whose absolute delta is closest to the midpoint of the `short_strike_delta` range from the available OTM put options at the current timestamp.
4. THE Strike_Selector SHALL use a configurable `spread_width` parameter (default: 50 points) to determine the long strikes.
5. WHEN the short call strike is selected at strike price S, THE Strike_Selector SHALL select the long call at strike price S + `spread_width`.
6. WHEN the short put strike is selected at strike price S, THE Strike_Selector SHALL select the long put at strike price S - `spread_width`.
7. IF fewer than 4 valid option contracts can be found for the required strikes, THEN THE Strike_Selector SHALL skip entry for that timestamp and log a warning.

### Requirement 5: Iron Condor Entry Logic

**User Story:** As a trader, I want the strategy to open Iron Condor positions with proper timing and capital constraints, so that positions are entered at optimal times within the weekly expiry cycle.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `entry_days_before_expiry` range with default values of 5 (minimum) and 10 (maximum) days.
2. WHEN `days_to_expiry` for the nearest weekly expiry falls within the `entry_days_before_expiry` range and no position is open and the VIX_Filter permits entry, THE Iron_Condor_Engine SHALL evaluate strike selection and attempt to open a position.
3. WHEN opening a position, THE Iron_Condor_Engine SHALL record all 4 legs with their entry premiums (LTP at entry timestamp): short call premium, long call premium, short put premium, and long put premium.
4. THE Iron_Condor_Engine SHALL compute `max_profit` as (short_call_premium + short_put_premium) - (long_call_premium + long_put_premium).
5. THE Iron_Condor_Engine SHALL compute `max_loss` as `spread_width` - `max_profit`.
6. THE Iron_Condor_Engine SHALL use a configurable `max_capital_per_position` parameter (default: 12000 rupees) and skip entry if `max_loss` multiplied by `default_lot_size` exceeds `max_capital_per_position`.
7. THE Iron_Condor_Engine SHALL only permit one open Iron Condor position at a time during backtesting.
8. WHEN a position is opened, THE Iron_Condor_Engine SHALL return a `TradeAction` with `Signal.SELL`, the net premium collected as the price, and metadata containing all 4 leg details (strikes, premiums, expiry, deltas).

### Requirement 6: Position Tracking and Combined P&L

**User Story:** As a trader, I want all 4 legs tracked as a single position with combined P&L, so that I can evaluate the Iron Condor as one trade rather than 4 separate ones.

#### Acceptance Criteria

1. THE Position_Manager SHALL store the 4 legs (short_call, long_call, short_put, long_put) with their entry strike prices, entry premiums, option_type, and expiry.
2. WHEN `on_candle` is called while a position is open, THE Position_Manager SHALL compute the current premium for each leg using the latest available option price data at that timestamp.
3. THE Position_Manager SHALL compute the combined current P&L as: (entry_net_premium - current_net_premium) multiplied by `default_lot_size`, where net_premium = (short_call + short_put) - (long_call + long_put).
4. THE Position_Manager SHALL compute the current P&L as a percentage of `max_profit`.
5. IF option price data for any leg is unavailable at a given timestamp, THEN THE Position_Manager SHALL use the last known premium for that leg and log a debug message.

### Requirement 7: Exit at 50% Max Profit Target

**User Story:** As a trader, I want the strategy to close positions at 50% of max profit, so that I lock in gains and improve the win rate by not holding to full expiry.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `profit_target_pct` parameter with a default value of 50.0 (percent of max profit).
2. WHEN the combined P&L of the open position reaches or exceeds `profit_target_pct` percent of `max_profit`, THE Iron_Condor_Engine SHALL close all 4 legs and return a `TradeAction` with `Signal.EXIT` and metadata containing `{"reason": "profit_target", "pnl_pct": <actual_pct>}`.

### Requirement 8: Stop-Loss Exit at 2x Premium

**User Story:** As a trader, I want a stop-loss that triggers when losses reach 2x the premium collected, so that I limit downside on positions that move against me.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `stop_loss_multiplier` parameter with a default value of 2.0.
2. WHEN the combined loss on the open position exceeds `max_profit` multiplied by `stop_loss_multiplier`, THE Iron_Condor_Engine SHALL close all 4 legs and return a `TradeAction` with `Signal.EXIT` and metadata containing `{"reason": "stop_loss", "pnl_pct": <actual_pct>}`.

### Requirement 9: Expiry Day Exit

**User Story:** As a trader, I want all positions closed by end of day on expiry, so that I avoid assignment risk and settlement complications.

#### Acceptance Criteria

1. WHEN the current trading day matches the expiry date of the open position, THE Iron_Condor_Engine SHALL close all 4 legs at or after 15:15 IST and return a `TradeAction` with `Signal.EXIT` and metadata containing `{"reason": "expiry_eod"}`.
2. IF the position has not been closed by any other exit rule by 15:15 IST on expiry day, THEN THE Iron_Condor_Engine SHALL force-close the position at the current leg premiums.

### Requirement 10: Multi-Leg Backtesting Support

**User Story:** As a trader, I want to backtest the Iron Condor strategy using the existing backtester infrastructure, so that I can evaluate historical performance with accurate multi-leg P&L.

#### Acceptance Criteria

1. THE Multi_Leg_Backtester SHALL accept the Iron_Condor_Engine and feed it candles from `nifty50_intraday_1min.csv` (the underlying Nifty 50 index data).
2. WHEN the Iron_Condor_Engine returns a `TradeAction` with `Signal.SELL`, THE Multi_Leg_Backtester SHALL record the trade entry with the net premium as the entry price and store the leg details from metadata.
3. WHEN the Iron_Condor_Engine returns a `TradeAction` with `Signal.EXIT`, THE Multi_Leg_Backtester SHALL close the trade, computing P&L from the metadata's reported combined P&L.
4. THE Multi_Leg_Backtester SHALL produce a `BacktestResult` compatible with the existing `ReportGenerator` for Excel report generation.
5. THE Multi_Leg_Backtester SHALL compute Iron Condor-specific metrics: average premium collected, average days held, percentage of trades closed at profit target vs stop-loss vs expiry, and average P&L per trade in rupees.

### Requirement 11: Trade Action Metadata for Multi-Leg Orders

**User Story:** As a trader, I want the TradeAction metadata to carry full leg details, so that order executors can place 4 simultaneous orders in live/paper/sandbox modes.

#### Acceptance Criteria

1. WHEN the Iron_Condor_Engine generates an entry `TradeAction`, THE `TradeAction.metadata` SHALL contain: `legs` (a list of 4 dicts, each with `strike`, `option_type`, `action` (buy/sell), `premium`, `delta`, `instrument_key`), `expiry`, `max_profit`, `max_loss`, `spread_width`, and `underlying_price`.
2. WHEN the Iron_Condor_Engine generates an exit `TradeAction`, THE `TradeAction.metadata` SHALL contain: `reason`, `pnl_pct`, `pnl_rupees`, `days_held`, and `legs` (with current premiums for each leg).
3. THE Iron_Condor_Engine SHALL set `TradeAction.quantity` to `default_lot_size` (25) for all trade actions.

### Requirement 12: Capital Protection — Max Invested Amount

**User Story:** As a trader, I want a hard cap on total capital at risk at any time, so that I never overexpose my account.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `max_total_capital` parameter with a default value of 100000 (₹1 lakh).
2. BEFORE opening a new position, THE Iron_Condor_Engine SHALL compute `total_capital_at_risk` as the sum of `max_loss × lot_size` for all open positions plus the proposed new position.
3. IF `total_capital_at_risk` would exceed `max_total_capital`, THEN THE Iron_Condor_Engine SHALL skip the entry and log a warning: "Capital limit reached: ₹{total_at_risk} / ₹{max_total_capital}".

### Requirement 13: Capital Protection — Single Position at a Time

**User Story:** As a trader, I want only one Iron Condor open at a time, so that I don't stack risk across overlapping positions.

#### Acceptance Criteria

1. WHILE an Iron Condor position is open (not yet exited), THE Iron_Condor_Engine SHALL reject all new entry signals and return `None` from `on_candle`.
2. ONLY AFTER the current position is fully closed (all 4 legs exited), THE Iron_Condor_Engine SHALL permit evaluation of new entry conditions.
3. THE Iron_Condor_Engine SHALL log "Skipping entry — position already open" when an entry signal is suppressed.

### Requirement 14: Capital Protection — Daily Loss Limit

**User Story:** As a trader, I want the strategy to stop trading for the day if cumulative daily losses exceed ₹20,000, so that a bad day doesn't spiral into a catastrophic loss.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `max_daily_loss` parameter with a default value of 20000 (₹20K).
2. THE Iron_Condor_Engine SHALL track `daily_realized_pnl` — the sum of P&L from all closed positions on the current trading day.
3. WHEN `daily_realized_pnl` drops below `-max_daily_loss`, THE Iron_Condor_Engine SHALL enter a "daily halt" state that rejects all new entries for the remainder of the trading day.
4. IF a position is currently open when daily halt triggers, THE Iron_Condor_Engine SHALL immediately close the open position at current market prices and return a `TradeAction` with `Signal.EXIT` and metadata `{"reason": "daily_loss_limit"}`.
5. THE `daily_realized_pnl` and "daily halt" state SHALL reset at the start of each new trading day (9:15 AM IST).

### Requirement 15: Capital Protection — Per-Trade Max Loss as % of Capital

**User Story:** As a trader, I want each trade's max possible loss capped at a percentage of my total capital, so that no single trade can cause outsized damage.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `max_loss_pct_of_capital` parameter with a default value of 5.0 (5% of `max_total_capital`).
2. BEFORE opening a position, THE Iron_Condor_Engine SHALL compute `position_max_loss` as `max_loss × lot_size`.
3. IF `position_max_loss` exceeds `max_loss_pct_of_capital / 100 × max_total_capital`, THEN THE Iron_Condor_Engine SHALL skip the entry and log: "Position max loss ₹{position_max_loss} exceeds {max_loss_pct_of_capital}% of capital (₹{limit})".
4. WITH default values (5% of ₹1L = ₹5,000), this means max loss per trade is capped at ₹5,000.

### Requirement 16: Capital Protection — Weekly Loss Limit

**User Story:** As a trader, I want the strategy to pause for the rest of the week if weekly losses exceed a threshold, so that losing streaks are contained.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `max_weekly_loss` parameter with a default value of 30000 (₹30K).
2. THE Iron_Condor_Engine SHALL track `weekly_realized_pnl` — the sum of P&L from all closed positions since Monday 9:15 AM IST.
3. WHEN `weekly_realized_pnl` drops below `-max_weekly_loss`, THE Iron_Condor_Engine SHALL enter a "weekly halt" state that rejects all new entries until the next Monday.
4. IF a position is currently open when weekly halt triggers, THE Iron_Condor_Engine SHALL immediately close the open position and return a `TradeAction` with `Signal.EXIT` and metadata `{"reason": "weekly_loss_limit"}`.
5. THE `weekly_realized_pnl` and "weekly halt" state SHALL reset at Monday 9:15 AM IST.

### Requirement 17: Capital Protection — Consecutive Loss Cooldown

**User Story:** As a trader, I want the strategy to pause after consecutive losses, so that it doesn't keep entering during unfavorable market conditions.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `max_consecutive_losses` parameter with a default value of 3.
2. THE Iron_Condor_Engine SHALL track the count of consecutive losing trades (trades closed with negative P&L).
3. WHEN the consecutive loss count reaches `max_consecutive_losses`, THE Iron_Condor_Engine SHALL enter a "cooldown" state and skip the next entry opportunity (wait one full expiry cycle).
4. AFTER skipping one entry cycle, THE Iron_Condor_Engine SHALL reset the consecutive loss counter and resume normal operation.
5. A winning trade SHALL reset the consecutive loss counter to 0.

### Requirement 18: Capital Protection — No Entry in Last 2 Hours Before Expiry

**User Story:** As a trader, I want the strategy to avoid opening new positions in the last 2 hours of expiry day, so that I don't enter positions with extreme gamma risk.

#### Acceptance Criteria

1. WHEN the current trading day is the expiry day of the nearest weekly expiry, THE Iron_Condor_Engine SHALL not open new positions after 13:30 IST (2 hours before market close).
2. THIS restriction SHALL apply regardless of all other entry conditions being met.
3. Existing open positions SHALL continue to be managed (profit target, stop-loss, expiry exit) normally.

### Requirement 19: Capital Protection — Minimum Premium Filter

**User Story:** As a trader, I want the strategy to skip trades where the premium collected is too low, so that brokerage and slippage don't eat into thin margins.

#### Acceptance Criteria

1. THE Iron_Condor_Engine SHALL use a configurable `min_premium_per_unit` parameter with a default value of 30.0 (₹30 per unit, ₹750 per lot).
2. IF the net premium collected (per unit) is less than `min_premium_per_unit`, THEN THE Iron_Condor_Engine SHALL skip the entry and log: "Premium too low: ₹{premium} < ₹{min_premium_per_unit}".
3. THIS ensures brokerage (₹500/trade) doesn't consume more than ~67% of the premium.

### Requirement 20: Configurable Strategy Parameters

**User Story:** As a trader, I want all strategy parameters to be configurable via constructor arguments, so that I can optimize the strategy through parameter sweeps.

#### Acceptance Criteria

1. THE Iron_Condor_Engine constructor SHALL accept the following parameters with their defaults: `max_vix` (13.0), `short_strike_delta_min` (0.15), `short_strike_delta_max` (0.16), `spread_width` (50), `profit_target_pct` (50.0), `stop_loss_multiplier` (2.0), `entry_days_before_expiry_min` (5), `entry_days_before_expiry_max` (10), `max_total_capital` (100000), `max_daily_loss` (20000), `max_weekly_loss` (30000), `max_loss_pct_of_capital` (5.0), `max_consecutive_losses` (3), `min_premium_per_unit` (30.0).
2. WHEN any parameter is provided to the constructor, THE Iron_Condor_Engine SHALL use the provided value instead of the default.
3. THE Iron_Condor_Engine SHALL validate that `spread_width` is a positive integer, `profit_target_pct` is between 1 and 100, `stop_loss_multiplier` is greater than 0, `short_strike_delta_min` is less than `short_strike_delta_max`, `max_total_capital` is greater than 0, `max_daily_loss` is greater than 0, and `max_loss_pct_of_capital` is between 1 and 100.
4. IF any parameter fails validation, THEN THE Iron_Condor_Engine SHALL raise a `ValueError` with a descriptive message.
