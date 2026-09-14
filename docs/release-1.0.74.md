# 名单缺失接管服务端补丁（1.0.74 客户端发布已取消）

2026-09-14 按用户要求仅部署服务端，取消客户端 1.0.74 发布。
[客户端发布流程](https://github.com/xiaqijun/eve-sentry/actions/runs/34825374136) 已取消，
未创建 v1.0.74 Release 或标签；仓库客户端版本号恢复为 1.0.73，防止后续 CI 自动补发。
客户端重试代码保留在仓库但尚未发布；文件名保留用于记录这次取消的发布候选。

## 改动

- 主监控持续有敌对图标，却没有当前画面的非空常规 OCR 名单时，服务端记录缺失等待。
  满足 15 秒及 3 个独立采集帧后，允许同星系有新鲜、完整、非空名单的备用来源接管。
- 备用的采集与名单接收时间均须在 45 秒内，当前指纹匹配且图标计数大于零。
  无合格备用保留图标预警，不推断清空；旧主重新排队，恢复后不抢回，不拼接节点名单。
- 尚未发布的客户端改动：常规 OCR 为空也会上报采集证据；保留快速重试，画面不变时每 5 秒继续重试，
  提示检查截图是否包含完整姓名列。识别成功、敌对归零后停止空名单定时重试。
- 已有名单且画面未变不要求重复上传；关闭 OCR 时不强制识别；手动查询不作为接管依据。
  仅 ESI 解析慢不会触发缺失名单接管。

## 兼容与发布边界

本次仅将服务端部署到 114，线上客户端保持 1.0.73，不要求升级。
旧客户端仍可通过有效 Presence 帧触发服务端接管，但不会获得客户端重试修复。
沿用现有 capture、OCR、alert.updated 和 Bootstrap 契约，没有新增数据库表或事件类型。
不修改心跳、代理、ESI 路线、组织开关、机器人开服通知或生产凭据。
截图仅框到图标列仍须用户重新框选，本补丁不能凭空恢复未截取的姓名。

## 本地验证

- 服务端全量：909 项通过、11 项跳过；未纳入用户原有未跟踪压测用例。
  一次 Windows HTTP 连接中止后，单项及整套重跑通过。
- 客户端：380 项通过；独立客户端/服务端 API 联调：71 项通过。
- 真实 PostgreSQL 接管、持久化、重启恢复及提交失败修复等专项：35 项通过。
  新增接管测试已纳入 Deploy Server 的 PostgreSQL 发布门禁。
- 机器人相关契约回归、改动文件 Ruff 和 Git whitespace 检查通过。
- GitNexus MCP/本地索引不可用，影响分析调用未成功；使用源码调用路径及 Git 差异复核。

分批用例有重叠，不累加作为独立用例总数。测试库仅在本机运行并已停止。
生产中双节点接管、真实游戏 OCR 和 QQ 时效仍需实际运行验收。

## 发布与回滚

通过 main 的 Deploy Server 和 Contract Compatibility 验证服务端部署。
Client CI 已通过，但 Release Client 已取消，本次不发布客户端资产。
服务端沿用受保护 production 部署与 readiness 失败自动恢复备份；客户端发布递增版本，
不覆盖已有 Release，启动健康检查失败由更新器恢复旧安装。
具体流程见[服务端部署](server-deployment.md)、[客户端发布](../client/docs/release-process.md)。

## 实际服务端部署结果（2026-09-14）

- [Deploy Server](https://github.com/xiaqijun/eve-sentry/actions/runs/34825232075) 全部成功，
  包括 PostgreSQL 门禁、Windows 测试、前端验证和生产部署。
- [Contract Compatibility](https://github.com/xiaqijun/eve-sentry/actions/runs/34825232116) 成功。
- SSH 只读复核 `eve-sentry` 为 active，`/var/lib/eve-sentry/deployed-revision` 为
  `6c4b5433a0d965e15d640a0605c647dd116c52d3`；内网、公网 `/api/readyz` 均为 ok。
- GitHub 最新 Release 与下载站 `latest.json` 仍为 1.0.73；客户端无需升级。

以上为部署及健康验收，未将其视为真实敌情接管和 QQ 时效的现场验收。
