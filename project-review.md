# OKX Quant Trader 扩展审查

审查日期：2026-09-28。审查对象为当前工作区，包括已暂存、未暂存及既有实现。仅审查，没有修改业务代码、配置，也没有连接交易账户、启动/停止机器人或提交订单。

本报告已统一收录：首轮 3 项问题、扩展审查 17 项问题、持仓一致性专项 8 项缺口，以及本轮 PnL 展示与重启清零专项结论和修改建议。专题之间有交叉，不将重复根因简单累加为独立问题。P1 为应优先修复的交易、控制或配置安全问题；P2 为功能、数据和交互正确性问题。

更新说明：用户要求此前所有问题均进入本报告。持仓专项全文已合并在后文，原独立报告仅保留为历史参考；本轮只修改审查材料，没有修改业务代码、配置或运行状态。

## 覆盖与验证边界

检查了当前部署链路的均值回归信号、入场预算、执行器退出与成交跟踪、OKX 持仓解析、启动核对和持久化、状态快照、面板接口、配置编辑、原生启动器、打包脚本、Docker 挂载、就绪检查及相关测试。通用 Hummingbot 框架按实际调用链检查；未逐文件审计仓库附带的所有其他交易所连接器、示例策略及第三方依赖。

验证包括生产方法 AST 隔离执行、临时目录配置读写、模拟 HTTP Handler 请求和浏览器中的静态隔离面板。模拟数据不代表真实账户状态。浏览器验证使用 1280 像素视口，未进行完整手机矩阵测试。

策略单测结果沿用前轮同一代码状态的 `python3 -m pytest -q test/strategy_unit`：128 通过、1 失败。失败位于 test_execution_policy.py:286，配置止盈为 1.2，而断言仅接受 1 或 2。三个框架测试文件因缺少 async_timeout、ruamel、tabulate 无法收集；隔离方法复现不替代完整交易环境测试。

## 新发现：交易和控制

### 1. [P1] OKX 持仓数量随价格变化，且被错误向整数放大

位置：[hummingbot/connector/derivative/okx_perpetual/okx_perpetual_derivative.py:659](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/connector/derivative/okx_perpetual/okx_perpetual_derivative.py:659>)。

`get_position_amount()` 使用 `notionalUsd / avgPx`，再执行 `max(amount, round(amount))`。notionalUsd 是当前美元名义价值，avgPx 是历史开仓均价，二者不能推回实际合约数量。举例：真实持仓 0.6、入场 100、现价 110，传入 notionalUsd=66 后返回 1，而不是 0.6，已隔离复现。

控制器直接按 account_positions.amount 提交手动/固定止盈平仓。当前 ONEWAY 下使用普通反向单且没有 reduceOnly，错误数量可能使平仓变成反向开仓。应从 pos 合约张数及 ctVal 换算基础资产数量，去掉整数放大，并对明确平仓路径使用交易所支持的只减仓保护。

### 2. [P1] 撤单会丢失已成交数量，可能重复平仓或遗留敞口

位置：[hummingbot/strategy_v2/executors/position_executor/position_executor.py:728](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:728>)；同文件 :87、:123。

撤单回调把 `_open_order`/`_close_order` 放进 `_failed_orders` 后清空；成交量属性只读当前订单，不累计已撤订单。已复现：平仓成交 0.4 后撤销，close_filled_amount 从 0.4 变成 0。原持仓为 1 时，下一单可能仍按 1 平仓，而不是剩余 0.6。开仓部分成交后撤销则相反，open_filled_amount 会变成 0，真实仓位可能失去管理。

需按订单 ID 累计所有开/平成交量与费用，撤单只结束订单生命周期，不能清空已有成交事实。新增限价平仓及前轮发现的全量撤单逻辑会更频繁触发这条路径。

### 3. [P1] 部分成交持仓没有止损检查

位置：[hummingbot/strategy_v2/executors/position_executor/position_executor.py:510](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:510>)；[controllers/market_making/pmm_simple.py:347](</Users/Files/Vibe Coding/quant_okx_trader/controllers/market_making/pmm_simple.py:347>)。

止损和普通止盈只在 `_open_order.is_filled` 为真时检查，而控制器又跳过任何已有成交量的入场单刷新。部分成交但余单一直挂着时，即使亏损超过止损，执行器也只继续检查金额止盈和总时限，可能等待至 3600 秒。生产方法隔离执行确认 partial fill 情况下 control_stop_loss 未被调用。

应按真实已成交敞口触发止损，同时取消未成交余量并在成交/撤单竞态后核对剩余数量。

### 4. [P1] 安装包内面板的 RESTART/STOP 不会执行实际控制

