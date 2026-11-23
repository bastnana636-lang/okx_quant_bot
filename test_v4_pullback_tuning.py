import numpy as np
import pandas as pd
from test_15m_native import load_and_aggregate_15m
from test_v3_math_fix import test_v3, symbols, cached_data

# Let's test a sweep of min_take_profit and max_hold_bars
for min_tp in [0.008, 0.012, 0.016]:
    for hold in [8, 12, 16]: # 2h, 3h, 4h
        all_t = []
        for s in symbols:
            tr = test_v3(cached_data[s], z_long=2.6, z_short=3.2, volume_mult=1.5, max_risk_allowed=0.018, min_take_profit=min_tp, max_hold_bars=hold)
            all_t.extend(tr)
        pnls = [x[6] for x in all_t]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr = len(wins)/len(pnls) if len(pnls) > 0 else 0
        pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else 0
        stalls = sum(1 for x in all_t if x[1] == 'PULLBACK_STALL')
        print(f"min_tp={min_tp:.1%}, hold={hold*15}m: trades={len(all_t)}, WR={wr:.1%}, PF={pf:.2f}, Net={sum(pnls):.2%}, Stalls={stalls}")

