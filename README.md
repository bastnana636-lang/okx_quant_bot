# OKX 多币种永续合约均值回归量化机器人

基于 **Hummingbot V2 控制器框架** 构建的高效、低延迟 OKX 永续合约多币种统计套利与均值回归量化交易系统。具备多层级风控、现金止盈、动态 ATR 止损、单向独立仓位管理以及零依赖的轻量级实时 Web 监控看板。

---

## 目录
- [核心特性](#核心特性)
- [项目架构](#项目架构)
- [支持交易对与参数](#支持交易对与参数)
- [快速开始](#快速开始)
- [常用命令 (Makefile)](#常用命令-makefile)
- [实时监控面板 (Dashboard)](#实时监控面板-dashboard)
- [风控与策略机制](#风控与策略机制)
- [单元测试](#单元测试)
- [安全提示](#安全提示)

---

## 核心特性

- **多币种并行监控与撮合**：单进程并发管理 10 组主流交易对，多空双向捕捉均值回归极端波动套利机会。
- **纯粹统计学信号引擎**：
  - 基于滚动 48 根 5 分钟 K 线计算移动均值与标准差。
  - 标准化 Z-Score 入场区间：`2.0 <= |Z| <= 3.5`，过滤极端行情漂移风险。
  - 出场 Z-Score 阈值（默认 `0.4`），结合点差与净收益风险比双重验证。
- **刚性风控与资金管理**：
  - **禁止加仓/同向叠加**：同一交易对已有交易所仓位或未平挂单时严格禁止重复开仓。
  - **自适应 ATR 动态止损**：动态结合 ATR 与固定比例下限/上限（`0.6% ~ 2.0%`）。
  - **固定现金止盈 (Cash TP)**：支持逐笔达到目标利润额（如 `0.75 USDT`）即刻锁定利润。
  - **非对称冷静期**：止损后冷却 15 分钟，常规交易后冷却 5 分钟，避免震荡损耗。
- **轻量独立实时监控看板**：
  - 原生 Python 标准库开发，零第三方依赖。
  - 毫秒级展示各币种实时 Z-Score、信号状态、多空仓位、活跃挂单、收益及滚动日志。

---

## 项目架构

本项目采用标准清晰的单层生产级量化架构：

```text
quant_okx_trader/
├── bin/                       # 命令行工具 (hbot, hbot-host 等)
├── certs/                     # 认证密钥目录 (受 .gitignore 保护)
├── conf/                      # 策略与运行时配置
│   ├── .gitignore             # 保护 API Key 和加密验证凭据
│   ├── conf_client.yml        # 客户端基础参数
│   ├── conf_fee_overrides.yml # 费率覆盖
│   ├── controllers/           # 10 组交易对控制器配置
│   └── scripts/               # 启动脚本编排 (conf_okx_multi.yml)
├── controllers/               # 核心策略控制器 (mean_reversion.py, pmm_simple.py)
├── dashboard/                 # 实时 Web 监控看板服务 (dashboard.py, README.md)
├── data/                      # 运行时数据 (status.json 状态快照, sqlite 数据库)
├── hummingbot/                # Hummingbot 核心引擎与 OKX 永续合约连接器
├── logs/                      # 策略运行日志
├── scripts/                   # 启动前自检与就绪检查脚本 (validate_mean_reversion 等)
├── strategy_configs/          # 策略模板副本 (纳入 Git 版本管理)
├── test/                      # 单元测试与策略集成测试
├── Dockerfile                 # 生产容器构建定义
├── docker-compose.yml         # Docker 编排配置
├── Makefile                   # 统一命令入口
├── MEAN_REVERSION.md          # 均值回归策略深度原理与排查手册
└── README_CN.md               # 中文说明文档
```

---

## 支持交易对与参数

默认并发运行 10 组 OKX 永续合约交易对（3 倍杠杆，单向持仓模式）：

| 交易对 | 最大名义本金 (USDT) | 杠杆 | 入场 Z 范围 | 出场 Z | 止盈目标 | ATR 止损倍数 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **BTC-USDT** | 180.00 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **ETH-USDT** | 112.50 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **SOL-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **XRP-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **DOGE-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **SUI-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **SNDK-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **ZEC-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **UNI-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **ADA-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |

> 参数模板统一归档于 `strategy_configs/okx_mean_reversion/`，可在实盘修改 `conf/controllers/` 对应文件即时调优。

---

## 快速开始

### 1. 准备工作
- 安装 [Docker](https://www.docker.com/) 与 Docker Compose。
- 确保具备 OKX API Key（包含合约交易读取与下单权限）。
- 配置代理（若处在网络受限环境）：已在 `docker-compose.yml` 中默认配置 Docker 宿主机代理通道。

### 2. 启动机器人
在项目根目录下直接运行：
```bash
make start
```
该命令会自动完成：
1. 后台启动 Hummingbot 容器；
2. 校验 10 组交易对控制器配置与参数模型；
3. 加载并启动多币种策略；
4. 轮询各品种 K 线与行情连接就绪状态；
5. 自动启动 Web 实时监控看板并在浏览器打开。

---

## 常用命令 (Makefile)

| 命令 | 描述 |
| :--- | :--- |
| `make start` | 一键启动容器、校验参数、启动策略并打开监控面板 |
| `make stop` | 优雅停止策略（不撤销已有交易所保护订单） |
| `make status` | 终端打印各交易对实时信号、持仓、已实现与未实现盈亏 |
| `make logs` | 实时跟踪策略容器输出日志 |
| `make wait-ready` | 检查各交易对 K 线订阅与 OKX 账户 WebSocket 连接进度 |
| `make dashboard` | 单独启动本地 Web 监控面板（默认端口 `8888`） |
| `make stop-dashboard` | 停止本地 Web 监控面板服务 |
| `make test` | 执行本地全套策略单元测试（106 项测试） |

---

## 实时监控面板 (Dashboard)

- **访问地址**：[http://127.0.0.1:8888](http://127.0.0.1:8888)
- **技术实现**：基于 Python 标准库 `http.server`，纯前端 HTML/CSS/Vanilla JS 局部局部无刷新更新，每 3 秒自动轮询数据差量。
- **展示板块**：
  1. **资产概览**：实盘 USDT 可用余额、运行时长、全球累计 PnL、整体收益率。
  2. **信号与绩效监控表**：各品种当前实时 Z-Score、信号建议方向（BUY/SELL）、当前阶段状态、累计交易量。
  3. **实时持仓与活跃挂单**：合约开仓价、标记价、未实现盈亏、挂单持续时间。
  4. **执行历史跟踪**：显示最近执行器的平仓原因（TAKE_PROFIT / STOP_LOSS / EARLY_STOP）。
  5. **实盘日志流**：展示最新关键日志，智能高亮 WARNING 与 ERROR。

---

## 风控与策略机制

1. **极端行情过滤**：
   当价格单边暴涨或暴跌导致 Z-Score 超出 `max_entry_z_score` (3.5) 时，系统判定为趋势行情而非波动回归，自动弃单以防止逆势接飞刀。
2. **净边际与收益风险比门槛**：
   开仓挂单前必须满足：
   $$\text{Expected Edge} \ge \text{Round Trip Cost} (0.12\%) + \text{Min Net Edge} (0.18\%)$$
   且预期盈利区间与止损风险比 $\ge 0.5$。
3. **未成交订单超时保护**：
   挂单超出 `executor_refresh_time`（默认 60 秒）仍未成交时，自动取消并释放额度，根据最新偏离度决定是否重新报价。

---

## 单元测试

项目内置了针对执行策略、边界控制、参数校验和生命周期的测试套件，无需依赖 C++ 编译扩展即可在本地极速运行：

```bash
python3 -m pytest -q test/strategy_unit
```
执行结果示例：
```text
106 passed, 2 warnings in 6.09s
```

---

## 安全提示

- **API 凭证隔离**：OKX API 密钥存储于 `conf/connectors/okx_perpetual.yml`，已被根目录与配置级 `.gitignore` 彻底屏蔽，**严禁上传到公共代码仓库**。
- **实盘前测试**：建议在真实资金介入前，使用小额名义本金进行充分的观察与验证。