位置：[dashboard/dashboard.py:90](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:90>)；[native/launcher.py:318](</Users/Files/Vibe Coding/quant_okx_trader/native/launcher.py:318>)。

面板统一启动 `[sys.executable, launcher.py, action]`。PyInstaller 打包后 sys.executable 是启动器自身，不是 Python；其 argparse 只接受 start/stop 等命令，会把 launcher.py 路径当成非法命令。模拟该参数组合确认退出码为 2。面板仍先返回 202“命令已发送”，且不展示子进程最终失败。

应复用支持 frozen 模式的启动命令构造，打包态仅传 action，并向页面暴露操作完成/失败状态。

### 5. [P1] 配置写入接口遗漏来源和本机校验

位置：[dashboard/dashboard.py:3052](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:3052>)。

restart、stop、close-position、manual-close 都检查本机地址及自定义请求头；`/api/config` 没有同等检查，也不验证 Origin 或 Content-Type。模拟 Origin 为外站、Content-Type 为 text/plain、无校验头的请求，仍调用 save_config 并返回 200。

这使能访问服务的客户端可直接改交易配置；浏览器跨站是否能到达还取决于浏览器本地网络防护，应用自身未防护。如果服务通过 DASHBOARD_HOST 暴露到网络，风险更直接。manual_kill_switch 又是运行时可更新字段，不能假定全部改动必须重启才产生影响。应统一写接口校验、验证允许的 Origin/Host，并使用 CSRF 防护。

### 6. [P2] “现金止盈”和均值止盈被固定浮盈目标覆盖

位置：[controllers/market_making/pmm_simple.py:432](</Users/Files/Vibe Coding/quant_okx_trader/controllers/market_making/pmm_simple.py:432>)；[hummingbot/strategy_v2/executors/position_executor/position_executor.py:300](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:300>)。

执行器金额目标取 `fixed_unrealized_tp_quote or take_profit_quote`；其限价止盈价也优先按金额目标计算，完全绕过均值目标。当前面板现金止盈填 1.2、固定止盈填 1.5，实际正常退出不会在现金浮盈 1.2 时触发，均值先回归也不再触发原均值目标。take_profit_quote 仍用于风险预算，界面却描述为独立止盈条件。

应明确三个条件是独立竞争退出，还是单一目标；如要独立，分别保留触发与限价目标，不能以一个配置覆盖另一个。

## 新发现：前端和数据

### 7. [P2] 没有状态文件时整个控制面板返回 500

位置：[dashboard/dashboard.py:830](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:830>)、:2651。

read_status 失败后 build_view 仅返回 error；render_html 仍直接读取 status_html、uptime_s 等必需字段。已复现 `render_html({'error':'missing status'})` 抛 KeyError('status_html')。首次启动、状态文件损坏或暂时不可读时，概览与配置路由都可能不可用。启动器虽然提前启动面板，用户仍看不到可操作的错误页。

应给错误/初始化态完整默认模型，始终渲染控制和配置界面，缺失数据显式显示“未知”。

### 8. [P2] 正在持仓却显示“暂无活跃持仓”

位置：[dashboard/dashboard.py:414](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:414>)、:920；[hummingbot/strategy_v2/executors/executor_orchestrator.py:819](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:819>)。

面板只解析 Positions Held。该报表来自 positions_held，用于已转交的持仓，不包含运行中的 PositionExecutor，也不等于交易所 account_positions。正常运行中的执行器可有真实敞口和浮亏，但持仓页仍显示“当前暂无活跃持仓”。隔离面板确认浮亏 -2 与该空态同时出现。

应输出结构化交易所仓位、执行器管理归属和本地持仓信息；“无本地接管记录”不能表述成“无仓位”。

### 9. [P2] Z 值超出禁止入场区间仍显示 BUY/SELL 信号

位置：[dashboard/dashboard.py:863](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:863>)。

前端固定以 ±2 判断方向，没有使用控制器 signal、max_entry_z_score、收盘确认和禁止入场原因。已复现 z=4.5、reason=outside_entry_band 时页面显示“⚡ +4.50 (SELL)”，当前配置最大入场 Z 仅 3.2。冷却、已有仓位等状态也不会阻止此标签。

应区分“价格偏离方向”和“允许入场信号”，显示真实 signal 与阻塞原因，阈值取实际运行配置。

### 10. [P2] 断线后 LIVE/ON 保留，页脚永久宣称在线

位置：[dashboard/dashboard.py:2152](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:2152>)、:2400、:2920。

轮询失败被空 catch 吞掉；前端定时器只把快照年龄变红，不把 LIVE/ON 改为未知或断线。收到 error 对象还会用空字符串覆盖 stale-banner。页脚 ENGINE ONLINE 是硬编码文字，LATENCY 也仅用首次渲染值。

