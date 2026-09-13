# 1.0.73 实时视图与重连对账发布

本记录随发布候选提交。生产部署与客户端发布结果以对应提交的 GitHub Actions、Release
和签名更新清单为准；本地测试通过不代表生产端到端时延已经验收。

## 修复内容

- Windows 预警启动、网络重连和正常 SSE 连接轮换均取当前快照及对应水位，
  整体替换实时状态，再接收水位之后的新变化；不补播停用或断线期间已结束的来敌/清空。
- 服务端保留星系与事件历史，但实时快照排除 unknown；失效 SSE 更新不携带旧人数和名单。
- 客户端列表、客户端/网页星图和机器人实时节点只展示有效监控，不展示历史“上次敌情”。
  失效数据移除不表示安全，不生成清空通知；客户端停止失效告警声音，机器人取消失效待发名单。
- 相同星系、采集会话、数量和状态的周期 Presence 确认日志去重；数量变化、重新开始及
  请求失败后的恢复仍记录。正常上报与续租频率不变。

## 兼容与边界

没有改动数据库表结构、历史保留策略、公开事件类型或机器人的可靠补发策略。
重连复用现有无游标 Bootstrap 契约及共享快照缓存，不分别请求状态和最新游标。
服务端、机器人及网页修改通过各自受保护工作流发布；客户端需安装 1.0.73 才获得本地修复。
组织模式、ESI 路线和生产凭据保持原配置，不重复执行 ESI transport rollout。

## 本地验证

- Windows 客户端：379 项通过；独立客户端/服务端 API 联调：71 项通过。
- 重连专项：7 项通过，覆盖已清空/仍有敌人、正常 EOF、网络和非预期错误、快照前失败重试、
  快照之后的新变化与非状态 ID 不覆盖水位。
- 服务端最终全量：895 项通过、10 项跳过（真实 PostgreSQL 模块在 CI 独立验证，
  未纳入用户原有未跟踪压测用例）；快照/游标专项：5 项通过。
- PostgreSQL 当前状态及实时投影专项：13 项通过；真实生产库未用作测试库。
- 机器人全量测试及 Ruff 通过；网页 25 个文件/130 项通过，TypeScript 和生产构建通过。
- 空列表与空星图离屏截图检查通过；Git diff whitespace 检查通过。

分批用例有重叠，不相加作为独立用例总数。最终发布还需检查 GitHub 的完整 PostgreSQL 门禁、
客户端 Release 源提交、清单签名和下载入口。真实游戏断线重连、远端清空、QQ 实际时延仍需运行验收。

## 发布与回滚

推送 main 后由 Client CI 自动触发 Release Client，同时核对 Contract Compatibility、
Deploy Server、Validate and Deploy Bot 和下载站工作流。不得覆盖已有同版本 Release。
客户端启动健康检查失败自动恢复旧安装；发布内容有误时停止推广并发布递增补丁版本。
服务端/机器人按受保护工作流和已有备份回滚，不删除历史表。

具体操作见[客户端发布流程](../client/docs/release-process.md)与[统一 CI/CD](ci-cd.md)。

## 首次 CI 门禁与修正

候选提交 `e36f862d3c7c65775b59a1190d699d4205ff1702` 已推送，客户端 CI、协议兼容与下载站
验证/部署通过。服务端首次 PostgreSQL 门禁 93 项通过、2 项失败，部署被正确拦截。
失败来自 `test_legacy_primary_postgres.py` 的旧断言：重启后仍要求 unknown 出现在实时快照。
修正为实时快照为空，同时验证持久历史仍为 unknown、事件未丢失、旧主来源不复活。
不删除测试、不跳过门禁、不修改业务代码；完整 PostgreSQL 门禁复跑后再触发受保护部署。

修正后本地完整 PostgreSQL 门禁 **95 项通过**。客户端 [v1.0.73](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.73)
已创建，[发布工作流](https://github.com/xiaqijun/eve-sentry/actions/runs/34762383096) 成功，
客户端源提交仍为上述 `e36f862`；补充提交只修测试和记录，不重建或覆盖客户端资产。
[机器人部署](https://github.com/xiaqijun/eve-sentry/actions/runs/34762298821) 已通过。
签名清单、下载入口和修正后的服务端部署继续验收，以最终工作流和检查输出为准。

## 最终发布结果（2026-09-13）

- [服务端第二轮部署](https://github.com/xiaqijun/eve-sentry/actions/runs/34762676341) 全部成功，
  包括完整 PostgreSQL 门禁、Windows 测试、前端测试/构建、生产切换及公网 readiness。
  SSH 只读复核 `/var/lib/eve-sentry/deployed-revision` 为
  `0402aa49a746fae6fc0ffe150bb3e06d8e8ad0e2`，`eve-sentry` 服务为 active。
- [客户端 Release](https://github.com/xiaqijun/eve-sentry/releases/tag/v1.0.73) 和标签指向
  `e36f862d3c7c65775b59a1190d699d4205ff1702`；源码元数据中的 `source_commit` 与
  `release_workflow_commit` 相同，未覆盖已有发布资产。
- [Client CI](https://github.com/xiaqijun/eve-sentry/actions/runs/34762298837)、
  [协议兼容](https://github.com/xiaqijun/eve-sentry/actions/runs/34762298815)、
  [机器人部署](https://github.com/xiaqijun/eve-sentry/actions/runs/34762298821)、
  [下载站部署](https://github.com/xiaqijun/eve-sentry/actions/runs/34762298853) 均成功。
- 发布和下载站 `latest.json` 内容一致，版本 1.0.73；使用仓库公钥验证 Ed25519 签名通过。
  GitHub 提供的程序包/模型包 SHA-256 摘要与大小均匹配签名清单；没有将本机未下载完的附件
  记作完整文件哈希复算。程序包 132,422,267 字节，模型包 105,099,126 字节。
- 下载站 `/health` 返回 ok；`/download/latest` 返回 302 指向 1.0.73；程序和模型
  Range 请求均返回 206，Content-Range 总长度与清单一致。

程序包 SHA-256：`363e66731b79197c0be87b0f5f5f50c1d9163b998c99286d611e6b9c4a804e6f`。
模型包 SHA-256：`370ff641c19876472e48309da52d60085c3a8455c71810cf10a7c654cd2b9f49`。

本次未更改全局代理或生产配置，未重新切换 ESI 路线；本地测试 PostgreSQL 已停止。
仍待用户实际安装新版并验证游戏中断线重连、远端清空和 QQ 时延，不将发布成功等同于业务时延验收。
