import numpy as np
import pandas as pd
from test_15m_strategy import run_strategy, aggregate_candles, load_data

trades = run_strategy('SOLUSDT', z_thresh=3.0)
df_t = pd.DataFrame(trades, columns=['side', 'reason', 'entry_i', 'exit_i', 'entry_p', 'exit_p', 'net_ret'])
print("Sample losing trades:")
print(df_t.sort_values('net_ret').head(5))
print("\nSample winning trades:")
print(df_t.sort_values('net_ret', ascending=False).head(5))

# Let's inspect the biggest losing trade in detail
raw = load_data('SOLUSDT')
df = aggregate_candles(raw, 900)
c = df['c'].to_numpy()

worst = df_t.sort_values('net_ret').iloc[0]
e_i = int(worst['entry_i'])
x_i = int(worst['exit_i'])
print(f"\nWorst trade: side={worst['side']}, reason={worst['reason']}, ret={worst['net_ret']:.2%}")
print("Price action 10 bars before entry to exit:")
for idx in range(e_i - 10, min(x_i + 5, len(c))):
    marker = " <-- ENTRY" if idx == e_i else (" <-- EXIT" if idx == x_i else "")
    print(f"Bar {idx}: Close={c[idx]:.2f}{marker}")

