# QMT 共享内存桥接设计

## 目标与约束

国金证券 QMT 内嵌 Python 3.6.8 不允许业务代码进行文件读写和网络连接，但仍允许导入外部 Python 文件。改造后：

- QMT 进程不创建 socket，不监听端口，不读取配置或日志文件。
- 外部普通 Python 进程独立提供原有 HTTP/WebSocket 服务。
- Django 的 `QMT_BASE_URL`、接口路径、Bearer 鉴权和返回结构保持不变。
- 账户、行情、交易等 QMT API 仍在 QMT 原线程执行，不额外创建工作线程。

## 进程结构

```text
Django / 其他本机客户端
          │ HTTP + WebSocket，127.0.0.1:18080，Bearer
          ▼
algo-monitor-qmt-gateway（外部 Python 3.11，NSSM 服务）
  - Host 校验、鉴权、HTTP/WebSocket
  - 读取并热加载 server_config.json
  - 请求槽位分配、超时、行情分发
          │ Global\QmtAlgoMonitorBridge（Windows 命名 mmap）
          ▼
QMT Python 3.6.8（server.py）
  - server_tick / handlebar 驱动共享内存轮询
  - 配置应用、账户/行情/交易 API
  - 返回 JSON 状态与数据，发布行情快照
```

外部网关应先启动。QMT 未启动时网关仍正常运行，HTTP 业务接口返回 `503 {"error":"qmt_unavailable"}`，处理中的请求超时返回 `503 {"error":"qmt_timeout"}`。

## 共享内存布局

使用 `mmap.mmap(-1, size, tagname=...)`，存储由 Windows 页面文件支持，不对应磁盘文件。默认总大小约 18 MiB：

| 区域 | 内容 |
|---|---|
| 全局头 | magic、协议版本、总大小、槽位配置、代次、QMT 心跳 |
| 8 个请求槽位 | 每槽 24 字节头 + 2 MiB 请求/响应缓冲区 |
| 行情通道 | 发布序号 + 长度 + 2 MiB 最新快照 |

槽位状态：

```text
FREE → CLAIMED → READY → PROCESSING → DONE → FREE
```

外部进程是唯一槽位分配者，QMT 是唯一请求消费者。请求和响应都使用 UTF-8 JSON。请求包含 `method` 和 `target`；响应包含 HTTP 状态码和业务 payload。头部最后发布，避免读取到不完整的 JSON。

- 请求 ID 防止迟到响应与后续请求混淆。
- 槽位满时调用方在自己的总超时内排队，不立即失败。
- 超时请求不会强制回收 QMT 仍可能写入的槽位；完成后的 `DONE` 槽位由后续请求安全回收。
- QMT 心跳用于快速识别未连接状态。
- 行情发布序号在每次写入时递增，读取前后校验序号与长度，避免使用半写入快照。

默认单请求或单响应上限为 2 MiB。超过响应容量时返回 `response_too_large`，不会截断 JSON。若确需扩大，必须同时调整 `shared_memory_protocol.py` 的默认值并重启网关和 QMT 策略。

## 配置与接口边界

外部网关读取 `C:\server\server_config.json`，检测修改时间变化后，通过内部 `CONFIG /__bridge/config` 请求下发配置。QMT 不再调用 `open`、`os.stat` 或读取代码时间。

外部网关负责：

- `Host` 仅允许 loopback 名称，防止 DNS rebinding。
- `auth_token` 缺失或不可读时 fail closed，业务请求返回 401。
- HTTP 序列化、404/405、长请求超时、WebSocket 握手和控制帧。

QMT 保留：

- 原有所有业务路由和返回字段。
- 下单金额、数量和频率保护。
- 账户发现、持仓/订单/成交刷新和行情订阅。
- `server_tick` 定时器；`handlebar` 也驱动桥接，避免定时器未触发时请求停滞。

共享内存是本机可信边界，不用于跨主机通信。HTTP Bearer 不替代本机进程权限控制。

## Windows 服务与 ACL

服务：`algo-monitor-qmt-gateway`，自动启动，程序为 Django venv 的 Python 3.11。

由于服务在 Session 0，而 QMT 通常在用户会话中，映射名使用 `Global\`。服务创建映射后，`external_server.py --mapping-user logan` 通过 Windows SID 设置 DACL，仅授予：

- LocalSystem；
- Administrators；
- 指定 QMT 用户 `logan`。

不使用 Everyone 写权限。若 QMT 用户变化，修改 `install_gateway.ps1` 的 `MappingUser` 后重新安装/重启服务。

常用命令：

```powershell
Get-Service algo-monitor-qmt-gateway
C:\app\nssm\nssm.exe restart algo-monitor-qmt-gateway
Get-Content C:\server\gateway_stderr.log -Tail 50
Get-Content C:\server\gateway_stdout.log -Tail 50
```

日志限制为单文件 10 MiB，由 NSSM 轮转。部署：

```powershell
scp qmt/server.py qmt/server_*.py qmt/shared_memory_protocol.py qmt/external_server.py qmt/install_gateway.ps1 logan@benben.cafe:C:/server/
ssh logan@benben.cafe "powershell -ExecutionPolicy Bypass -File C:\server\install_gateway.ps1"
```

## 验证

本地单元测试：

```bash
python -m unittest discover -s qmt -p 'test_*.py' -v
```

覆盖请求响应、24 路并发、超时槽位回收、配置和行情快照。

Windows 集成测试使用两个独立进程，覆盖真实命名 mmap、鉴权、32 路并发、路由和 WebSocket。生产服务另做了 LocalSystem 服务与 `logan` 用户进程之间的跨会话验证。测试 helper 仅放在 `C:\app\qmt_bridge_test`，不作为生产服务运行。

真实 QMT 最终验收：启动原 QMT Python 策略后，确认 `/health` 返回 `http_mode=shared_memory`、`server_version=9`、`listener_ready=true`，再检查 `/positions`、`/quotes` 和 WebSocket。不要在自动验收中调用 `/order`。

## 回滚

部署前保留了 `C:\server\server.py.pre-shm.bak` 和 `server_runtime_utils.py.pre-shm.bak`。回滚时停止 `algo-monitor-qmt-gateway`，恢复两个文件，并重新启动原 QMT Python 策略。Django 无需改动。
