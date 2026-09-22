# OKX Quant Trader — 监控与配置面板

本地 Web 面板。监控页只读；**CONFIG** 页把杠杆、开单金额和策略参数写回 `conf/controllers/`。

## 快速启动

```bash
python3 dashboard/dashboard.py
```

然后浏览器打开：**http://127.0.0.1:8888**

`make start` 和 `make dashboard` 也会启动它。

## 页面

顶栏三个入口并列：

| 页面 | 地址 | 说明 |
|------|------|------|
| DASHBOARD | `/` | 余额、Z-Score、持仓、挂单、执行器和日志。每 3 秒原地更新 |
| MARKETS | `/markets` | 当前策略交易对的 OKX 永续公开行情 |
| CONFIG | `/config` | 修改杠杆（默认 3，范围 1–5）、各币种开单金额和策略参数 |

CONFIG 保存后写入当前 `conf/scripts/conf_okx_multi.yml` 启用的控制器文件。正在运行的策略不会热加载，需要 `make stop && make start` 后生效。此页不处理 API 密钥；密钥在首次 `make start` 时写入本机。

- **`/api/view`** 返回监控页补丁数据
- **`/api/markets`** 返回行情页数据
- **`/api/config`** 读取配置；`POST /api/config` 保存配置
- **`/api/status`** 返回原始 JSON

## 数据来源

引擎每 5 秒把当前挂单/持仓写入 `status.json`。监控页只在首次打开时渲染骨架，之后用 `/api/view` 原地改数字和表格。

- `data/bot/status.json` — 策略实时状态（由引擎定时写入）
- `logs/logs_conf_okx_multi.log` — 运行日志
- `conf/controllers/*.yml` — CONFIG 页读写的策略参数

快照超过 20 秒未更新时，顶部会标 `STALE`。

## 依赖

仅使用 Python 标准库，无需 `pip install`。

## 端口修改

如需修改端口，编辑 `dashboard/dashboard.py` 里的：

```python
PORT = 8888  # 改为你想要的端口
```
