# OKX 均值回归策略

当前 `conf_okx_multi.yml` 启用 BTC、ETH、SOL、XRP、DOGE、SUI、SNDK、ZEC、UNI、ADA 十个 USDT 永续品种，使用项目已有的加密货币配置和当前均值回归实现，杠杆均为 3 倍。原科技股配置保留，但不在当前启动列表中。

| 品种 | 配置名义仓位上限（USDT） | 现有风控下的最高名义仓位（USDT，取整前） |
| --- | ---: | ---: |
| BTC-USDT | 180 | 180 |
| ETH-USDT | 112.5 | 112.5 |
| SOL-USDT | 78.75 | 78.75 |
| XRP-USDT | 78.75 | 78.75 |
| DOGE-USDT | 78.75 | 78.75 |
| SUI-USDT | 78.75 | 78.75 |
| SNDK-USDT | 78.75 | 78.75 |
| ZEC-USDT | 78.75 | 78.75 |
| UNI-USDT | 78.75 | 78.75 |
| ADA-USDT | 78.75 | 78.75 |

`total_amount_quote` 是名义仓位上限，不会再乘以杠杆。当前十个品种的配置合计 922.5 USDT；0.75 USDT 现金止盈、最低 0.6% 止损、0.12% 成本和 `min_reward_risk=0.5` 会把单笔预算限制到不超过 `0.75 / (0.5 × (0.006 + 0.0012)) ≈ 208.33 USDT`，因此十个品种按最低止损计算时都以配置上限为准，合计 922.5 USDT，3 倍杠杆对应初始保证金约 307.50 USDT（不含额外费用和交易所保证金调整）。ATR 止损更宽时，预算会继续缩小。

订单按交易所精度向下取整，低于最小下单量就跳过。价格、合约规格和账户可用保证金仍以 OKX 当时的状态为准。

策略沿用 `pmm_simple` 配置入口，但实现已独立于原做市基类。EMA 顺势信号、双边网格、补仓和追踪止盈均不再参与交易。通用做市基类和其他示例策略保留，避免影响项目其他用途。

## 入场与风控

| 参数 | 当前值 / 行为 |
| --- | --- |
| 均值 | 最近 48 根已收盘的 5 分钟 K 线收盘价均值（4 小时） |
| 入场 | 收盘价和实时中间价均偏离均值 2～3.5 个标准差；低位买入，高位卖出 |
| 极端行情 | 超过 3.5 个标准差不新开仓 |
| 均值止盈 | 开仓时固定目标：均值靠近入场方向 0.15 个标准差；限价平仓 |
| **金额止盈** | **每笔仓位净浮盈 ≥ 0.75 USDT，立即发起市价平仓** |
| 浮盈口径 | 执行器 `net_pnl_quote`：按可平仓方向的盘口价格计算，减已计手续费；不把多个仓位相加 |
| 止盈检查 | 持仓执行器默认约每秒检查；不等待 K 线收盘，不依赖控制器行情指标成功计算 |
| 止损 | `max(0.6%, 1.5 × ATR14 / 入场价)`；所需止损超过 2% 则拒绝开仓 |
| 成本门槛 | 往返手续费和滑点预估 0.12%；净收益空间至少 0.18%，预估净收益/含成本风险至少 1 |
| 现金止盈约束 | 必要时缩小仓位，使预估止损含成本不超过 `0.75 / min_reward_risk` USDT |
| 仓位限制 | 每品种最多一个执行器；已有仓位、部分成交或正在平仓时不新开仓 |
| 下单 | Post-only 限价入场；买卖盘口价差超过 0.2% 跳过；未成交订单最多保留 60 秒 |
| 冷却 | 每次结束后 300 秒，止损结束后 900 秒；同一根信号 K 线最多尝试一次 |
| 超时 | 执行器建立 3600 秒后触发市价退出（包含等待入场时间） |
| 数据异常 | K 线不足、缺口、重复、失效、价格无效时暂停入场并撤销未成交入场单；持仓执行器继续管理退出 |

金额止盈、均值止盈、止损和超时退出以先触发者为准，因此也可能在浮盈不足 0.75 USDT 时因回归均值而退出。金额止盈触发后不会等待进一步回归。0.75 USDT 是触发条件，市价成交后的净利润还会受剩余手续费、滑点和网络延迟影响。

参数是经过逻辑和边界测试的初值，未经过历史收益优化或样本外验证，不代表已验证的盈利能力。成本预估应按实际 OKX 费率调整。

## macOS 启动

当前机器使用 Colima，且 `/Users/Files` 已配置为可写共享目录。Docker 守护进程未运行时先启动 Colima：

```bash
cd "/Users/Files/Vibe Coding/quant_okx_trader"
colima start
make start
```

`make start` 会创建/更新容器，挂载本次修改的策略和执行器，执行只读的导入/配置预检查，再用 `conf_okx_multi.yml` 启动交易。已有机器人会按原有 `--replace` 行为替换。密码沿用本机 `hummingbot/.compose.env` 中的 `HBOT_PASSWORD`；代理沿用现有 `host.docker.internal:7897` 配置。

启动时 `hbot start` 输出的 `running` 仅表示策略进程已启动。`make start` 现在会继续等待当前进程的新状态快照，确认连接器及控制器行情数据就绪，最终输出 `Ready: ...`。默认等待上限为 120 秒，可通过 `make start READY_TIMEOUT=180` 调整。超时会返回失败并显示仍未就绪的依赖；机器人可能仍在运行，此检查不会自动停止或重启交易。

`make stop` 可重复执行：容器不存在、容器已停止或机器人已停止都正常返回；Docker 不可用、暂停状态和实际停止失败仍会报错。`make status` 在容器未运行时直接显示已停止。