隔离浏览器中快照已过期而 LIVE 和页脚 ENGINE ONLINE 仍保留。应维护统一连接状态，按最后成功快照时间驱动所有在线标记，并明确区分“引擎运行”“数据新鲜”“控制请求完成”。

### 11. [P2] 行情页未启动定时刷新

位置：[dashboard/dashboard.py:2144](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:2144>)、:2175、:2443、:2554。

MARKETS_POLL_MS=4000 仅被声明；pollMarkets 只在切换到 markets 时调用。用户一直停留行情页，行情价格和涨跌幅不会周期刷新。应在可见时启动轮询、离开时暂停，并标注行情自己的数据时间。

### 12. [P2] 运行时长字段不匹配，数字停在打开页面时

位置：[dashboard/dashboard.py:974](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:974>)、:2421。

API 返回 uptime_s，JS 却读取 uptime_str 或 uptime，二者均不存在，patch 不更新该节点。应统一字段并根据最新运行秒数格式化，或用稳定 start_time 推进前端计时。

### 13. [P2] “当前生效”策略元数据是常量

位置：[dashboard/strategy_config.py:22](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/strategy_config.py:22>)、:308；[dashboard/dashboard.py:1091](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:1091>)。

元数据固定为 5m、48 根、3600 秒，并始终标注“生效中”。临时配置改为 15m、20 根、1800 秒并成功保存后，读取的 strategy.interval 仍是 5m。保存的磁盘值也不等于运行进程已加载的值。

应分别展示运行配置和待重启配置；参数摘要从对应数据生成，不能用常量或保存成功推断已经生效。

### 14. [P2] 手动平仓标记会长期隐藏新仓，并可能重复计入盈亏

位置：[dashboard/dashboard.py:534](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:534>)、:549。

标记没有订单 ID、控制器 ID、会话或成交时间，仅按 pair/side/amount/breakeven 匹配，且永久累加手工盈亏。未来相同参数的新仓会被隐藏；若执行器本身已记录该笔实现盈亏，手工数值会重复计入。已复现相同新仓可见数为 0，已有已实现 1.2 又手工记 1.2 后变为 2.4。

应把手工调整作为有来源、有撤销入口的独立流水，关联实际成交或明确会话，并和引擎去重；不应用展示层标记删除真实持仓视图。

### 15. [P2] 重启请求被拒绝也会清空盈亏基线

位置：[dashboard/dashboard.py:102](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:102>)。

restart_strategy 先 reset_session_baseline，再调用 _launch_control。若已有操作进行、启动器缺失或进程创建失败，重启没有发生，但基线和历史已经丢失。模拟 _launch_control 返回 busy 后确认 session_key 被清空。

应在检测到新 bot 会话后切换基线；请求受理不等于策略已重启。

### 16. [P2] 常见桌面宽度下 STOP 按钮超出视口

位置：[dashboard/dashboard.py:1311](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:1311>)、:2126。

1280 像素浏览器实测页面宽 1327，STOP 按钮位于 x=1268.55 至 1327.14，只有左侧小部分可见。响应式重排只在 900 以下启用，中等宽度仍将品牌、五个导航页和控制区放在一行。

应让控制按钮保持完整可见，提前换行或缩减导航，至少覆盖 1024/1280/1440 和手机宽度。

### 17. [P2] 保存配置会默默启用原本关闭的固定止盈

位置：[dashboard/strategy_config.py:130](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/strategy_config.py:130>)、:224。

策略模型的 fixed_unrealized_tp_quote 是可选字段，缺省为 None；配置编辑器却把缺省值补成 2，并要求大于 1。临时文件删除该字段后，加载、原样保存便写入 fixed_unrealized_tp_quote: 2，已复现。用户只想改杠杆也可能改变退出行为，且前端没有关闭选项。

应保留 None/关闭语义，提供明确启用开关，不把展示缺省值写成交易指令。

## 前轮已确认，仍需修复

- [P1] [position_executor.py:396](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:396>)：手动退出与配置了现金目标的止损/超时重试按盈利价格挂限价单；亏损时无法及时退出。
- [P1] [pmm_simple.py:190](</Users/Files/Vibe Coding/quant_okx_trader/controllers/market_making/pmm_simple.py:190>)：固定止盈每轮撤销全部未完成订单，包括正在执行的平仓单；可能造成撤单/重下循环及重复平仓。
- [P1] [executor_orchestrator.py:383](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:383>)：把 OKX `_update_positions()` 正常返回当作全部查询成功，但其内部吞掉单个请求异常，缓存为空时可能删除真实本地持仓。

## 其他交互改进（不与上述缺陷重复计数）

