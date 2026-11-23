import pandas as pd
from scalping_engine import load_all_symbols, backtest_trend_pullback, backtest_bollinger_mr, backtest_supertrend, SYMBOLS_10

def main():
    print("Loading data for 10 symbols...")
    data = load_all_symbols()
    print("Data loaded.\n")
    
    strategies = [
        ("Trend Pullback Scalper", backtest_trend_pullback),
        ("Bollinger Mean Reversion", backtest_bollinger_mr),
        ("Supertrend Breakout", backtest_supertrend)
    ]
    
    for name, strat_fn in strategies:
        print(f"=== Testing {name} ===")
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        
        results = []
        for sym in SYMBOLS_10:
            df = data[sym]
            res = strat_fn(df)
            results.append({
                "Symbol": sym.replace("-USDT-SWAP", ""),
                "Trades": res.trades_count,
                "WinRate": f"{res.win_rate:.1f}%",
                "Return": f"{res.total_ret:+.2f}%",
                "PF": f"{res.profit_factor:.2f}",
                "MaxDD": f"{res.max_drawdown:.2f}%",
                "AvgHold": f"{res.avg_hold_mins:.1f}m"
            })
            total_trades += res.trades_count
            total_wins += int(res.trades_count * res.win_rate / 100.0)
            total_pnl += res.total_ret
            
        df_res = pd.DataFrame(results)
        print(df_res.to_string(index=False))
        avg_ret = total_pnl / len(SYMBOLS_10)
        overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        print(f"--> Total Trades: {total_trades}, Avg Return across 10: {avg_ret:+.2f}%, Overall WR: {overall_wr:.1f}%\n")

if __name__ == "__main__":
    main()
