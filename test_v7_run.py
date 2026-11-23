import numpy as np, pandas as pd
from test_15m_native import load_and_aggregate_15m
from test_v5_solution import run_smart_exhaustion, symbols, cached_data

# Let's test smart exhaustion with and without trend filter
for tf in [True, False]:
    all_t = []
    res = []
    for s in symbols:
        df = cached_data[s]
        # let's run test_v5 with trend_filter
        # In test_v5, let's see how trend_filter affects it