- 风控字段输出 class=field，样式主要定义 field-card/field-label，实际浏览器呈现为紧挨标签的默认控件；应统一组件、间距和焦点样式。
- 止损、点差等比率使用 0.006/0.002，小数单位容易误解。建议以百分比输入，保存时明确转换；名义本金与保证金继续清楚区分。
- 日志筛选和清屏会被下一轮 logbox.innerHTML 更新覆盖，筛选框内容与可见日志不一致。应保存过滤状态，更新后重新应用。
- “RETURN%”实际为盈亏/成交额，不是资金收益率；应改名或显示明确公式。盈亏来自格式化文本且源头只保留两位，界面显示四位不代表四位精度。
- MEAN_REVERSION.md 中仍有 3 倍、0.75 USDT、旧币种数量、旧预算及市价止盈说明，与当前 5 倍、1.2/1.5 配置及限价实现不一致。
- 模板、实际配置与测试断言未形成单一真源；当前失败用例不应简单扩大允许值，而应明确哪些参数允许本机覆盖。

## 建议处理顺序

1. 先修持仓数量、累计成交、部分成交止损，以及前轮的退出/撤单问题，用故障和竞态测试验证不会反向开仓。
2. 修配置写接口、打包版控制链路，并让前端展示真实完成状态。
3. 用结构化状态取代格式化文本解析，统一交易所持仓、策略敞口和盈亏口径。
4. 修错误空态、实时刷新、配置生效态和布局；同步文档及测试。


## 持仓一致性专项（已合并）

日期：2026-09-28。要求：以当前 API 所属 OKX 账户实际持仓为准，策略 start、stop、restart 不应改变账户观察的可用性或把未知状态变成空仓。此次仅审查和只读验证，没有修复代码，也没有启动、停止、重启机器人或提交交易。

### 结论

当前实现不满足要求。Dashboard 和 make status 同时读取本地策略状态，不等于同时读取了真实账户。策略停止后没有独立持仓采集者；重启核对也只是删除部分本地记录，不能保证恢复到交易所实际数量。

“始终一致”必须有明确的数据时间和失败语义：成功获取同一账户、同一范围、同一版本的持仓时，两个入口应一致；OKX 不可达时只能显示最近成功快照及过期标记，不能承诺实时一致，更不能显示空仓。价格和浮盈持续变化，跨时刻读数不能要求逐位相等。

### 现场只读结果

- Docker 中 hummingbot 容器存在且运行，但实际 `make status` 显示策略 stopped，仅报告进程状态和历史错误，没有持仓表。
- 宿主机 data/bot/status.json 检查时约 30 分钟未更新，running=false，format_status 为空，没有 balances、positions 或 account_positions 字段。
- 已核对容器中实际 status.py 与本地审查文件哈希相同。
- 使用现有容器密钥在内存中签名，只调用 OKX GET /api/v5/account/positions；请求返回 HTTP 403，未获得可验证的 OKX 业务响应。因此无法确认此刻账户是否有仓位、具体数量或是否与本地一致。没有输出密钥或解密密码。

### 必须修复的缺口

#### 1. [P1] 账户观察依赖策略进程，停止后不再更新

[engine.py:118](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/cli/engine.py:118>) 停止周期快照和连接器；[status.py:84](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/cli/commands/status.py:84>) 在机器人停止后直接使用空 snapshot。

Dashboard 仅读文件；make status 不独立查询账户。停止后在 OKX 手动加仓、减仓、平仓，两个入口都无法跟进。容器停止时 scripts/hbot_control.sh 更会直接输出 stopped 后退出。

需要把只读账户采集与交易策略生命周期分离。面板运行时采集必须继续；make status 应读取同一份账户快照，不能用策略是否运行决定账户是否有仓位。

#### 2. [P1] 停止/初始化/状态生成失败被解释成零资产、零盈亏、无仓位

[engine.py:75](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/cli/engine.py:75>) 最终停止快照不采集余额，format_status 也可能为空；[dashboard.py:833](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:833>) 把缺失数据默认成空字典和零。

隔离复现：输入刚生成的停止快照，页面结果为资产 0.00、盈亏 +0.0000、“暂无活跃持仓”，快照徽标却为 LIVE。即使真实账户还有仓，这个页面也会如此显示。

必须区分“成功查询且确认为空”“尚未查询”“查询失败”“过期缓存”。只有第一种可以显示无仓位；其他情况保留最后成功值或显示未知。

#### 3. [P1] 展示源是本地 PositionHold，不是账户实际仓位

[dashboard.py:414](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:414>) 解析 format_status 的 Positions Held；[executor_orchestrator.py:819](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:819>) 只汇总 positions_held。

运行中的 PositionExecutor、手工仓位、未归属控制器的仓位均不保证进入这张表。面板还通过手工平仓标记隐藏匹配记录，而 make status 不使用同样的标记，两个入口可以直接互相矛盾。

