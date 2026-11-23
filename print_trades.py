import pandas as pd

df = pd.read_csv('data/okx_human_trades_log.csv')
print(f"Total Completed Trades: {len(df)}")
for idx, r in df.iterrows():
    pnl_str = f"+${r['net_pnl']:.2f}" if r['net_pnl'] >= 0 else f"-${abs(r['net_pnl']):.2f}"
    print(f"#{r['trade_id']:02d} | {r['symbol']:14s} | {r['side']:5s} | Mode:{r['mode']:6s} | Lev:{r['leverage']:>3s} | Margin:${r['margin']:7.2f} | Entry:{r['entry_time'][:16]} @ {r['entry_price']:9.4f} | Exit:{r['exit_time'][:16]} @ {r['exit_price']:9.4f} | Hold:{r['hold_mins']:4d}m | PnL:{pnl_str:>9s} ({r['roe_pct']:+6.1f}%) | Reason:{r['exit_reason']:24s} | Bal:${r['account_equity']:8.2f}")
