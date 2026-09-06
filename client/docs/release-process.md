# 客户端 CI/CD 与发布流程

本文说明单体仓库 `client/` 目录的测试、构建和发布流程。服务端、ESI Gateway 和机器人
位于同一仓库的其他目录，跨组件接口以根目录服务端 API 文档为准。

## 工作流概览

仓库仅保留一个 GitHub Actions 工作流：

- `Client CI`：验证客户端代码、打包配置、资源、脚本和测试。

根仓库的 `Release Client` 自动发布工作流已废弃。安装包发布由独立客户端发布流程调用
`client/scripts/publish_client_release.ps1` 完成。

生产部署审批、发布后的健康验证和回滚由 role `90` 负责。

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

## 手动发布

通过独立客户端发布流程执行以下步骤：

1. 从最新 `main` 开始开发，完成本地检查后直接提交到 `main`。
2. 修改 `app/version.py` 中的 `APP_VERSION`，同时提交版本相关变更。
3. 等待 `main` 的 `Client CI` 通过。
4. 使用 `client/scripts/publish_client_release.ps1` 构建、签名并发布客户端包。
5. 验证固定下载入口、签名清单和客户端更新检查。

发布脚本拒绝覆盖同名 Release；删除发布、回滚或重新签名属于生产操作，应由 role `90` 执行。

## 本地校验

提交工作流修改前至少运行：

```powershell
actionlint ../../.github/workflows/ci-client.yml
..\.venv\Scripts\python -m pytest -q client/tests/test_release_workflows.py
```

提交前还应运行 GitNexus `detect_changes()`，确认修改只影响预期的文档、测试和发布流程。