账户持仓视图必须来源于结构化交易所数据；策略归属只能作为附加信息。手工备注不能隐藏或篡改这张真实仓位表。

#### 4. [P1] 重启核对只检查是否存在，不同步数量、均价，也不补回缺失仓位

[executor_orchestrator.py:391](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:391>) 将实时仓位降为 connector/pair/side 集合。只要同方向存在，就完整保留旧本地记录；不会读取新数量和均价，也不会把交易所有、本地没有的仓位加入账户展示。

隔离验证：设交易所同方向仓位已由 1 减至 0.4、均价变为 105，本地记录仍保留数量 1、均价 100；本地记录为空时，即使交易所有同方向仓位，核对后仍为零行。

账户视图必须完整替换成功查询范围内的仓位事实。交易策略是否接管该仓位是另一项决策，不应成为是否展示它的条件。

#### 5. [P1] REST 成功返回空列表时，旧持仓缓存不会被清理

[okx_perpetual_derivative.py:618](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/connector/derivative/okx_perpetual/okx_perpetual_derivative.py:618>) 仅遍历响应返回的记录，data=[] 时不执行任何清理。

如果 WebSocket 漏掉平仓事件，REST 已明确返回该交易对无仓，旧 account_positions 仍可保留，后续核对会把它当作有效实时仓位。隔离执行该方法确认 data=[] 时 remove_position 从未调用。

应按每次查询的明确范围执行快照替换：成功空结果清空该范围，失败结果保留旧值并标记失效，不得混用。

#### 6. [P1] 当前查询范围与“我的 OKX 持仓”范围不一致

[okx_perpetual_derivative.py:604](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/connector/derivative/okx_perpetual/okx_perpetual_derivative.py:604>) REST 只查询配置中的 trading_pairs；[executor_orchestrator.py:258](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:258>) 只恢复当前控制器和市场对应的数据库记录。

切换策略列表、去掉某个币种或手工交易列表外品种后，账户可能仍有仓，但本地恢复/REST 兜底无法提供完整列表。当前策略虽是十个 USDT 永续品种，账户总持仓不应被默认等同于这十个控制器。

建议同一 API 账户先获取完整持仓，再由页面筛选“全部账户持仓/本策略相关”；必须展示账户与产品范围。不能声称覆盖 API 无权访问的其他子账户或其他账户。

#### 7. [P2] 文件更新时间不证明账户数据已更新

[engine.py:69](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/cli/engine.py:69>) 在采集前就生成 updated_at；写快照仅格式化已有缓存，不强制确认持仓成功刷新。`hbot status` 的刷新请求只等待文件时间增加，超时也没有要求持仓数据具备成功更新时间。

因而机器人存活、快照新鲜、账户持仓新鲜是三种不同状态。应单独记录 last_successful_account_sync、采集错误和统一 snapshot_id，并由两个入口采用同一新鲜度规则。

#### 8. [P2] stop 入口语义不统一

[Makefile:121](</Users/Files/Vibe Coding/quant_okx_trader/Makefile:121>) 的 make stop 保留 Dashboard；[native/launcher.py:284](</Users/Files/Vibe Coding/quant_okx_trader/native/launcher.py:284>) 的 stop 会连 Dashboard 一起关闭。面板 STOP 走后者。

如产品要求面板独立运行，停止交易与退出面板应分开。更重要的是，即便面板保留运行，也必须有独立账户采集，不能只留下一张不再更新的页面。

### 与此前缺陷的关系

此前已确认的数量换算错误、撤单后丢失成交量、查询失败误删本地记录、永久手工平仓覆盖都会进一步破坏一致性。重启盈亏基线只属于策略会话报表，不能拿它重置账户余额、账户持仓或交易所浮盈。

### 生命周期验收矩阵

| 场景 | 当前行为/问题 | 修复后的必要结果 |
| --- | --- | --- |
| 策略运行，执行器有敞口 | 本地持仓表可能为空 | 两端显示同一账户仓位及归属 |
| make stop，面板继续运行 | 快照停止；CLI 不报告账户仓位 | 策略 stopped，账户采集继续 |
| 面板 STOP | 原生启动器关闭面板 | 停交易与退出面板有独立操作 |
| 停止后在 OKX 手动减仓 | 无采集；旧数量无法更新 | 两端更新为实际剩余量 |
| 停止后在 OKX 手工开新仓 | 本地无记录 | 两端显示未归属策略的真实仓位 |
| restart 时本地仓位数量过时 | 同方向即保留旧数量 | 用最新成功账户快照展示 |
| start 更换交易对列表 | 列表外仓位可被遗漏 | 账户全量视图仍保留 |
| WebSocket 漏事件，REST 返回空仓 | 旧缓存不清理 | 清空成功查询范围，两个入口同步 |
| OKX 返回 403/超时 | 无法确认实时账户状态 | 明确失败/过期，不显示确认为空 |
| 引擎崩溃，面板还在运行 | 旧快照可能继续显示 ON | 引擎离线，账户观察不依赖引擎 |
| 仅重启 Dashboard | 读取旧文件、可能丢失展示上下文 | 恢复最近账户快照并立即重新采集 |

