import pandas as pd
import numpy as np
from line_search_optimizer import OKXScalperSimulator, SYMBOLS_10
from scalping_engine import evaluate_trades

def main():
    sim = OKXScalperSimulator()
    optimal_params = {
        "rsi_len": 3,
        "os_level": 5.0,
        "ob_level": 95.0,
        "exit_rsi": 55.0,
        "tp_pct": 0.008,
        "sl_atr_mult": 1.8,
        "max_hold_bars": 12,
        "trend_ema_len": 200,
        "trend_asym": True,
        "fee_rate": 0.0004
    }
    
    print("=" * 80)
    print("  OKX Perpetual Contract Scalping Strategy - 1-Month Backtest Report")
    print("  Strategy: CRSI-Trend-Adaptive Scalper (极值动量与自适应趋势超短线策略)")
    print("=" * 80)
    
    table_rows = []
    all_trades = []
    
    for sym in SYMBOLS_10:
        trades = sim.run_single_symbol(sym, **optimal_params)
        all_trades.extend(trades)
        res = evaluate_trades(trades)
        
        table_rows.append({
            "Contract": sym,
            "Trades": res.trades_count,
            "WinRate": f"{res.win_rate:.1f}%",
            "NetReturn": f"{res.total_ret:+.2f}%",
            "ProfitFactor": f"{res.profit_factor:.2f}" if res.profit_factor < 100 else "Inf",
            "MaxDD": f"{res.max_drawdown:.2f}%",
            "AvgHold": f"{res.avg_hold_mins:.1f}m"
        })
        
    df_table = pd.DataFrame(table_rows)
    print(df_table.to_string(index=False))
    
    overall = evaluate_trades(all_trades)
    print("\n" + "-" * 80)
    print(f"PORTFOLIO AGGREGATE:")
    print(f"  Total Trades:           {overall.trades_count}")
    print(f"  Portfolio Win Rate:     {overall.win_rate:.2f}% ({int(overall.win_rate * overall.trades_count / 100)} Wins / {overall.trades_count - int(overall.win_rate * overall.trades_count / 100)} Losses)")
    print(f"  Portfolio Profit Factor:{overall.profit_factor:.2f}")
    print(f"  Portfolio Max Drawdown: {overall.max_drawdown:.2f}%")
    print(f"  Average Holding Time:   {overall.avg_hold_mins:.1f} minutes")
    print(f"  Average Return/Symbol:  {df_table['NetReturn'].str.rstrip('%').astype(float).mean():+.2f}%")
    print("-" * 80)
    
    # Analyze trade reasons
    df_trades = pd.DataFrame(all_trades)
    if not df_trades.empty:
        print("\nExit Reason Breakdown:")
        print(df_trades["reason"].value_counts())
        print("\nDirection Breakdown:")
        print(df_trades["side"].value_counts())
        print(f"Avg Profit on Winning Trades: {df_trades[df_trades['ret'] > 0]['ret'].mean()*100:+.2f}%")
        print(f"Avg Loss on Losing Trades:    {df_trades[df_trades['ret'] <= 0]['ret'].mean()*100:+.2f}%")

if __name__ == "__main__":
    main()
