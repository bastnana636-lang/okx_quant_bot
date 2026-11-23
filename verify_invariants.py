import pandas as pd
import numpy as np

df = pd.read_csv('data/okx_human_trades_log.csv')
print("=== OKX Aggressive Trader Verification Suite ===")

# 1. Entry time <= exit time and exact hold duration alignment
time_valid = (pd.to_datetime(df['entry_time']) <= pd.to_datetime(df['exit_time'])).all()
duration_mins = (pd.to_datetime(df['exit_time']) - pd.to_datetime(df['entry_time'])).dt.total_seconds() / 60
duration_matches = (duration_mins == df['hold_mins']).all()
print(f"1. Entry <= Exit time: {time_valid} | Timestamp duration == hold_mins: {duration_matches}")
assert time_valid and duration_matches

# 2. Hold duration >= 5m
hold_min_valid = (df['hold_mins'] >= 5).all()
print(f"2. Hold duration >= 5m: {hold_min_valid}")
assert hold_min_valid

# 3. Hold duration <= 1440m (within 1 day)
hold_max_valid = (df['hold_mins'] <= 1440).all()
max_hold = df['hold_mins'].max()
print(f"3. All trades hold duration within 1 day (<= 1440m): {hold_max_valid} (Max hold: {max_hold}m / {max_hold/60:.2f}h)")
assert hold_max_valid

# 4. PnL accounting integrity
sum_pnl = df['net_pnl'].sum()
final_eq = df['account_equity'].iloc[-1]
diff = abs((10000.0 + sum_pnl) - final_eq)
print(f"4. PnL Sum (${sum_pnl:.2f}) matches Final Equity (${final_eq:.2f}): diff={diff:.6f}")
assert diff < 0.05, f"Accounting mismatch: diff={diff}"

# 5. Trade count & win rate
wins = len(df[df['net_pnl'] > 0])
losses = len(df[df['net_pnl'] <= 0])
wr = wins / len(df) * 100
gain = df[df['net_pnl'] > 0]['net_pnl'].sum()
loss = abs(df[df['net_pnl'] <= 0]['net_pnl'].sum())
pf = gain / loss
print(f"5. Total Trades: {len(df)} | Wins: {wins} | Losses: {losses} | Win Rate: {wr:.2f}% | Profit Factor: {pf:.2f}")

# 6. Leverages used
leverages = df['leverage'].unique().tolist()
print(f"6. Leverages used: {leverages}")
assert '15x' in leverages or '10x' in leverages

# 7. Check liquidation distance
max_loss = df['net_pnl'].min()
min_roe = df['roe_pct'].min()
print(f"7. Max single-trade loss: ${max_loss:.2f} ({min_roe:.2f}% ROE) - safely far from liquidation (~-95%)")
assert min_roe > -50.0

print("\n=== ALL INVARIANT CHECKS PASSED PERFECTLY! ===")
