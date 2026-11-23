from test_human_sim import run_simulation
import pandas as pd

results = []
for os in [4.0, 5.0, 6.0, 7.0, 8.0]:
    for ob in [93.0, 94.0, 95.0, 96.0]:
        for tp in [0.008, 0.010, 0.012]:
            for rsi_exit in [50.0, 55.0, 60.0]:
                cash, df = run_simulation(base_os=os, base_ob=ob, tp_pct=tp, exit_rsi=rsi_exit)
                if len(df) >= 20:
                    wins = df[df['net_pnl'] > 0]
                    wr = len(wins) / len(df) * 100
                    pnl = df['net_pnl'].sum()
                    losses = df[df['net_pnl'] <= 0]
                    pf = wins['net_pnl'].sum() / abs(losses['net_pnl'].sum()) if len(losses) > 0 else 999
                    results.append({
                        'os': os, 'ob': ob, 'tp': tp, 'exit_rsi': rsi_exit,
                        'trades': len(df), 'wr': wr, 'pnl': pnl, 'pf': pf
                    })

df_res = pd.DataFrame(results)
df_res = df_res.sort_values(by='pnl', ascending=False)
print("Top 15 Parameter Configurations:")
print(df_res.head(15).to_string(index=False))
