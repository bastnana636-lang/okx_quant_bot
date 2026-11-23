from pathlib import Path

content = Path("scripts/adaptive_reversal_strategy.py").read_text()

# We can add a function `evaluate_monthly(trades: List[TradeRecord]) -> pd.DataFrame` and update `main()`
print("Current len:", len(content))