这些场景未对实盘执行 start/stop/restart 操作；表中“当前行为”由调用链与隔离复现支撑，不能当作实盘全流程验收通过。

### 修复应遵守的数据约定

- 唯一账户持仓事实源：OKX 成功响应，保留 instId、posSide、mgnMode、ccy 及账户作用域；以 pos 合约张数结合合约规格计算基础资产数量。
- 单一只读采集服务：独立于交易引擎生命周期；两个展示入口使用同一规范化快照，避免各算各的。
- 显式新鲜度：snapshot_id、source、account_scope、last_success_at、error、stale；来源或权限不确定时不声称已核实。
- 停止不会清空账户快照；只有完整成功查询的空结果才表示空仓。
- 策略盈亏、账户浮盈和资金权益分开命名；策略重启只影响会话统计，不改变账户事实。
- 所有生命周期/故障测试先使用模拟 OKX 和隔离目录；实盘只读核对成功后再声明账户一致。未经测试不能给出“一直一致”的保证。

## PnL 展示与重启清零专项审查

### 判断：账户数据不能清零，会话统计可以另起一段

当前逻辑将“策略累计已实现差值”“持仓全生命周期浮盈”“历史成交额”和“最近约 150 个点”拼成同一组“本次/全局”指标，口径不一致。重启策略并不能消除真实亏损、改变持仓成本或清零交易所浮盈。

建议把界面分成两组：

| 指标 | 正确的数据来源/含义 | restart / stop / start 的处理 |
| --- | --- | --- |
| 账户持仓、均价、仓位浮盈 | OKX 当前账户对应字段，保持与交易所相同币种、估值口径和时间 | 不清零；不可达显示过期或未知 |
| USDT 权益 | 当前账户 USDT 权益；如果只读取 USDT，不应称多币种“总账户资产” | 不清零；不把缺失值补零 |
| 账户期间净损益 | 同一账户、币种及时间范围内的已实现、浮盈变动、手续费、资金费等；核对资金划转 | 按选定期间展示，不由策略进程重启隐式重置 |
| 策略累计净 PnL | 可归属于策略的完整成交流水、费用和未平仓估值 | 跨进程持久化，不随面板重启改变 |
| 本次策略经济收益 | 从确定的策略会话开始时刻计算净损益增量，包括跨会话持仓的估值变化 | 新策略会话从 0 起，保留旧会话，可回看 |
| 本次已实现 / 当前浮盈 | 前者为会话内实现变动，后者是当前仓位的全生命周期浮盈 | 可以并列展示，但不能未经调整直接称为“本次收益” |

不要为了三个入口显示相同而把错误的策略 PnL 复制给账户指标。相同名称、相同数据范围与相同快照必须相同；不同口径的指标要明确命名。

### PNL-01 [P2] 基线取自首次页面读取，漏掉启动后已经发生的收益

位置：[dashboard.py:763](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:763>)。

基线在 `_sync_session_metrics()` 首次看到控制器时，直接设为当前累计 realized。它不是策略开始时的真实值。若启动时累计已实现为 10，用户晚些打开 Dashboard 前已盈利 2，首次读到 12 后页面会显示本次已实现 0，而不是 2。新增控制器晚到时也会发生同样问题。

**复现：**首次传入 realized=12，返回本次 realized=0。

**修改建议：**由后端在建立会话时确定持久化 session_id 和账本游标/基线，或按会话时间及成交 ID 重算；Dashboard 只读展示。基线不可用时显示“本次统计初始化中”，不能把首次页面值当作开机值。

### PNL-02 [P2] 仅已实现清零，旧浮盈仍算入“本次收益”

位置：[dashboard.py:787](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:787>)。

当前算法是 `session_realized = R(t) - R(t0)`，`global_pnl = session_realized + U(t)`，没有处理 U(t0)。有旧仓的重启后，界面文案说“重启清零”，但本次收益不为零。

**复现：**重启基线时 R=10、U=6，返回 `(realized=0, unrealized=6, pnl=6)`；随后原价平仓、R=16、U=0，返回 `(6,0,6)`。这 6 是重启前已存在的浮盈，不能全部归属为新会话经济收益。

