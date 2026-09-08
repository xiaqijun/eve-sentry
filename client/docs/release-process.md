# 客户端 CI/CD 与发布流程

本文说明单体仓库 `client/` 目录的测试、构建和发布流程。服务端、ESI Gateway 和机器人
位于同一仓库的其他目录，跨组件接口以根目录服务端 API 文档为准。

## 工作流概览

客户端发布相关的根目录 GitHub Actions 工作流有三个：

- `Client CI`：验证客户端代码、打包配置、资源、脚本和测试。
- `Contract Compatibility`：并行验证服务端协议、client-server 集成和机器人消费者；它是
  协议变更门禁，但当前不是 `Release Client` 的 `workflow_run` 前置。
- `Release Client`：自动路径复用成功的主线 Client CI；手动路径在 job 内重新运行客户端测试，
  然后构建、签名并发布 Windows 客户端。

`Release Client` 的 job guard 与 `client-release` Environment 自定义分支策略都只允许
`main`。当前没有 required reviewer 人工审批；发布后仍必须完成下文验证，失败时按统一
回滚说明处理。

## Client CI

以下情况会触发根目录 `.github/workflows/ci-client.yml`：

- 客户端相关路径 push 到 `main`；
- 目标为 `main` 的 pull request（如平台触发）修改了客户端相关路径；
- 在 GitHub Actions 中手动运行。

工作流使用 `windows-latest`、Python 3.13 和 `requirements-onnx.txt`，并缓存 pip 下载。
同一分支或 pull request 出现更新提交时，旧的未完成 CI 会被取消，避免浪费 runner。

测试命令为：

```powershell
python -m pytest -q --ignore=tests/test_intel_client.py
python -m pytest -q tests/test_intel_client.py
```

两条命令必须使用两个独立 pytest 进程。第二个进程临时组合 `client/app` 与根目录服务端
`app.server`，用于真实 client-server 集成；与普通客户端测试混在同一解释器会污染同名
`app` 包命名空间。

## 自动发布门禁

根目录 `.github/workflows/release-client.yml` 监听 `Client CI` 完成事件。自动发布必须同时满足：

1. CI 结论为成功；
2. CI 由 push 事件触发；
3. CI 的源分支为 `main`；
4. CI 的源仓库就是当前仓库，而不是 fork；
5. 发布目标提交位于 `refs/heads/main`，且 `client-release` Environment 只允许 `main`。

这些条件确保 pull request、fork 或名称碰巧为 `main` 的外部分支不能获得发布写权限和签名
密钥。自动发布使用已经通过 CI 的同一个完整 commit SHA，不会改为构建当时最新的其他提交。

## 手动发布与标签产物

发布工作流还支持从 `main` 手动运行，可选填写期望版本进行安全校验。非 `main` 手动运行
不会进入发布 job；push 任意 `v*` 标签不再触发发布。

自动发布已经复用主线 CI 结果，因此不会重复运行测试。手动发布没有可复用的
`workflow_run` CI 结果，会在构建前重新安装 pytest，并用上述两个独立进程运行测试；标签
只作为发布产物。

发布脚本成功创建 Release 时才创建与 `app.version.APP_VERSION` 一致的标签，例如版本
`1.2.3` 产生 `v1.2.3`。标签是发布结果，不是发布入口。

## 发布阶段

发布分为快速检查和受保护发布两个 job：

1. `check-release` 在 Ubuntu runner 上以只读权限解析版本和目标 SHA。
2. 如果对应 GitHub Release 已存在，工作流成功结束，不覆盖任何资产。
3. 如果版本未发布，`release` 进入 `client-release` 环境，并仅为该 job 授予
   `contents: write`。
4. Windows runner 只从 `xiaqijun/eve-sentry` 最新既有 Release 恢复 OCR 模型，然后构建
   监控客户端和频道客户端；没有有效的单体仓库模型资产时立即失败，不访问任何仓库外的
   客户端发布源。
5. `scripts/publish_client_release.ps1` 生成程序包、模型包、SHA-256、签名更新清单和源码元数据。
6. 发布脚本再次检查同名 Release，随后创建标签和 GitHub Release；脚本不使用
   `--clobber`，拒绝覆盖已有发布。

发布需要仓库 Actions Secret `EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`。GitHub Release
使用工作流自带的 `github.token`，不需要单独的 Release token。

## 标准版本发布步骤

1. 从最新 `main` 开始开发，完成本地检查后将版本变更合并或提交到 `main`。
2. 修改仓库根路径 `client/app/version.py` 中的 `APP_VERSION`，同时提交版本相关变更。
3. 等待 `main` 的 `Client CI` 通过。
4. 协议相关变更还要确认 `Contract Compatibility` 通过；自动 Release 当前只由 Client CI
   触发，因此不能把另一个 workflow 的失败当作已被自动阻止。
5. 发布 job 进入只允许 `main` 的 `client-release` Environment。
6. 验证 GitHub Release、固定下载入口、签名清单和客户端更新检查。

不要为同一个版本反复创建或覆盖 Release。若同版本已经存在，重复运行会作为成功的 no-op
结束；若发布失败且 Release 尚未创建，可以在修复后重新运行。删除发布、回滚或重新签名属于
生产操作，必须按受保护环境和本文的发布流程执行。

## 发布验证与回滚

发布完成后至少确认 Release target、远端标签、`eve-sentry-client-source.json` 中的
`source_commit` / `release_workflow_commit` 指向同一个完整 SHA；程序包和模型包的大小、
SHA-256 与已签名 `latest.json` 一致，并用 `client/resources/update_public_key.pem` 验证
Ed25519 签名。随后检查下载站 `/health`、`/latest.json`、`/download/latest` 302 跳转和
程序/模型 Range 206，再在验收客户端安装并记录实际版本、心跳和星图状态。

客户端更新器在新程序启动健康检查失败时会恢复更新前备份。同版本 Release 不允许覆盖；
若发布内容本身有误，应停止推广并发布递增版本，不要删除后重建同一标签。下载站和其他
生产组件的验证、恢复边界见[统一 CI/CD 回滚说明](../../docs/ci-cd.md#验证与回滚)，下载入口
细节见[下载站开发与部署](../../docs/download-site.md)。

## 本地校验

从仓库根目录提交工作流修改前至少运行：

```powershell
actionlint .github/workflows/ci-client.yml .github/workflows/ci-contracts.yml .github/workflows/release-client.yml
.\.venv\Scripts\python -m pytest -q tests/test_workflow_safety.py client/tests/test_release_workflows.py
Push-Location client
python -m pytest -q --ignore=tests/test_intel_client.py
python -m pytest -q tests/test_intel_client.py
Pop-Location
```

提交前还应运行 GitNexus `detect_changes()`，确认修改只影响预期的文档、测试和发布流程。
