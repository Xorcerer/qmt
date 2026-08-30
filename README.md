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
- `server_fundamental_utils.py`：复权因子、批量标的、换手率、股本、交易日历、板块成分
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
  - `server_fundamental_utils.py`
  - `server_runtime_utils.py`
  - `server_socket_utils.py`

## 配置
1. 复制 `server_config.json.example` 为 `server_config.json`
2. 按本机账户与订阅需求填写

示例字段：
- `account_id`：账户号；如果 `ContextInfo` 能自动识别，可留空
- `account_type`：默认 `STOCK`
- `auth_token`：访问令牌，**必填**。为空时服务器拒绝所有请求（fail-closed），不要留空来“关闭鉴权”
- `quote_symbols`：启动时自动订阅的行情代码
- `quote_period`：默认 `tick`
- `quote_dividend_type`：默认 `none`

注意：
- `server_config.json` 是本地环境文件，不应提交到仓库。
- 当前代码会热加载 `server_config.json`，修改后无需重启 Python 进程即可生效。

## 鉴权
本服务能提交真实委托，因此鉴权是 **fail-closed** 的：

- `auth_token` 为空（或配置文件读取失败）：**拒绝所有请求**，返回 `401 {"error":"unauthorized"}`
- `auth_token` 非空，以下任一方式通过即可：
  - HTTP `Authorization: Bearer <token>`
  - HTTP `X-QMT-Token: <token>`
  - WebSocket 握手支持上述 Header
  - WebSocket 也支持 `Sec-WebSocket-Protocol: qmt-token.<token>`

令牌比较使用 `hmac.compare_digest`，避免时序侧信道。

调用方（algo_monitor 的 Django 后端）从 `config.yaml` 的 `qmt_auth_token` 或环境变量
`QMT_AUTH_TOKEN` 读取同一个令牌，两边必须一致。

### 其他访问控制
- **Host 白名单**：只接受 `127.0.0.1[:18080]` / `localhost[:18080]`，其他 Host 返回
  `403 {"error":"host_not_allowed"}`，用于阻断 DNS rebinding
- **不返回任何 CORS 头**（`CORS_ALLOW_ORIGIN = ''`）：唯一调用方是同机的 Django 后端，
  不需要浏览器跨域；一旦返回 `Access-Control-Allow-Origin: *`，本机浏览器打开的任意
  网页都能读账户并调用 `/order`

### 下单限额
`server.py` 顶部的常量对每一笔委托做硬性约束，超限直接拒绝并记入 `/health`：

- `MAX_ORDER_NOTIONAL`（单笔金额上限，默认 200000）
- `MAX_ORDER_VOLUME`（单笔数量上限，默认 100000）
- `MAX_ORDERS_PER_MINUTE`（每分钟报单数上限，默认 30）

建议：
- 令牌用 `python -c "import secrets;print(secrets.token_urlsafe(40))"` 生成
- 不要把真实 `server_config.json` 或令牌提交到仓库
- NAS 日更 **不直连** 本服务，只打 Django `/api/ingest/qmt/...`（Bearer `ALGO_MONITOR_API_TOKEN`）；Django 再用本段 `QMT_AUTH_TOKEN` 调 `127.0.0.1:18080`
- 在 benben 上探测本服务时用 `curl.exe`（PowerShell 的 `curl` 是 `Invoke-WebRequest` 别名，会挂死无输出）：

```powershell
curl.exe -s --max-time 10 -H "Authorization: Bearer <auth_token>" http://127.0.0.1:18080/health
```

公网 `https://benben.cafe/qmt/` 是 Django staff 代理，未登录 401，不是本端口。

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
- `GET /candles-bulk?symbols=600000.SH,000001.SZ&period=1d&start=&end=`：批量日 K（NAS ingest 经 Django 转发）
- `GET /instrument?symbol=000300.SH`：标的基本信息
- `GET /instrument-bulk?symbols=600000.SH,000001.SZ`：批量标的信息
- `GET /divid-factors?symbols=...&start=&end=`：复权因子
- `GET /turnover-rate?symbols=...&start=&end=`：换手率
- `GET /total-share?symbols=...`：总股本
- `GET /trading-dates?symbol=000001.SZ&start=&end=`：交易日历
- `GET /sector?name=沪深300`：板块 / 指数成分
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
- 远程请用 `curl.exe`，不要用 `curl`
- 401：缺 Bearer / `X-QMT-Token`，或 `auth_token` 为空（fail-closed）
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
