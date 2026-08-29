# 客户端迁移第一阶段设计

## 目标与范围

本阶段从 `E:\github\eve-sentry` 提取 Windows 客户端运行时及其可验证的构建资产，保留原提交历史，不改变服务端代码，不删除源仓库内容，也不触发发布。

已迁移的范围：

- `app/ui/`、`app/engine/`、`app/channels/`；
- `app/detector_client.py`、`app/intel_client.py`、`app/persistent_intel_client.py`、`app/alert_client.py`；
- 客户端启动、诊断、单实例、版本与更新模块；
- `app/core` 中的共享模型、客户端身份和 heartbeat；
- `app/models/region_prefs.py`；
- `app/esi/__init__.py` 与 `app/esi/sso.py`（仅保留本地 SSO/token 兼容边界）；
- OCR/捕获/声音/更新公钥资源、PyInstaller spec、运行时 hook、客户端启动/签名/发布脚本；
- 与上述模块对应的 pytest 测试及 `docs/client.md`。

未迁移：`app/server/`、`app/intel/`、前端管理控制台、部署脚本、服务端专用依赖和服务端测试。

## 历史保留方案

环境最初没有 `git filter-repo`，且 `git filter-branch` 的 tree-filter 在 379 个提交上过慢；已在工作区运行时安装 `git-filter-repo`，仅对临时克隆执行：

```powershell
git clone --no-local E:\github\eve-sentry E:\github\eve-sentry-client\.migration-tmp5
python -m git_filter_repo --force --path <allowlisted-path> ...
git -C E:\github\eve-sentry-client fetch migration main
git -C E:\github\eve-sentry-client merge --allow-unrelated-histories migration/main
```

影响：过滤后的历史会生成新的提交哈希，并移除未列入 allowlist 的路径；源仓库不变，客户端仓库保留 379 个原始提交对应的客户端演进历史。过滤前已说明并验证该影响，未执行任何发布或远端推送。

## `app/core` / `app/esi` 契约边界

### 客户端拥有的契约

- `app.core.models`: `Observation`、`Evidence`、`ThreatEvent` 及规范化/序列化字段。该字段形状被 `IntelApiClient`、UI、上传队列和测试共同依赖，迁移阶段保持原样。
- `app.core.client_identity`: 安装级稳定身份生成与持久化；只产生客户端 ID，不访问服务端存储。
- `app.core.heartbeat`: detector/alert/channel heartbeat payload 的稳定字段和诊断摘要。
- `IntelApiClient` / `PersistentIntelApiClient`: 面向服务端 HTTP API 的传输契约；包括 report/observation/channel/OCR/presence/heartbeat、bootstrap、alerts 和 ESI 代理查询。

### 服务端拥有的契约

- `app.esi.client`、`resolver`、`cache`、`session`：服务端 ESI 解析、缓存、联系人 standing、远端会话和 SSO 编排实现。
- 客户端不直接导入这些服务端实现。客户端只保留 `app.esi.sso` 的本地 token/PKCE 类型与兼容入口，供身份日志扫描和本地登录边界使用。
- 若未来需要客户端原生 ESI 能力，应新增明确的 HTTP/JSON adapter（或独立 `client_contracts` 包），不得把服务端 resolver/store 反向带入客户端。

## 后续阶段建议

1. 在客户端仓库建立独立依赖文件和 CI 矩阵（Paddle 与 ONNX 两种 OCR 构建）。
2. 为 `IntelApiClient` 的请求/响应 payload 建立版本化 contract tests；先覆盖 heartbeat、OCR snapshot、presence、alerts、ESI gateway。
3. 将服务端 API URL、认证模式和 ESI 代理能力写入客户端配置契约，避免隐式依赖服务端模块。
4. 完成客户端专用打包流水线后，再评估移除临时 `migration` remote 和本地过滤辅助文件；本阶段不发布。

## 验证记录

- 客户端本地回归：`241 passed`（身份、heartbeat、更新器、捕获/OCR、worker、主窗口、alert、channel GUI 等）。
- `tests/test_intel_client.py` 依赖未迁移的 `app.server.http_server` 与 `app.server.intel_store`，本阶段保留测试文件但不在客户端仓库执行；后续以 HTTP contract test 替代该服务端耦合 fixture。