对已启动的机器人只检查就绪状态（不重启、不下单）：

```bash
make wait-ready
```

```bash
make status
make logs
make stop
```

只检查代码加载与配置、不启动交易：

```bash
cd "/Users/Files/Vibe Coding/quant_okx_trader/hummingbot"
docker compose up -d hummingbot
docker exec hummingbot python -m scripts.validate_mean_reversion --config conf_okx_multi.yml
```

首次运行需要足够的已收盘 K 线。启动发现同一品种已有未托管的交易所仓位时会暂停新开仓；不会擅自接管已有手工仓位。

从科技股切换到当前五个加密货币，需要在项目根目录执行 `make stop`、`make start`。运行中的进程仍使用启动时读取的控制器列表；更改脚本 YAML 不会自动把旧品种替换掉。此次配置切换未重启实盘进程，也未提交订单。重启后的 `make status` 应显示 `okx_pmm_btc`、`okx_pmm_eth`、`okx_pmm_sol`、`okx_pmm_xrp`、`okx_pmm_doge`。

## 长时间没有挂单的排查

`running` 只表示进程存在。长期显示 `z=n/a | waiting_for_data` 表示连接器或 K 线尚未就绪，尚未进入信号计算。历史 K 线会自动回补，48 根 5 分钟 K 线并不意味着每次启动都必须等待 4 小时。

2026-09-21 排查到两项故障，修复已写入本地文件，需要重启加载：

- 容器只设置了 HTTP(S) 代理。aiohttp 对 `wss://` 使用 `WSS_PROXY`，不会自动沿用 `HTTPS_PROXY`。Compose 已补齐 WS/WSS 代理，避免 WebSocket 意外直连。宿主机代理的 `7897` 端口仍须能从容器访问。
- OKX 私有流断线后，如果下一次重连失败，会再次删除上一轮已经移除的连接，抛出 `list.remove(x): x not in list` 并终止监听任务。现在每轮重置连接引用并检查成员后清理；修复文件已加入容器挂载。

后续切换加密货币时还复现了启动阶段的 REST 连接中断：订单簿、资金费率初始化任务在首次请求失败后直接退出，网络恢复后仍长期显示 `Market connectors are not ready`。这两处现在按交易对每 5 秒重试，已成功初始化的交易对不会重复建立任务，停止时仍可取消。杠杆设置的连接错误最多重试 3 次；交易所明确拒绝不会重试，且控制器在本地尚未确认配置杠杆时显示 `leverage_not_ready` 并禁止新开仓。订单提交接口没有增加自动重试。

在项目根目录执行以下命令加载修复并恢复策略交易：

```bash
make stop
make start
make status
```

先正常停止，再由 `make start` 更新容器和启动策略。仅执行 `docker restart` 不会应用新增的代理环境变量和文件挂载。本次排查只运行了模拟测试与公共行情读取，没有重启实盘策略或提交交易订单。

更新后，等待状态会列出具体依赖，例如 `okx_perpetual[user_stream_initialized]` 或 `okx_perpetual_BTC-USDT_5m[candles=12/50]`。当前使用共享就绪检查，所以任一订阅品种的 K 线未就绪都会暂停新入场。

| 状态 | 含义 / 处理 |
| --- | --- |
| `waiting_for_data: ...user_stream_initialized...` | 账户 WebSocket 未就绪；检查最近日志中的代理、连接或认证失败 |
| `waiting_for_data: ...candles=N/50...` | 指定品种的 K 线尚未补齐；检查其订阅和历史数据请求 |
| `Market connectors are not ready` | 启动几秒内通常是初始化；若持续数分钟，应检查本次启动后的订单簿、资金费率与网络日志 |
| `leverage_not_ready` | 配置杠杆尚未确认，暂停新开仓；检查杠杆设置失败或重试日志 |
| `outside_entry_band`，且有 z 值 | 正常计算中，尚不满足入场条件；不是连接故障 |
| `Insufficient price dispersion` | 最近价格波动低于配置下限，策略主动跳过 |
| `exchange_position_present` | 该品种已有交易所仓位，策略禁止叠加 |
| `Budget below exchange minimum order size` | 预算取整后不足最小下单量，需要核对合约规格及预算 |

策略是均值回归，只有已收盘与实时价格均处于 2～3.5 个标准差的入场区间，且通过点差、成本、收益风险比等检查后才会挂单。恢复连接不代表会立即下单，不应为了强行成交绕过这些检查。

状态中的 BTC、ETH 等基础资产余额为零提示来自通用余额检查；这些 USDT 永续合约使用 USDT 保证金，不要求先买入基础资产。实际资金约束应看可用 USDT 和所需保证金，不能把总余额全部当作可用额度。`No positions held` 指控制器记录的仓位，不代表 OKX 账户一定没有其他仓位。

## 参数文件与验证

实际使用的是 `conf/controllers/conf_okx_pmm_*.yml`。这些本地配置被项目原有 Git 规则忽略；本次参数模板另存于 `strategy_configs/okx_mean_reversion/`，可以纳入版本管理。Windows 打包目录 `../deploy/windows/runtime/` 中的配置和执行代码也已同步。

本机执行单元测试：

```bash
cd "/Users/Files/Vibe Coding/quant_okx_trader/hummingbot"
python3 -m pytest -q test/strategy_unit
```

测试覆盖信号方向、未收盘数据隔离、异常数据、参数校验、收益成本门槛、现金止盈阈值、逐笔计算、禁止叠加仓位及冷却。为不依赖本机未编译的 Hummingbot 扩展，执行策略测试使用框架边界替身并加载真实策略/执行器方法；这不替代完整运行环境和交易所成交测试。
