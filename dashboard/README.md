# OKX Quant Trader — Bloomberg Slate Cockpit

本地 Web 控制台，采用 **React + TypeScript + Tailwind CSS + Lightweight Charts** 构建前端（静态构建产物位于 `dashboard/dist/`），由零依赖 Python 标准库 HTTP 服务 (`dashboard/dashboard.py`) 托管运行。

## 快速启动

```bash
python3 dashboard/dashboard.py
```

浏览器打开：**http://127.0.0.1:8888**（`make start`、`make dashboard` 与原生安装包会自动拉起）。

## 功能页面

- **Overview 总览**：账户总资产、会话盈亏/峰值统计、Bot Session PnL / Total Assets 双轨曲线、全币种信号与绩效矩阵、一键市价平仓。
- **Markets 行情K线**：TradingView Lightweight Charts 实时 K 线与均值/通道/ATR 动态止损线叠加、Z-Score / 动量震荡副图、实时盘口价差表与持仓联动。
- **Positions 持仓监控**：活跃保证金持仓、手工交易所平仓盈亏录入与撤销台账、活跃限价挂单、执行器生命周期及引擎警告。
- **Config 策略配置**：全量策略参数（包含 `signal_mode`、动量参数、Z-Score、ATR 动态止损、报价/冷静期）、策略预设模板、交易对启停与新增、单币种独立参数覆盖、修改差异对比 (Diff) 与保存并重启。
- **Logs 日志**：策略控制台实时日志流，支持按等级筛选、关键词过滤、暂停与导出。

## API 接口

- `GET /api/view`：结构化面板实时数据（账户、绩效、信号、持仓、挂单、执行器、日志）
- `GET /api/markets`：OKX 永续合约公开盘口与价差
- `GET /api/candles?pair=BTC-USDT&interval=1h&limit=200`：OKX 永续 K 线与服务端叠加指标（离线时自动降级读取 `data/okx_candles/`）
- `GET /api/config` / `POST /api/config` / `POST /api/config/save-and-restart` / `POST /api/config/add-pair`
- `GET /api/logs` / `GET /api/control-log` / `GET /api/presets` / `GET /api/status`
- `POST /api/restart` / `POST /api/stop` / `POST /api/close-position` / `POST /api/manual-close` / `POST /api/manual-close/delete`

## 前端开发与构建

```bash
cd dashboard/frontend
npm install
npm run dev    # 本地热更新开发 (自动代理 /api 到 127.0.0.1:8888)
npm run build  # TypeScript 校验并输出生产包到 dashboard/dist/
```
