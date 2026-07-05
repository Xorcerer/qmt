# QMT 文档导航

## 文档分工
- [xtdata.md](xtdata.md#L1-L199)：行情数据模块，适合查订阅、下载、缓存、K 线与 tick 数据
- [xttrader.md](xttrader.md#L1-L259)：XtQuant Trader 交易接口，适合查连接、回调注册、订阅账号、查资产/持仓/委托
- [迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L1-L4547)：QMT 策略脚本模式主文档，适合查 `ContextInfo`、`run_time`、回调、`get_trade_detail_data`

## 当前项目最常用
- `ContextInfo` 与 `set_account`：[Python_API_说明文档_Python3.md:L531-L568](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L531-L568)
- `run_time(funcName, period, startTime, market)`：[Python_API_说明文档_Python3.md:L971-L997](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L971-L997)
- `get_trade_detail_data(accountID, strAccountType, strDatatype)`：[Python_API_说明文档_Python3.md:L2987-L3023](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L2987-L3023)
- `account_callback / order_callback / deal_callback / position_callback`：[Python_API_说明文档_Python3.md:L4399-L4529](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L4399-L4529)

## 按主题查阅

### QMT 脚本模式
- `ContextInfo` 总入口与生命周期：[Python_API_说明文档_Python3.md:L531-L604](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L531-L604)
- 定时器与 `stop`：[Python_API_说明文档_Python3.md:L971-L1017](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L971-L1017)
- 行情订阅 `subscribe_quote / unsubscribe_quote`：[Python_API_说明文档_Python3.md:L2197-L2259](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L2197-L2259)

### 持仓与账户
- 交易明细查询 `POSITION / ORDER / DEAL / ACCOUNT / TASK`：[Python_API_说明文档_Python3.md:L2987-L3015](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L2987-L3015)
- 持仓与账户示例：[Python_API_说明文档_Python3.md:L3016-L3073](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L3016-L3073)
- 实时主推回调：[Python_API_说明文档_Python3.md:L4399-L4529](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L4399-L4529)

### 行情接口
- xtdata 运行逻辑、接口分类、订阅/下载分工：[xtdata.md:L57-L78](xtdata.md#L57-L78)
- xtdata `period`、时间范围、复权参数：[xtdata.md:L79-L125](xtdata.md#L79-L125)
- xtdata `subscribe_quote / subscribe_whole_quote / unsubscribe_quote`：[xtdata.md:L127-L177](xtdata.md#L127-L177)

### XtQuant Trader
- 交易接口快速示例：连接、订阅、查资产、查持仓、阻塞运行：[xttrader.md:L153-L259](xttrader.md#L153-L259)
- 回调注册、启动、连接、停止、`run_forever`：[xttrader.md:L603-L689](xttrader.md#L603-L689)
- 账号订阅与取消订阅：[xttrader.md:L711-L767](xttrader.md#L711-L767)

## 读法建议
- 如果是当前 `qmt/server.py` 这类策略内脚本开发，优先看 QMT Python API 主文档，再参考 xtdata
- 如果是独立 Python 程序连 MiniQMT 做交易，优先看 xttrader
- 如果要查实时行情订阅、缓存和历史下载，优先看 xtdata

## 快速问题入口
- “如何让 QMT 定时调用一个函数？”：看 [run_time](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L971-L997)
- “如何订阅交易主推？”：看 [set_account](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L543-L568) 和 [实时回调](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L4399-L4529)
- “如何查持仓和账户？”：看 [get_trade_detail_data](迅投QMT极速策略交易系统_模型资料_Python_API_说明文档_Python3.md#L2987-L3023)
- “如何订阅行情？”：看 [xtdata 订阅接口](xtdata.md#L127-L177)
- “如何从独立 Python 连接交易端？”：看 [xttrader 快速示例](xttrader.md#L153-L259)

## 备注
- 这些 Markdown 来自 PDF 文本提取，适合检索与快速定位
- 个别代码示例和标题可能因 PDF 转换而换行不整齐，遇到上下文不完整时优先看相邻页
