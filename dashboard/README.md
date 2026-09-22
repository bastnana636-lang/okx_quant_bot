# OKX Quant Trader — 实时监控面板

独立的只读 Web 监控面板，**不修改任何现有项目文件和运行逻辑**。

## 快速启动

```bash
cd "/Users/Files/Vibe Coding/quant_okx_trader"
python dashboard/dashboard.py
```

然后浏览器打开：**http://127.0.0.1:8888**

## 功能

| 模块 | 说明 |
|------|------|
| 📊 顶部信息栏 | 策略状态、运行时长、USDT余额、综合盈亏 |
| 📊 绩效汇总表 | 各控制器实时 Z-Score 及信号提示、已实现/未实现 PnL、交易量 |
| 📌 当前持仓 | 持仓方向、数量、价值、盈亏平衡价、浮盈浮亏 |
| 📋 当前挂单 | 交易对、方向、挂单价、数量、挂单时长 |
| ⚡ 最近执行器 | 每个控制器的最近开单记录、盈亏、平仓类型 |
| 📜 实时日志 | 末尾 60 行日志，高亮 WARNING/ERROR |

- **每 3 秒原地更新数字**（不整页刷新，滚动位置保持）
- **`/api/view`** 返回面板补丁数据；**`/api/status`** 返回原始 JSON

## 数据来源

引擎每 5 秒把当前挂单/持仓写入 `status.json`。页面只在首次打开时渲染骨架，之后用 `/api/view` 原地改数字和表格。

- `hummingbot/data/bot/status.json` — 策略实时状态（由引擎定时写入）
- `hummingbot/logs/logs_conf_okx_multi.log` — 运行日志

快照超过 20 秒未更新时，顶部会标 `STALE`。

## 依赖

仅使用 Python 标准库（`http.server`, `json`, `re`, `pathlib`），无需 `pip install`。

## 端口修改

如需修改端口，编辑 `dashboard/dashboard.py` 第 20 行：

```python
PORT = 8888  # 改为你想要的端口
