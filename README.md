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
├── scripts/                   # 首次配置、启动前自检与就绪检查
│   ├── ensure_setup.py        # 首次运行写入本机密码与加密 OKX API
│   └── write_okx_keys.py      # 在容器内加密 API，不把明文写入仓库
├── strategy_configs/          # 策略模板副本 (纳入 Git 版本管理)
├── test/                      # 单元测试与策略集成测试
├── Dockerfile                 # 生产容器构建定义
├── docker-compose.yml         # Docker 编排配置
├── Makefile                   # 统一命令入口
├── MEAN_REVERSION.md          # 均值回归策略深度原理与排查手册
└── README.md                  # 中文说明文档
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
| **OKB-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |
| **ADA-USDT** | 78.75 | 3x | 2.0 ~ 3.5 | 0.4 | 0.75 USDT | 1.5x |

> 参数模板统一归档于 `strategy_configs/okx_mean_reversion/`。实盘参数以 `conf/controllers/` 为准，可在监控面板的 **CONFIG** 页修改；保存后需要重启策略才会生效。

---

## 快速开始

### 原生安装包（推荐）

从 [Releases](https://github.com/bastnana636-lang/okx_quant_bot/releases/latest) 下载对应安装包：

- **macOS 12+（Intel / Apple Silicon）**：`OKX-Quant-Trader-*-macOS-Universal.dmg`，打开后把应用拖入“应用程序”，再启动 `OKX Quant Trader`。
- **Windows 10/11 x64**：`OKX-Quant-Trader-*-Windows-x64-Setup.exe`，安装后从桌面或开始菜单启动。

安装包会提供系统原生入口，自动执行首次配置、启动策略并打开 Dashboard。交易引擎仍运行在 Linux 容器中，因此两个平台都需要先安装并启动 [Docker Desktop](https://www.docker.com/products/docker-desktop/)；Windows 必须使用 Linux containers 模式，不再需要手动安装 Make、Bash、WSL 或 Python。

当前安装包未使用 Apple Developer ID 或 Windows Authenticode 证书签名。macOS 首次打开时可在 Finder 中右键应用并选择“打开”；Windows 若显示 SmartScreen，请核对下载来源为本仓库 Release 后选择继续运行。

本机配置与日志不会随升级或卸载自动删除：

| 平台 | 本机数据目录 |
| :--- | :--- |
| macOS | `~/Library/Application Support/OKX Quant Trader` |
| Windows | `%LOCALAPPDATA%\OKX Quant Trader` |

### 从源码运行

#### 1. 准备工作
- 安装 [Docker](https://www.docker.com/) 与 Docker Compose。
- 从源码运行需安装 Python 3.11 或更高版本；原生安装包已内置启动器，不需要单独安装 Python。
- 准备 OKX API Key、Secret Key 和 Passphrase（需要合约交易的读取与下单权限）。
- 若本机需要代理才能访问 OKX，准备代理地址。容器里访问宿主机代理时使用 `http://host.docker.internal:端口`，例如 `http://host.docker.internal:7897`。留空则直连。

#### 2. 首次配置
在项目根目录运行对应平台的原生启动器：

```powershell
# Windows 10/11
.\start.cmd
```

```bash
# macOS 12+
python3 native/launcher.py start
```

启动器会自动检查并启动 Docker Desktop。Linux 或已配置 Make 的开发环境仍可使用 `make start`。

如果本机还没有密钥库和 OKX API，启动前会在终端询问一次：

1. 密钥库密码（用来加密 API，请自行保管）；
2. OKX API Key、Secret Key、Passphrase；
3. 可选的 HTTP 代理。

这些内容写入本机，且被 Git 忽略：

| 内容 | 位置 |
| :--- | :--- |
| 密钥库密码、代理 | `.compose.env` |
| 密码校验 | `conf/.password_verification` |
| 加密后的 OKX API | `conf/connectors/okx_perpetual.yml` |

之后再次启动不会重复询问。更换 API 时运行：

```powershell
# Windows
python native/launcher.py replace-keys
```

```bash
# macOS
python3 native/launcher.py replace-keys
```

#### 3. 启动机器人
配置完成后，平台启动器会自动完成：

1. 后台启动 Hummingbot 容器；
2. 校验已启用交易对的控制器配置与参数模型；
3. 加载并启动多币种策略；
4. 轮询各品种 K 线与行情连接就绪状态；
5. 自动启动 Web 监控面板并在浏览器打开。

---

## 常用命令 (Makefile)

| 命令 | 描述 |
| :--- | :--- |
| `make start` | 首次运行时完成本机配置，然后启动容器、校验参数、启动策略并打开监控面板 |
| `make stop` | 优雅停止策略（不撤销已有交易所保护订单） |
| `make status` | 终端打印各交易对实时信号、持仓、已实现与未实现盈亏 |
| `make logs` | 实时跟踪策略容器输出日志 |
| `make wait-ready` | 检查各交易对 K 线订阅与 OKX 账户 WebSocket 连接进度 |
| `make dashboard` | 单独启动本地 Web 监控面板（默认端口 `8888`） |
| `make stop-dashboard` | 停止本地 Web 监控面板服务 |
| `make test` | 执行本地全套策略单元测试 |
| `python3 scripts/ensure_setup.py --replace-keys` | 重新输入并覆盖本机 OKX API |

---

## 实时监控面板 (Dashboard)

- **访问地址**：[http://127.0.0.1:8888](http://127.0.0.1:8888)
- **页面**：顶栏有三个入口，**DASHBOARD**、**MARKETS**、**CONFIG**。
- **技术实现**：基于 Python 标准库 `http.server`。监控页用纯前端局部更新，每 3 秒轮询一次。

### DASHBOARD

1. **资产概览**：实盘 USDT 可用余额、运行时长、全球累计 PnL、整体收益率。
2. **信号与绩效监控表**：各品种当前实时 Z-Score、信号建议方向（BUY/SELL）、当前阶段状态、累计交易量。
3. **实时持仓与活跃挂单**：合约开仓价、标记价、未实现盈亏、挂单持续时间。
4. **执行历史跟踪**：显示最近执行器的平仓原因（TAKE_PROFIT / STOP_LOSS / EARLY_STOP）。
5. **实盘日志流**：展示最新关键日志，智能高亮 WARNING 与 ERROR。

### MARKETS

只展示当前策略交易对的 OKX 永续公开行情：最新价、24 小时涨跌、买卖价、点差和成交额。

### CONFIG

用来修改实盘参数，写入 `conf/controllers/` 里当前启用的控制器：

- **杠杆**：默认 3，允许 1 到 5。可以用「应用到全部币种」，也可以按币种单独填写。
- **开单金额**：每个币种的单笔最大名义本金（USDT）。
- **现金止盈**，以及 Z-Score、止损、冷静期等策略参数。策略参数保存后对全部已启用币种统一生效。

正在运行的策略不会热加载这些文件。保存后执行 `make stop && make start` 才会用上新参数。CONFIG 页不显示、也不修改 API 密钥。

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
113 passed, 2 warnings
```

---

## 安全提示

- **API 凭证隔离**：OKX API 经密钥库密码加密后存放在 `conf/connectors/okx_perpetual.yml`。密码和代理在 `.compose.env`。这两处都被 Git 忽略，**不要提交，也不要发给别人**。拿到仓库副本的人没有你的密码和 API，无法直接交易。
- **本机密码等于启动钥匙**：它保存在本机是为了后续启动不用重复输入。能读取该文件的人可以启动机器人。
- **实盘前测试**：建议在真实资金介入前，使用小额名义本金进行充分的观察与验证。
