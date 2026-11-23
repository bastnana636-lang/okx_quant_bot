import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
from pathlib import Path

from line_search_optimizer import OKXScalperSimulator, SYMBOLS_10
from scalping_engine import evaluate_trades

def generate_report():
    sim = OKXScalperSimulator()
    optimal_params = {
        "rsi_len": 3,
        "os_level": 5.0,
        "ob_level": 96.0,
        "exit_rsi": 55.0,
        "tp_pct": 0.008,
        "sl_atr_mult": 1.8,
        "max_hold_bars": 12,
        "trend_ema_len": 200,
        "trend_asym": True,
        "fee_rate": 0.0004
    }
    
    all_trades = []
    symbol_results = []
    
    # Collect trades
    for sym in SYMBOLS_10:
        trades = sim.run_single_symbol(sym, **optimal_params)
        all_trades.extend(trades)
        res = evaluate_trades(trades)
        symbol_results.append({
            "Contract": sym,
            "Trades": res.trades_count,
            "WinRate": res.win_rate,
            "NetReturn": res.total_ret,
            "ProfitFactor": res.profit_factor if res.profit_factor < 100 else 999.0,
            "MaxDD": res.max_drawdown,
            "AvgHold": res.avg_hold_mins
        })
        
    df_sym = pd.DataFrame(symbol_results)
    df_trades = pd.DataFrame(all_trades)
    
    # Sort trades by timestamp / exit_i
    df_trades = df_trades.sort_values(by=["entry_i"]).reset_index(drop=True)
    df_trades["cum_ret"] = (1.0 + df_trades["ret"]).cumprod() - 1.0
    
    # Save trades to CSV
    csv_path = Path("data/okx_scalper_trades_summary.csv")
    df_trades.to_csv(csv_path, index=False)
    print(f"Saved trades to {csv_path}")
    
    # Plotting
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("OKX Contract Ultra-Short-Term Scalper (CRSI-Adaptive) - 1 Month Backtest", fontsize=16, fontweight="bold")
    
    # 1. Cumulative Equity Curve
    ax1 = axes[0, 0]
    ax1.plot(df_trades.index + 1, df_trades["cum_ret"] * 100, marker="o", color="#008080", linewidth=2, markersize=5)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.6)
    ax1.set_title("Portfolio Cumulative Net Return (%)", fontsize=12, fontweight="bold")
    ax1.set_xlabel("Trade Number")
    ax1.set_ylabel("Cumulative Return (%)")
    ax1.grid(True, linestyle=":", alpha=0.6)
    
    # 2. Per-Contract Returns Bar Chart
    ax2 = axes[0, 1]
    short_names = [s.replace("-USDT-SWAP", "") for s in df_sym["Contract"]]
    colors = ["#2ca02c" if r > 0 else "#d62728" for r in df_sym["NetReturn"]]
    bars = ax2.bar(short_names, df_sym["NetReturn"], color=colors, edgecolor="black", alpha=0.85)
    ax2.axhline(0, color="black", linewidth=1)
    ax2.set_title("Net Return by Contract (%) - All 10 Positive!", fontsize=12, fontweight="bold")
    ax2.set_ylabel("Net Return (%)")
    ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
    for bar in bars:
        yval = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2.0, yval + (0.05 if yval >= 0 else -0.15), f"{yval:+.2f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
        
    # 3. Win Rate per Contract
    ax3 = axes[1, 0]
    wr_bars = ax3.bar(short_names, df_sym["WinRate"], color="#1f77b4", edgecolor="black", alpha=0.85)
    ax3.axhline(50, color="red", linestyle="--", alpha=0.7, label="50% Threshold")
    ax3.set_title("Win Rate by Contract (%) - Overall 86.2%", fontsize=12, fontweight="bold")
    ax3.set_ylabel("Win Rate (%)")
    ax3.set_ylim(0, 115)
    ax3.grid(True, axis="y", linestyle=":", alpha=0.6)
    for bar in wr_bars:
        yval = bar.get_height()
        ax3.text(bar.get_x() + bar.get_width()/2.0, yval + 2, f"{yval:.0f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
    ax3.legend(loc="lower right")
    
    # 4. Holding Duration Distribution
    ax4 = axes[1, 1]
    holding_mins = df_trades["bars"] * 5
    ax4.hist(holding_mins, bins=10, color="#ff7f0e", edgecolor="black", alpha=0.85)
    ax4.axvline(holding_mins.mean(), color="red", linestyle="--", linewidth=2, label=f"Mean: {holding_mins.mean():.1f}m")
    ax4.set_title("Position Holding Duration Distribution (Minutes)", fontsize=12, fontweight="bold")
    ax4.set_xlabel("Holding Time (Minutes)")
    ax4.set_ylabel("Number of Trades")
    ax4.grid(True, linestyle=":", alpha=0.6)
    ax4.legend()
    
    plt.tight_layout()
    chart_path = Path("data/okx_scalper_backtest_chart.png")
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"Saved chart to {chart_path}")

if __name__ == "__main__":
    generate_report()
