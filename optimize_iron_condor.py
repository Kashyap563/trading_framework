"""Optuna-based parameter optimizer for the Iron Condor strategy.

Runs multiple backtests with different parameter combinations to find
the optimal settings. Uses time-based train/test split to avoid overfitting.

Usage:
    python optimize_iron_condor.py [--trials 200] [--timeout 3600]

Output:
    - Console logs with progress
    - iron_condor_optimization_report.xlsx with results
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime

from zoneinfo import ZoneInfo

import optuna
import pandas as pd
from optuna.trial import Trial

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_framework.backtester import Backtester
from trading_framework.data_fetcher import load_from_csv
from trading_framework.models import Candle
from trading_framework.strategies.iron_condor import IronCondorStrategy, OptionsDataLoader

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPTIONS_CSV = os.path.join(os.path.dirname(__file__), "data", "nifty50_options_1min.csv")
INTRADAY_CSV = os.path.join(os.path.dirname(__file__), "data", "nifty50_intraday_1min.csv")

# Fixed constraints
FIXED_CAPITAL = 100_000.0
MAX_DAILY_LOSS_CAP = 50_000.0

# Train/Test split date
SPLIT_DATE = date(2025, 7, 1)


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_and_split_candles() -> tuple[list[Candle], list[Candle]]:
    """Load intraday candles, sort, and split into train/test by date."""
    logger.info("Loading intraday candles from %s", INTRADAY_CSV)
    all_candles = load_from_csv(INTRADAY_CSV)

    # Filter to options data period (Aug 2024 onwards)
    options_start = date(2024, 8, 23)
    all_candles = [
        c for c in all_candles
        if c.timestamp.astimezone(IST).date() >= options_start
    ]
    all_candles.sort(key=lambda c: c.timestamp)

    logger.info("Total candles in options period: %d", len(all_candles))

    # Split
    train = [c for c in all_candles if c.timestamp.astimezone(IST).date() < SPLIT_DATE]
    test = [c for c in all_candles if c.timestamp.astimezone(IST).date() >= SPLIT_DATE]

    logger.info(
        "Split: Train=%d candles (%s to %s), Test=%d candles (%s to %s)",
        len(train),
        train[0].timestamp.astimezone(IST).date() if train else "N/A",
        train[-1].timestamp.astimezone(IST).date() if train else "N/A",
        len(test),
        test[0].timestamp.astimezone(IST).date() if test else "N/A",
        test[-1].timestamp.astimezone(IST).date() if test else "N/A",
    )

    return train, test


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_score_from_trades(trades: list) -> tuple[float, dict]:
    """Compute optimization score from iron condor trade metadata.

    Uses the actual premium-based P&L stored in trade.metadata["pnl_rupees"],
    NOT the generic backtester's spot-price-based P&L.

    Returns:
        (score, metrics_dict)
    """
    if not trades:
        return 0.0, {}

    # Extract P&L from metadata
    pnl_list = []
    for t in trades:
        meta = getattr(t, "metadata", {}) or {}
        pnl = meta.get("pnl_rupees", 0.0)
        pnl_list.append(pnl)

    total_trades = len(pnl_list)
    if total_trades < 10:
        return 0.0, {"total_trades": total_trades, "reason": "too_few_trades"}

    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]

    total_pnl = sum(pnl_list)
    total_brokerage = total_trades * 700.0  # ₹700 per round trip
    net_pnl = total_pnl - total_brokerage

    if net_pnl <= 0:
        return 0.0, {
            "total_trades": total_trades,
            "net_pnl": net_pnl,
            "reason": "not_profitable",
        }

    win_rate = len(wins) / total_trades * 100.0
    total_wins = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.0
    profit_factor = total_wins / total_losses if total_losses > 0 else 10.0
    profit_factor = min(profit_factor, 10.0)

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_list:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    dd_ratio = 1.0 - min(max_dd / FIXED_CAPITAL, 1.0)

    # --- Composite Score for Live Trading Confidence ---
    # Components:
    #   1. Net P&L per month (annualized return on capital)
    #   2. Risk-adjusted: penalize drawdown relative to capital
    #   3. Consistency: profit factor and win rate
    #   4. Brokerage efficiency: net already deducts brokerage, so more trades
    #      with same net = same score (brokerage naturally penalizes overtrading)
    
    # Monthly return on capital (10 months of train data)
    monthly_return = (net_pnl / FIXED_CAPITAL) / 10.0  # fraction per month
    
    # Drawdown penalty: max_dd as fraction of capital (0 = no DD, 1 = wiped out)
    dd_penalty = max_dd / FIXED_CAPITAL
    
    # Risk-adjusted return (like Calmar ratio): monthly return / drawdown fraction
    # If no drawdown, use a high but finite value
    if dd_penalty > 0:
        calmar = monthly_return / dd_penalty
    else:
        calmar = monthly_return * 100  # No drawdown = excellent
    
    # Final score: Calmar × Profit Factor × Win Rate
    # - Calmar rewards high return relative to drawdown
    # - Profit Factor rewards consistent edge
    # - Win Rate rewards probability of success
    # Net P&L already has brokerage deducted, so overtrading is naturally penalized
    score = calmar * profit_factor * (win_rate / 100.0)

    metrics = {
        "total_trades": total_trades,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "net_pnl": round(net_pnl, 0),
        "total_pnl": round(total_pnl, 0),
        "total_brokerage": round(total_brokerage, 0),
        "max_drawdown": round(-max_dd, 0),
        "avg_win": round(sum(wins) / len(wins), 0) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 0) if losses else 0,
        "score": round(score, 2),
    }

    return score, metrics


# ---------------------------------------------------------------------------
# Optuna Objective
# ---------------------------------------------------------------------------

def create_objective(train_candles: list[Candle]):
    """Create the Optuna objective function with train data in closure."""

    # Pre-load options data ONCE — shared across all trials (read-only)
    logger.info("Pre-loading options data (this takes a few minutes)...")
    shared_data_loader = OptionsDataLoader(OPTIONS_CSV)
    if not shared_data_loader.load():
        logger.error("Failed to pre-load options data")
        sys.exit(1)
    logger.info("Options data pre-loaded — ready for optimization")

    def objective(trial: Trial) -> float:
        # --- Sample parameters ---
        max_vix = trial.suggest_float("max_vix", 12.0, 30.0, step=1.0)
        delta_min = trial.suggest_float("delta_min", 0.08, 0.20, step=0.02)
        delta_max = trial.suggest_float("delta_max", 0.18, 0.35, step=0.02)

        # Constraint: delta_min < delta_max
        if delta_min >= delta_max:
            return 0.0

        spread_width = trial.suggest_categorical("spread_width", [50, 100, 150])
        profit_target_pct = trial.suggest_float("profit_target_pct", 25.0, 75.0, step=5.0)
        stop_loss_multiplier = trial.suggest_float("stop_loss_multiplier", 1.0, 4.0, step=0.5)
        entry_dte_min = trial.suggest_int("entry_dte_min", 0, 3)
        entry_dte_max = trial.suggest_int("entry_dte_max", 3, 28)

        # Constraint: dte_min < dte_max
        if entry_dte_min >= entry_dte_max:
            return 0.0

        max_consecutive_losses = trial.suggest_int("max_consecutive_losses", 2, 6)
        min_premium = trial.suggest_float("min_premium", 3.0, 20.0, step=1.0)
        max_daily_loss = trial.suggest_float("max_daily_loss", 10000.0, MAX_DAILY_LOSS_CAP, step=5000.0)
        max_weekly_loss = trial.suggest_float("max_weekly_loss", 15000.0, 80000.0, step=5000.0)

        # Constraint: weekly >= daily
        if max_weekly_loss < max_daily_loss:
            return 0.0

        max_capital_per_position = trial.suggest_float(
            "max_capital_per_position", 8000.0, 25000.0, step=1000.0,
        )

        # --- Create strategy and run backtest ---
        try:
            strategy = IronCondorStrategy(
                max_vix=max_vix,
                short_strike_delta_min=delta_min,
                short_strike_delta_max=delta_max,
                spread_width=spread_width,
                profit_target_pct=profit_target_pct,
                stop_loss_multiplier=stop_loss_multiplier,
                entry_days_before_expiry_min=entry_dte_min,
                entry_days_before_expiry_max=entry_dte_max,
                max_total_capital=FIXED_CAPITAL,
                max_daily_loss=max_daily_loss,
                max_weekly_loss=max_weekly_loss,
                max_loss_pct_of_capital=20.0,
                max_consecutive_losses=max_consecutive_losses,
                min_premium_per_unit=min_premium,
                max_capital_per_position=max_capital_per_position,
                options_csv_path=OPTIONS_CSV,
            )

            # Inject pre-loaded data to skip CSV reload
            strategy._data_loader = shared_data_loader

            backtester = Backtester(strategy)
            result = backtester.run(train_candles)

            score, metrics = compute_score_from_trades(result.trades)

            # Log progress — all parameters
            logger.info(
                "Trial %d: score=%.2f | trades=%d | win=%.0f%% | pf=%.2f | "
                "net=\u20b9%.0f | dd=\u20b9%.0f | vix<%.0f | delta=%.2f-%.2f | "
                "spread=%d | target=%d%% | sl=%.1fx | dte=%d-%d | "
                "daily_loss=\u20b9%.0f | weekly_loss=\u20b9%.0f | consec=%d | "
                "min_prem=%.0f | cap_per_pos=\u20b9%.0f",
                trial.number, score,
                metrics.get("total_trades", 0),
                metrics.get("win_rate", 0),
                metrics.get("profit_factor", 0),
                metrics.get("net_pnl", 0),
                metrics.get("max_drawdown", 0),
                max_vix, delta_min, delta_max, spread_width,
                profit_target_pct, stop_loss_multiplier,
                entry_dte_min, entry_dte_max,
                max_daily_loss, max_weekly_loss,
                max_consecutive_losses, min_premium,
                max_capital_per_position,
            )

            return score

        except Exception as exc:
            logger.warning("Trial %d failed: %s", trial.number, exc)
            return 0.0

    return objective

def validate_best_params(params: dict, test_candles: list[Candle]) -> tuple:
    """Run backtest with best params on test data. Returns (result, score, metrics)."""
    strategy = IronCondorStrategy(
        max_vix=params["max_vix"],
        short_strike_delta_min=params["delta_min"],
        short_strike_delta_max=params["delta_max"],
        spread_width=params["spread_width"],
        profit_target_pct=params["profit_target_pct"],
        stop_loss_multiplier=params["stop_loss_multiplier"],
        entry_days_before_expiry_min=params["entry_dte_min"],
        entry_days_before_expiry_max=params["entry_dte_max"],
        max_total_capital=FIXED_CAPITAL,
        max_daily_loss=params["max_daily_loss"],
        max_weekly_loss=params["max_weekly_loss"],
        max_loss_pct_of_capital=20.0,
        max_consecutive_losses=params["max_consecutive_losses"],
        min_premium_per_unit=params["min_premium"],
        max_capital_per_position=params.get("max_capital_per_position", 12000.0),
        options_csv_path=OPTIONS_CSV,
    )

    backtester = Backtester(strategy)
    result = backtester.run(test_candles)
    score, metrics = compute_score_from_trades(result.trades)
    return result, score, metrics


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(
    study: optuna.Study,
    train_metrics: dict,
    test_metrics: dict,
    best_params: dict,
    train_result,
    test_result,
    output_path: str,
) -> None:
    """Generate Excel report with optimization results."""

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Sheet 1: Best Parameters
        params_df = pd.DataFrame([best_params]).T
        params_df.columns = ["Value"]
        params_df.index.name = "Parameter"
        params_df.to_excel(writer, sheet_name="Best Parameters")

        # Sheet 2: Train vs Test comparison
        all_keys = ["total_trades", "winning_trades", "losing_trades", "win_rate",
                    "profit_factor", "net_pnl", "total_pnl", "total_brokerage",
                    "max_drawdown", "avg_win", "avg_loss", "score"]
        comparison = {"Metric": all_keys}
        comparison["Train"] = [train_metrics.get(k, "N/A") for k in all_keys]
        comparison["Test"] = [test_metrics.get(k, "N/A") for k in all_keys]
        comp_df = pd.DataFrame(comparison)
        comp_df.to_excel(writer, sheet_name="Train vs Test", index=False)

        # Sheet 3: All trials
        trials_data = []
        for trial in study.trials:
            if trial.state == optuna.trial.TrialState.COMPLETE:
                row = {"trial": trial.number, "score": trial.value}
                row.update(trial.params)
                trials_data.append(row)
        trials_df = pd.DataFrame(trials_data)
        trials_df.sort_values("score", ascending=False, inplace=True)
        trials_df.to_excel(writer, sheet_name="All Trials", index=False)

        # Sheet 4: Top 10 parameter sets
        top10 = trials_df.head(10)
        top10.to_excel(writer, sheet_name="Top 10", index=False)

        # Sheet 5: Train trades
        if train_result.trades:
            train_trades = []
            for t in train_result.trades:
                meta = getattr(t, "metadata", {}) or {}
                train_trades.append({
                    "entry_time": t.entry_time.replace(tzinfo=None) if t.entry_time else None,
                    "exit_time": t.exit_time.replace(tzinfo=None) if t.exit_time else None,
                    "pnl_rupees": meta.get("pnl_rupees", 0),
                    "pnl_pct": meta.get("pnl_pct", 0),
                    "days_held": meta.get("days_held", 0),
                    "exit_reason": meta.get("reason", t.exit_reason),
                })
            pd.DataFrame(train_trades).to_excel(writer, sheet_name="Train Trades", index=False)

        # Sheet 6: Test trades
        if test_result.trades:
            test_trades = []
            for t in test_result.trades:
                meta = getattr(t, "metadata", {}) or {}
                test_trades.append({
                    "entry_time": t.entry_time.replace(tzinfo=None) if t.entry_time else None,
                    "exit_time": t.exit_time.replace(tzinfo=None) if t.exit_time else None,
                    "pnl_rupees": meta.get("pnl_rupees", 0),
                    "pnl_pct": meta.get("pnl_pct", 0),
                    "days_held": meta.get("days_held", 0),
                    "exit_reason": meta.get("reason", t.exit_reason),
                })
            pd.DataFrame(test_trades).to_excel(writer, sheet_name="Test Trades", index=False)

    logger.info("Report saved to %s", output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Optimize Iron Condor parameters with Optuna")
    parser.add_argument("--trials", type=int, default=200, help="Number of optimization trials")
    parser.add_argument("--timeout", type=int, default=None, help="Max time in seconds")
    parser.add_argument("--report", default="iron_condor_optimization_report.xlsx", help="Output report path")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("iron_condor_optimization.log", mode="w"),
        ],
    )
    # Suppress noisy optuna logs
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    # Suppress strategy-level position monitor logs during optimization
    # Fix strategy logger — set parent level so INFO propagates to root handlers
    logging.getLogger("trading_framework").setLevel(logging.INFO)

    logger.info("Trials: %d | Timeout: %s", args.trials, args.timeout or "None")
    logger.info("Fixed capital: ₹%.0f | Max daily loss cap: ₹%.0f", FIXED_CAPITAL, MAX_DAILY_LOSS_CAP)
    logger.info("Train/Test split: %s", SPLIT_DATE)
    logger.info("")

    # Load data
    start_time = time.time()
    train_candles, test_candles = load_and_split_candles()

    if not train_candles or not test_candles:
        logger.error("Insufficient data for train/test split")
        sys.exit(1)

    load_time = time.time() - start_time
    logger.info("Data loaded in %.1f seconds", load_time)
    logger.info("")

    # Run optimization
    logger.info("Starting Optuna optimization...")
    logger.info("-" * 60)

    study = optuna.create_study(
        direction="maximize",
        study_name="iron_condor_optimization",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    objective = create_objective(train_candles)
    study.optimize(
        objective,
        n_trials=args.trials,
        timeout=args.timeout,
        show_progress_bar=True,
    )

    logger.info("-" * 60)
    logger.info("Optimization complete! Best score: %.4f", study.best_value)
    logger.info("")

    # Best parameters
    best_params = study.best_params
    logger.info("BEST PARAMETERS:")
    for k, v in sorted(best_params.items()):
        logger.info("  %s = %s", k, v)
    logger.info("")

    # Validate on train (with best params)
    logger.info("Running backtest with best params on TRAIN data...")
    train_result, train_score, train_metrics = validate_best_params(best_params, train_candles)
    logger.info(
        "TRAIN: %d trades | Win=%.1f%% | PF=%.2f | Net=₹%.0f | DD=₹%.0f | Score=%.2f",
        train_metrics.get("total_trades", 0), train_metrics.get("win_rate", 0),
        train_metrics.get("profit_factor", 0), train_metrics.get("net_pnl", 0),
        train_metrics.get("max_drawdown", 0), train_score,
    )
    logger.info("")

    # Validate on test
    logger.info("Running backtest with best params on TEST data...")
    test_result, test_score, test_metrics = validate_best_params(best_params, test_candles)
    logger.info(
        "TEST:  %d trades | Win=%.1f%% | PF=%.2f | Net=₹%.0f | DD=₹%.0f | Score=%.2f",
        test_metrics.get("total_trades", 0), test_metrics.get("win_rate", 0),
        test_metrics.get("profit_factor", 0), test_metrics.get("net_pnl", 0),
        test_metrics.get("max_drawdown", 0), test_score,
    )
    logger.info("")

    # Overfit check
    if train_score > 0 and test_score > 0:
        ratio = test_score / train_score
        if ratio > 0.7:
            logger.info("✅ GOOD: Test/Train ratio = %.2f (>0.7 = robust)", ratio)
        elif ratio > 0.4:
            logger.warning("⚠️  CAUTION: Test/Train ratio = %.2f (moderate overfit)", ratio)
        else:
            logger.warning("❌ OVERFIT: Test/Train ratio = %.2f (parameters may not generalize)", ratio)
    elif train_score > 0 and test_score == 0:
        logger.warning("❌ OVERFIT: Train scored %.2f but test scored 0", train_score)
    logger.info("")

    # Generate report
    generate_report(study, train_metrics, test_metrics, best_params, train_result, test_result, args.report)

    total_time = time.time() - start_time
    logger.info("Total time: %.1f minutes", total_time / 60)
    logger.info("Report: %s", args.report)
    logger.info("Log: iron_condor_optimization.log")


if __name__ == "__main__":
    main()
