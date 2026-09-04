# EVE Sentry

EVE Sentry 是一套面向 EVE Online 本地频道的敌对监控与预警系统，包含 Windows
监控客户端、Python 情报服务、React 管理页面和 QQ 机器人事件接入。

## 系统架构

```mermaid
flowchart LR
    subgraph edge["EVE 客户端侧"]
        eve["选中的 EVE 窗口"]
        logs["EVE Chatlogs"]
        monitor["Windows 监控客户端<br/>截图 · 红框检测 · ONNX OCR"]
        overlay["实时预警浮窗"]
        eve --> monitor
        logs --> monitor
    end

    subgraph service["情报服务"]
        api["认证 API · OCR 上报 · 心跳"]
        intel["角色解析 · 敌我分类<br/>实时态 · 历史告警"]
        stream["SSE 事件流"]
        database[("PostgreSQL")]
        api --> intel
        intel <--> database
        intel --> stream
    end

    subgraph consumers["展示与通知"]
        web["React 管理系统<br/>态势图 · 来袭报表"]
        bot["QQ 机器人"]
        clients["其他预警客户端"]
    end

    esi["EVE ESI"] --> intel
    sde["EVE SDE"] --> intel
    monitor -->|"可选设备密钥 · 快照"| api
    stream --> overlay
    stream --> web
    stream --> bot
    stream --> clients
```

## 预警链路

```mermaid
sequenceDiagram
    participant EVE as EVE 成员列表
    participant Client as 监控客户端
    participant Server as 情报服务
    participant Notice as 浮窗 / Web / QQ

    EVE->>Client: 当前成员列表画面
    Client->>Client: 检测红色敌对图标并立即本地预警
    Client->>Server: 上报星系、敌对姓名和人数快照
    Server->>Server: 解析角色并确认敌我关系
    Server-->>Notice: 来敌事件 + 当前敌对人数
    loop 持续监控
        Client->>Server: 刷新当前名单快照
        Server->>Server: 计算进入、仍在和离开状态
    end
    Server-->>Notice: 敌对全部离开后发布星系安全事件
```

## 身份与访问

```mermaid
flowchart LR
    member["普通用户"] --> sso["EVE SSO"]
    sso --> corp{"允许军团？"}
    corp -->|"是"| session["网页登录会话"]

    admin["管理员"] --> password["密码登录"]
    password --> session

    session --> key["可选：创建桌面设备密钥"]
    client["桌面客户端"] --> configured{"已填写设备密钥？"}
    configured -->|"否"| anonymous["跳过认证预检与 Listener 身份扫描<br/>不发送 Authorization"]
    configured -->|"是"| keycheck["验证设备密钥"]
    key --> configured
    keycheck --> access["开启经过认证的客户端访问"]
    keycheck -.-> listenerEnabled{"已开启 Listener 身份扫描？"}
    listenerEnabled -->|"是"| listener["客户端读取最近修改的 Chatlogs Listener"]
    listener -->|"发现角色"| check["服务端身份风控"]
    check --> rule{"允许军团或角色白名单？"}
    rule -->|"是"| access
    rule -->|"否"| revoke["禁用用户并吊销会话和密钥"]
    anonymous -.-> policy["enforce 模式仍会拒绝未认证的受保护请求"]
```

## 当前能力

- 监控客户端只采集用户选中的 EVE 窗口，识别成员列表中的红色敌对图标与角色名。
- 客户端使用 ONNX Runtime + DirectML 运行 PP-OCRv6 检测和识别模型。
- 当前星系从所选角色的 EVE 本地聊天日志读取，不需要周期查询 ESI 位置接口。
- OCR 快照和客户端心跳写入服务端，服务端负责角色解析、敌我分类、实时态和历史告警。
- 客户端可同时开启预警浮窗，通过 SSE 接收所有在线节点和敌对人数变化。
- Web 管理系统提供星图态势、实时处置工作台、历史来袭分析、设备密钥、用户、身份规则和审计日志。
- 已验证敌对角色可补充 zKillboard 危险度和战斗统计；外部统计只用于研判，不参与敌我分类和告警生成。
- Web 管理系统直接使用 `@arco-design/web-react` 统一标准业务控件，并支持全局明暗主题
  切换；个人账号、系统管理、星图 Canvas、ECharts 和 Arco 组件会同步更新，选择会保存
  在浏览器中。项目未使用 Arco Design Pro 脚手架，星图和图表保留专用实现。
- 管理员使用密码登录，普通用户使用 EVE SSO；桌面客户端可选使用设备密钥。密钥留空时
  不进行认证预检、Listener 身份扫描，也不发送 `Authorization`；服务端 `enforce` 模式
  仍会拒绝未认证的受保护请求。Listener 身份扫描由客户端独立开关控制，默认关闭。

## 本地启动

Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-onnx.txt
$env:EVE_SENTRY_OCR_BACKEND = "onnx"
$env:EVE_SENTRY_OCR_DEVICE = "dml"
python -m app.detector_client
```

## 客户端下载与更新

Windows 用户直接下载并解压完整便携包即可使用。连接服务端、选择 EVE 窗口、框选识别
区域以及开启监控和预警的步骤见[客户端仓库操作指南](https://github.com/xiaqijun/eve-sentry-client/blob/main/docs/client.md)。

固定下载入口：[下载最新版 Windows 客户端](https://evesentrydownload.kisectool.com/download/latest)。
该地址不包含版本号，会自动跳转到最新完整客户端包，并支持 HTTP Range 断点续传。

`main` 分支由 GitHub Actions 自动测试并部署服务端；修改 `app/version.py` 的版本号会
额外触发 Windows 客户端构建，并发布到 GitHub Release 与 Cloudflare 下载站。
客户端从签名清单中的下载站主地址下载程序和模型，支持断点续传与 SHA-256 校验。
GitCode 镜像当前已暂停，详情见 [GitCode 镜像状态](docs/gitcode-release-mirror.md)。

ONNX 模型应位于：

```text
.runtime/onnx-models/PP-OCRv6_medium_det/model.onnx
.runtime/onnx-models/PP-OCRv6_medium_rec/model.onnx
```

本地服务端使用 PostgreSQL；启动时仅加载有界近期报告与活跃情报引用：

```powershell
python -m pip install -r requirements-server.txt
python -m app.server --host 127.0.0.1 --port 8765 --postgres-dsn postgresql://eve_sentry:password@127.0.0.1:5432/eve_sentry
```

前端开发：

```powershell
cd frontend
npm ci
npm run dev
```

## 测试

```powershell
pytest
cd frontend
npm test
npm run build
```

## 文档

- [客户端操作指南](https://github.com/xiaqijun/eve-sentry-client/blob/main/docs/client.md)
- [系统架构](docs/architecture.md)
- [服务端部署](docs/server-deployment.md)
- [认证与 EVE 身份校验](docs/authentication.md)
- [Web 管理系统](docs/web-console.md)
- [完整 API 参考](docs/api-reference.md)
- [预警消息 API 接入指南](docs/alert-api.md)
- [SSE 性能与重连约束](docs/sse-performance-guardrails.md)
- [多仓库开发与联动](docs/multi-repository-development.md)
- [GitCode 镜像状态](docs/gitcode-release-mirror.md)

完整文档索引见 [docs/README.md](docs/README.md)。

运行时数据库、配置、EVE SSO token、本地密钥状态和模型缓存均不应提交到仓库。
