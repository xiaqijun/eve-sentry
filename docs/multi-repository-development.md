# EVE Sentry 单体仓库开发流程

当前仓库是 EVE Sentry 的唯一开发源和发布入口。原来的服务端、客户端、机器人和
ESI Gateway 已按目录导入并保留历史；后续功能修改、提交和 CI/CD 均在本仓库 `main`
分支完成。

## 目录职责

| 目录 | 职责 |
| --- | --- |
| `app/`、`frontend/` | 服务端、Web Console、HTTP/SSE、认证和 PostgreSQL |
| `client/` | Windows OCR/监控客户端、星图态势和客户端更新器 |
| `bot/` | QQ 机器人、事件消费、投递队列和 QQ 适配 |
| `esi-gateway/` | 公共 ESI 代理、鉴权、缓存、限流和健康检查 |
| `docs/` | 跨组件协议、架构、部署、故障和联调文档 |

`eve-sentry-contracts` 已废弃，不再作为代码或子目录导入。契约说明统一维护在
`docs/`，不能恢复一个独立 contracts 仓库来承载接口事实。

## 开发规则

1. 所有新工作直接在 `main` 开发，不创建功能分支或 Pull Request。
2. 修改服务端 API、SSE、事件字段、游标、认证或 ESI 行为时，必须同步更新 `docs/`
   下的接口和兼容性说明。
3. 组件内部代码保留在对应目录；跨组件调用通过服务端 API、事件和明确的 Python/JSON
   接口完成，不复制另一组件的实现。
4. 修改前先做 GitNexus 影响分析；提交前运行 `detect_changes()`，确认只影响预期目录。
5. 生产部署、健康检查和回滚仍由 role `90` 负责；普通开发任务只提交和推送代码。
6. 工作树中已有的用户修改必须保留，不得用重置或强制覆盖方式清理。

## 组件验证

- 服务端：在仓库根目录运行 `pytest`，并执行前端测试和构建。
- 客户端：在 `client/` 运行 `python -m pytest -q --ignore=tests/test_intel_client.py`。
- 机器人：在 `bot/` 使用 `uv sync --frozen --extra dev && uv run pytest -q`。
- ESI Gateway：在 `esi-gateway/` 安装 `.[test,storage]` 后运行 `pytest` 和 `ruff`。

根目录 `.github/workflows/` 根据目录变更触发对应验证和部署；子目录中的旧 workflow
仅作为历史参考，不是有效发布入口。
