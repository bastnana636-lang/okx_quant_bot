import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

df1 = pd.read_csv('data/monthly_pnl_mode1_fixed_10k.csv')
df2 = pd.read_csv('data/monthly_pnl_mode2_yearly_10k.csv')

plt.style.use('dark_background')
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 12), sharex=False)
fig.patch.set_facecolor('#0b0f19')

# -----------------
# 图1: 每月月初固定 10,000 USDT 本金（每月独立核算）
# -----------------
ax1.set_facecolor('#111827')
months = df1['month'].tolist()
pnl1 = df1['net_pnl'].tolist()
colors1 = ['#10b981' if x >= 0 else '#ef4444' for x in pnl1]

bars1 = ax1.bar(months, pnl1, color=colors1, width=0.65, edgecolor='none', alpha=0.9)
ax1.axhline(0, color='#9ca3af', linestyle='--', linewidth=0.8)
ax1.set_title('Mode 1: Monthly Net PnL (Reset to $10,000 USDT at beginning of EACH month)', color='#f9fafb', fontsize=13, fontweight='bold', pad=12)
ax1.set_ylabel('Monthly Net PnL (USDT)', color='#9ca3af', fontsize=11)
ax1.tick_params(axis='x', rotation=45, colors='#9ca3af', labelsize=9)
ax1.tick_params(axis='y', colors='#9ca3af', labelsize=9)
ax1.grid(True, linestyle=':', alpha=0.3, color='#4b5563')

for bar in bars1:
    h = bar.get_height()
    va = 'bottom' if h >= 0 else 'top'
    y_pos = h + (100 if h >= 0 else -250)
    ax1.text(bar.get_x() + bar.get_width()/2.0, y_pos, f"{h:+,.0f}", ha='center', va=va, color='#f9fafb', fontsize=8, fontweight='bold')

# -----------------
# 图2: 每年年初重置 10,000 USDT 本金（年度连续复利/抗压曲线）
# -----------------
ax2.set_facecolor('#111827')
# 分别展示 Year 1 与 Year 2 净值曲线变化
y1_rows = df2[df2['year'].str.contains('Year 1')]
y2_rows = df2[df2['year'].str.contains('Year 2')]

# 合并展示月度盈亏柱状图
m2_labels = [f"{r['month']}\n({r['year'][:6]})" for _, r in df2.iterrows()]
pnl2 = df2['net_pnl'].tolist()
colors2 = ['#38bdf8' if x >= 0 else '#fb7185' for x in pnl2]

bars2 = ax2.bar(range(len(df2)), pnl2, color=colors2, width=0.65, edgecolor='none', alpha=0.9)
ax2.axhline(0, color='#9ca3af', linestyle='--', linewidth=0.8)
ax2.set_title('Mode 2: Monthly PnL under Annual Continuity (Reset to $10,000 at start of Year 1 & Year 2)', color='#f9fafb', fontsize=13, fontweight='bold', pad=12)
ax2.set_ylabel('Net PnL (USDT)', color='#9ca3af', fontsize=11)
ax2.set_xticks(range(len(df2)))
ax2.set_xticklabels(m2_labels, rotation=45, ha='center', color='#9ca3af', fontsize=8)
ax2.tick_params(axis='y', colors='#9ca3af', labelsize=9)
ax2.grid(True, linestyle=':', alpha=0.3, color='#4b5563')

for bar in bars2:
    h = bar.get_height()
    va = 'bottom' if h >= 0 else 'top'
    y_pos = h + (100 if h >= 0 else -250)
    ax2.text(bar.get_x() + bar.get_width()/2.0, y_pos, f"{h:+,.0f}", ha='center', va=va, color='#f9fafb', fontsize=8, fontweight='bold')

plt.tight_layout()
chart_path = Path('/Users/bastnana/.gemini/antigravity/brain/74c3cd25-283c-402e-8d12-3ab8380ee803/okx_monthly_resets_performance.png')
plt.savefig(chart_path, dpi=200, bbox_inches='tight')
plt.close()
print(f"Saved monthly reset chart to {chart_path}")