**修改建议：**保留真实 U(t) 不清零；独立计算本次收益。若策略归属和费用口径稳定，基础公式为 `ΔR_net + ΔU = [R_net(t)-R_net(t0)] + [U(t)-U(t0)]`。手续费、资金费应在净账本中只计一次；归属转移要有账本事件。账户权益法则需另扣净入金/划转，并在多币种时统一估值。若产品只想看本次落袋金额，应直接称“本次已实现”，不要将它与当前旧仓浮盈相加冒充本次收益。

### PNL-03 [P1] 本地持仓汇总漏扣手续费，和执行器净盈亏不一致

位置：[executor_orchestrator.py:902](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:902>)；[data_types.py:77](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/data_types.py:77>)。

汇总注释称 PositionSummary.realized_pnl_quote 已扣费，但 PositionHold 累加的是成交价差，费用单独存放。PositionSummary.global_pnl_quote 会减 cum_fees_quote，而 generate_performance_report 直接相加 realized 和 unrealized，没有减费用。运行中执行器用 net_pnl_quote，又是另一种口径。

**复现：**成本 100、当前价 101、数量 1、累计费用 0.2，PositionSummary 净 PnL=0.8，而 performance_report=1。该报表也被固定浮盈止盈读取，因此不只是显示误差，还可能提前触发退出。

**修改建议：**统一 gross_realized、fees、funding、net_realized、unrealized 字段含义，在汇总层明确只扣一次费用；恢复持仓、转 PositionHold、平仓三个阶段必须满足同一经济净值的不变性。

### PNL-04 [P2] “RETURN%”分子是本次收益，分母却是历史累计成交额

位置：[dashboard.py:543](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:543>)、[dashboard.py:795](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:795>)。

realized 减掉启动基线，volume 未减。历史成交越多，相同的本次收益就显示越小的百分比；该指标也不是账户资金回报率，且成交额通常累计双边成交。

**复现：**本次赚 2、历史累计 volume=10100，显示 0.02%。如果本次 volume=100，则本次成交额收益率应为 2%；账户资金回报率还需要独立资金分母。

**修改建议：**选择并明确一种指标：`本次净收益 / 本次成交额`（名称明确包含成交额，零分母显示“—”），或采用有资金流调整的资金回报率。保留累计成交额时，同时展示累计同口径收益，不混用会话窗口。

### PNL-05 [P2] 控制器暂时缺失或数据失败会改写总 PnL 和曲线

位置：[dashboard.py:787](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:787>)、[dashboard.py:813](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:813>)。

总计只对当前快照中出现的控制器求和；空性能表被当成零。状态生成失败、初始化期间、控制器停止/被移除，会使历史已实现收益突然消失，并产生伪造的零资产/零盈亏点。这与持仓专项的未知状态误当空仓属于相同根因。

**复现：**两个控制器本次分别赚 2、3，总计为 5；下一快照只出现第一个后总计变 2。空行表和缺失余额会写入 `{bot_pnl:0, assets:0}`。

**修改建议：**后端维护完整会话账本及控制器生命周期，已实现收益不能因控制器退出而从会话总计移除。对缺失/失败数据拒绝写“有效采样点”，保留最后成功值，图表显示断点与失效时间。真正清空仓位必须来自成功账户查询。

### PNL-06 [P2] “本次峰值”和曲线实际只有短时滚动窗口

位置：[dashboard.py:817](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:817>)、[dashboard.py:2236](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:2236>)。

history 超过 150 点便丢弃旧点，页面 `SESSION PEAK GAIN` 对剩余点取最大。快照约每 2 秒产生时窗口约 5 分钟；采集延迟或无页面请求会改变实际时间长度。它不是本次完整历史或本次峰值。

**复现：**会话早期盈利达到 9，随后为 1，滚动到 150 点后显示峰值 1，真实本次峰值仍为 9。

**修改建议：**完整会话高水位和统计独立持久化；图表可降采样保留全程，或准确标明“最近 150 个采样点”。不得用图表缓冲区重新计算会话峰值和最大回撤。

### PNL-07 [P2] 曲线横轴忽略时间，初始化还补造未观测的零点

位置：[dashboard.py:771](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:771>)、[dashboard.py:2266](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:2266>)。

后端初始化创建 `updated_at-60` 和 `updated_at` 两个 PnL=0 点，随后可能在同一 updated_at 再添加非零点；前端横坐标按数组索引等分，不用 t。停顿 10 分钟与相隔 2 秒视觉间距一样，同一时刻的两个点也被画成不同时间跨度。

**修改建议：**以真实时间计算 x，去重 `(session_id, snapshot_id)`，禁止旧时间回流；零基点只可使用后端确知的会话开始基线，不伪造一分钟前的余额和 PnL。缺失期间应断线或明确标记，而不是平滑伪装成连续采样。

