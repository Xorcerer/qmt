# QMT HTTP / WebSocket 服务

## 目的
- 在 QMT 策略脚本环境内启动一个本地 HTTP / WebSocket 服务。
- 对外暴露持仓、账户、委托、成交、行情、K 线、信号点和下单能力。
- 让外部程序通过 `127.0.0.1:18080` 与 QMT 交互，而不必直接运行在 QMT 的 Python 解释器里。

## 项目边界
- `qmt/` 目录内的 Python 代码只依赖标准库和同目录模块。
- 运行时依赖 QMT 提供的 `ContextInfo`、`run_time(...)`、`get_trade_detail_data(...)`、行情订阅等接口。
- `docs/` 下是 QMT 官方文档的提取与导航，供查 API 使用，不是本项目自己的部署文档。

## 目录说明
- `loader.py`：复制到 QMT 策略脚本目录，由 QMT 调用；负责热加载 `server.py`
- `server.py`：HTTP / WebSocket 服务主入口
- `server_http_utils.py`：HTTP / WebSocket 握手与帧工具
- `server_market_utils.py`：行情、K 线、龙虎榜、信号点整理
- `server_socket_utils.py`：非阻塞 socket 轮询
- `server_runtime_utils.py`：运行时状态与序列化工具
- `server_config.json.example`：示例配置
- `server_config.json`：本地真实配置，不应提交到仓库
- `docs/`：QMT 文档索引与提取内容

## 运行前提
- Windows 环境
- QMT 策略脚本模式
- `loader.py` 和 `server.py` 语法需兼容 Python 3.6.8
- QMT 已能正常调用 `init / after_init / handlebar / run_time / 回调函数`

## 依赖
- 本项目自身不依赖额外 pip 包。
- 运行依赖来自 QMT 内置环境，因此 `requirements.txt` 仅作为说明文件保留。

## 部署方式

1. QMT GUI新建策略，然后将 `loader.py` 的内容复制到策略中，保存。
2. 将 `server.py`、`server_*_utils.py`、`server_config.json` 放到 `C:\server\`。
3. 启动 QMT 策略（可以设置为随GUI启动）。

`loader.py` 会按以下顺序寻找 `server.py`：
- 环境变量 `QMT_WATCH_DIR`
- `C:\server`
- `loader.py` 所在目录

### 自定义路径
- 可通过环境变量 `QMT_WATCH_DIR` 指向 `server.py` 所在目录。
- 目录中至少需要包含：
  - `server.py`
  - `server_http_utils.py`
  - `server_market_utils.py`
  - `server_runtime_utils.py`
  - `server_socket_utils.py`

## 配置
1. 复制 `server_config.json.example` 为 `server_config.json`
2. 按本机账户与订阅需求填写

示例字段：
- `account_id`：账户号；如果 `ContextInfo` 能自动识别，可留空
- `account_type`：默认 `STOCK`
- `auth_token`：访问令牌；为空表示不启用鉴权
- `quote_symbols`：启动时自动订阅的行情代码
- `quote_period`：默认 `tick`
- `quote_dividend_type`：默认 `none`

注意：
- `server_config.json` 是本地环境文件，不应提交到仓库。
- 当前代码会热加载 `server_config.json`，修改后无需重启 Python 进程即可生效。

## 鉴权
- 如果 `auth_token` 为空：HTTP / WebSocket 不鉴权
- 如果 `auth_token` 非空：
  - HTTP 支持 `Authorization: Bearer <token>`
  - HTTP 支持 `X-QMT-Token: <token>`
  - WebSocket 握手支持上述 Header
  - WebSocket 也支持 `Sec-WebSocket-Protocol: qmt-token.<token>`

建议：
- 对外开放前务必配置强随机 `auth_token`
- 不要把真实 `server_config.json` 或令牌提交到仓库

## 运行机制
- 默认监听 `127.0.0.1:18080`
- 不启动阻塞线程，而是通过 `ContextInfo.run_time("server_tick", "10nMilliSecond", ...)` 驱动非阻塞 socket 轮询
- `handlebar` 保留为策略语义入口
- `server_tick` 专门处理 HTTP / WebSocket 轮询
- 持仓、委托、成交、行情快照都会缓存在运行时状态中

## HTTP / WebSocket 接口

### 状态与基础信息
- `GET /`：服务名、模式、公开端点列表
- `GET /health`：运行状态、配置状态、最近错误、订阅状态
- `GET /accounts`：账户信息
- `GET /positions`：持仓信息

### 行情与订阅
- `GET /quotes`：当前缓存的全部行情
- `GET /quote?symbol=000300.SH`：单个标的行情
- `GET /subscribe?symbol=000300.SH`：手动加入订阅列表
- `GET /unsubscribe?symbol=000300.SH`：手动移除订阅列表
- `GET /ws`：WebSocket 行情推送，推送类型为 `quote_snapshot`

### 交易与成交
- `GET /orders`：委托列表；支持 `symbol`、`strategy_name`、`remark`、`limit`
- `GET /deals`：成交列表；支持 `symbol`、`strategy_name`、`remark`、`limit`
- `GET /signals?symbol=000300.SH`：从成交记录推导买卖点、最低买入价、最高买入价
- `GET /order?...`：提交股票下单请求；关键参数：
  - `symbol`
  - `side=BUY|SELL`
  - `price`
  - `volume`
  - `price_type`
  - `remark`
  - `batch_id`
  - `source`

说明：
- `/order` 已实现请求入口，不再属于“计划中未实现”功能。
- 是否能成功下单仍取决于 QMT 环境、账户上下文和参数合法性。

### K 线、标的信息与期权
- `GET /candles?symbol=000300.SH&period=1d&count=240`：K 线
- `GET /instrument?symbol=000300.SH`：标的基本信息
- `GET /options?...`：期权列表与可选附加信息
- `GET /option-trade-options`：期权交易相关选项

### 其他数据
- `GET /longhubang?symbol=000300.SH&start=YYYYMMDD&end=YYYYMMDD`：龙虎榜数据
- `GET /debug/trade`：聚合调试视图，返回 health / accounts / positions / orders / deals / quotes / signals

## 常见问题

### 找不到 `server.py`
- 优先检查 `QMT_WATCH_DIR`
- 如果未设置，检查 `C:\server\server.py` 是否存在
- 再检查 `loader.py` 同目录是否有 `server.py`

### 修改配置后不生效
- `server_config.json` 依赖文件修改时间触发热加载
- 先确认写入的确是 `loader.py` 当前监听目录中的配置文件

### 外部程序连不上
- 检查 QMT 策略是否已启动
- 检查本机 `127.0.0.1:18080` 是否被监听
- 查看 `/health` 输出中的 `last_error`、`listener_ready`、`account_source`

### 订阅没有推送
- 先调用 `/subscribe`
- 检查 `/quotes` 是否已有缓存
- 检查账户持仓和 `quote_symbols` 是否为空

## 发布卫生
- 不提交真实 `server_config.json`
- 不提交日志、缓存和 `__pycache__/`
- 对外发布时至少包含：
  - `loader.py`
  - `server.py`
  - `server_*_utils.py`
  - `server_config.json.example`
  - `README.md`
  - `docs/`（可选，作为 QMT API 参考）
