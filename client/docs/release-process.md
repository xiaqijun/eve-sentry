# 客户端 CI/CD 与发布流程

本文说明单体仓库 `client/` 目录的测试、构建和发布流程。服务端、ESI Gateway 和机器人
位于同一仓库的其他目录，跨组件接口以根目录服务端 API 文档为准。

## 工作流概览

仓库包含两个 GitHub Actions 工作流：

- `Client CI`：验证客户端代码、打包配置、资源、脚本和测试。
- `Release Client`：在 CI 门禁通过后构建、签名并发布 Windows 客户端。

生产部署审批、发布后的健康验证和回滚由 role `90` 负责。普通开发任务不应手动绕过
`client-release` 环境保护。

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
```

`tests/test_intel_client.py` 仍引用已迁移出本仓库的 `app.server`，因此暂不属于客户端 CI。

## 自动发布门禁

根目录 `.github/workflows/release-client.yml` 监听 `Client CI` 完成事件。自动发布必须同时满足：

1. CI 结论为成功；
2. CI 由 push 事件触发；
3. CI 的源分支为 `main`；
4. CI 的源仓库就是当前仓库，而不是 fork。

这些条件确保 pull request、fork 或名称碰巧为 `main` 的外部分支不能获得发布写权限和签名
密钥。自动发布使用已经通过 CI 的同一个完整 commit SHA，不会改为构建当时最新的其他提交。

## 标签和手动发布

发布工作流也支持：

- push `v*` 标签；
- 从 `main` 手动运行，可选填写期望版本进行安全校验。

自动发布已经复用主线 CI 结果，因此不会重复运行测试。标签和手动发布没有对应的主线 CI
门禁，会在构建前重新安装 pytest 并运行客户端测试。

标签名必须与 `app.version.APP_VERSION` 一致，例如版本 `1.2.3` 对应标签 `v1.2.3`。

## 发布阶段

发布分为快速检查和受保护发布两个 job：

1. `check-release` 在 Ubuntu runner 上以只读权限解析版本和目标 SHA。
2. 如果对应 GitHub Release 已存在，工作流成功结束，不覆盖任何资产。
3. 如果版本未发布，`release` 进入 `client-release` 环境，并仅为该 job 授予
   `contents: write`。
4. Windows runner 恢复已发布的 OCR 模型缓存，构建监控客户端和频道客户端。
5. `scripts/publish_client_release.ps1` 生成程序包、模型包、SHA-256、签名更新清单和源码元数据。
6. 发布脚本再次检查同名 Release，随后创建标签和 GitHub Release；脚本不使用
   `--clobber`，拒绝覆盖已有发布。

发布需要仓库环境中配置 `EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`。GitHub Release 使用
工作流自带的 `github.token`，不需要单独的 Release token。

## 标准版本发布步骤

1. 从最新 `main` 开始开发，完成本地检查后直接提交到 `main`。
2. 修改 `app/version.py` 中的 `APP_VERSION`，同时提交版本相关变更。
3. 等待 `main` 的 `Client CI` 通过。
4. 由 role `90` 审批 `client-release` 环境中的生产发布。
5. 验证 GitHub Release、固定下载入口、签名清单和客户端更新检查。

不要为同一个版本反复创建或覆盖 Release。若同版本已经存在，重复运行会作为成功的 no-op
结束；若发布失败且 Release 尚未创建，可以在修复后重新运行。删除发布、回滚或重新签名属于
生产操作，应由 role `90` 执行。

## 本地校验

提交工作流修改前至少运行：

```powershell
actionlint ../../.github/workflows/ci-client.yml ../../.github/workflows/release-client.yml
..\.venv\Scripts\python -m pytest -q client/tests/test_release_workflows.py
```

提交前还应运行 GitNexus `detect_changes()`，确认修改只影响预期的文档、测试和发布流程。