### PNL-08 [P2] 资产曲线的悬浮定位仍使用 PnL 值

位置：[dashboard.py:2340](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:2340>)。

assets 模式绘制的是 assets 曲线，悬浮点却始终计算 `getY(item.bot_pnl, minVal, maxVal)`。例如资产在 500 附近、PnL=1 时，点会落在资产纵轴范围外，不跟随所指曲线。dual 模式还使用各自缩放，但未提供清楚的双轴刻度，容易误读变化幅度。

**修改建议：**悬浮点和纵轴按当前模式绑定字段；双轨显示两套带币种的轴，或统一成同一基准的变化率，并明确公式。此项由 JS 数据路径静态确认，未另做本轮浏览器悬浮回归。

### PNL-09 [P2] PnL 经文本两位小数截断后再显示四位，无法做可靠对账

位置：[strategy_v2_base.py:584](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy/strategy_v2_base.py:584>)；[dashboard.py:386](</Users/Files/Vibe Coding/quant_okx_trader/dashboard/dashboard.py:386>)。

策略先格式化 realized/unrealized 为两位小数，Dashboard 用正则解析为 float，再显示四位。每个控制器先舍入后相加也会产生累计误差；看似四位精度实为两位精度，不足以解释小金额手续费差异。

**修改建议：**结构化快照传递 Decimal 字符串和原始币种；计算时保持精度，只在展示边界舍入。make status 与 Dashboard 共用同一规范化值，允许各自显示精度，但不能分别从展示文本重算。

### PNL-10 [P2] 策略 UNREALIZED 不能直接当作 OKX 的仓位浮盈

位置：[position_executor.py:169](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:169>)、[position_executor.py:255](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/position_executor/position_executor.py:255>)；[executor_orchestrator.py:170](</Users/Files/Vibe Coding/quant_okx_trader/hummingbot/strategy_v2/executors/executor_orchestrator.py:170>)。

活动执行器按可平仓方向买卖盘估值并扣交易费，PositionHold 按中间价估值，OKX 返回的 upl 是交易所自己的仓位口径；当前性能汇总也没有归入资金费事件。三者天然可能不同。账户权益又会受资金费、手工交易、转账等影响，因此不能把策略 PnL 宣称为账户全量真实损益。

**修改建议：**直接展示 OKX 返回的仓位浮盈，并标注时间；策略估算净 PnL 独立展示估值方法与费用覆盖范围。若要提供账户期间净损益，应按交易所账单/成交/资金费构建去重账本，不以 USDT 余额差或策略执行器总和代替。

### 重启清零的建议状态机

1. **停止交易：**保留账户快照、结束中的策略会话与历史；不得清账户资产或持仓浮盈。最终退出成交归入原会话，待对账完成后封账。
2. **发出 restart/start 请求：**仅标记“启动中”，不删除任何基线。请求被拒绝或启动失败时，会话不变。对应前文问题 15。
3. **新策略实际建立：**由后端生成持久化 UUID 会话标识，建立账本边界；初始化账户和归属状态未完成时不声称会话统计已就绪。
4. **完成初始对账：**将基线、账本游标和会话状态一次持久化；本次经济收益从确定边界计算，账户当前浮盈仍显示真实值。
5. **仅重启面板：**只恢复和读取状态，不建立新会话、不改变基线、不主动清零。
6. **断线或数据缺失：**保留成功基线与最后有效值，显示 stale/unknown；恢复后按事件 ID 补账，不用恢复时的值重新归零。
7. **用户主动重置统计：**如提供此功能，创建新的“统计区间”并保留旧区间；应与重启交易分开，不能删除交易账本。

### 本轮验证与建议新增回归用例

可复现脚本：[pnl-review-repro.py](/Users/bastnana/.codex/visualizations/2026/09/28/01a0e5cd-eb4d-7541-b0ff-23482cc77bdf/review/pnl-review-repro.py)。它从生产文件加载方法，所有写入重定向到临时目录，未连接交易所。

已得到的结果：首次显示时遗漏已发生收益；携带旧浮盈重启后本次收益为 6；历史成交额导致收益率 0.02%；实际峰值 9 显示成 1；控制器缺失令总计由 5 变 2；缺失快照生成零资产采样点；PositionHold 净值 0.8 与性能报表 1 不一致。

回归用例应覆盖：延迟打开面板、面板自身重启、跨会话仓位原价/涨跌平仓、部分减仓、手续费及资金费、重启请求被拒绝、停止末尾成交、查询失败后恢复、控制器移除但保留既有收益、超过 150 点的峰值、同时间戳去重、多币种权益和手工调整去重。完整测试通过之前，不能声明本次收益清零或与 OKX 账户对账已正确。
