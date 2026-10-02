# EVE Sentry 单体仓库开发流程

当前仓库是 EVE Sentry 的唯一开发源和发布入口。原来的服务端、客户端、机器人和
ESI Gateway 已按目录导入并保留历史。功能分支和 Pull Request 可以运行验证；`main`
是唯一生产部署和客户端发布分支。

## 目录职责

| 目录 | 职责 |
| --- | --- |
| `app/`、`frontend/` | 服务端、Web Console、HTTP/SSE、认证和 PostgreSQL |
| `client/` | Windows OCR/监控客户端、星图态势和客户端更新器 |
| `bot/` | QQ 机器人命令、事件消费和 QQ 适配；生产运行时由 `app/server/qq_bot_runtime.py` 内嵌托管 |
| `esi-gateway/` | 公共 ESI 代理、鉴权、缓存、限流和健康检查 |
| `download-site/`、`deploy/cloudflare-download/` | 下载站静态页面、Cloudflare 下载 Worker 和发布校验 |
| `docs/` | 跨组件协议、架构、部署、故障和联调文档 |

`eve-sentry-contracts` 已废弃，不再作为代码或子目录导入。契约说明统一维护在
`docs/`，不能恢复一个独立 contracts 仓库来承载接口事实。

## 开发规则

1. 功能分支和 Pull Request 只用于验证；生产部署、客户端 Release 和发布标签只能来自
   `main`。
2. 修改服务端 API、SSE、事件字段、游标、认证或 ESI 行为时，必须同步更新 `docs/`
   下的接口和兼容性说明。
3. 组件内部代码保留在对应目录；跨组件调用通过服务端 API、事件和明确的 Python/JSON
   接口完成，不复制另一组件的实现。
4. 修改前先做 GitNexus 影响分析；提交前运行 `detect_changes()`，确认只影响预期目录。
5. 生产部署必须通过根目录 GitHub Actions 执行；workflow job guard 与 `production`、
   `client-release` Environment 分支策略都只允许 `main`。发布后按对应组件文档完成健康
   检查，失败时使用已记录的回滚流程。
6. 工作树中已有的用户修改必须保留，不得用重置或强制覆盖方式清理。

Seat 联调保持两端事实源分离：Sentry 保存主节点监控贡献区间、预警事件资格、修订、授权投递、客户端 ACK
和对账游标；GloryNavy_Seat 保存账号归属、奖励/价格政策、币预留、消费、释放和退款。主节点贡献通过
`GET /api/v1/integrations/seat/monitor-contributions` 只读导出，当前只支持 `sentry_monitor` 覆盖基础奖，
不导出独立事件奖。任何跨项目字段变更都要同时更新 `docs/api-reference.md` 与 Seat 侧 OpenAPI/模块文档，并明确协议版本、
幂等键、重放结果和不可恢复游标缺口。收费开关默认关闭，不能以本地服务端测试代替 exchange
生产费率或结算验收。

## 组件验证

- 服务端：在仓库根目录运行 `python -m pytest -q tests`，并在 `frontend/` 执行前端测试
  和构建。
- 客户端：在 `client/` 依次用两个独立进程运行：

  ```powershell
  python -m pytest -q --ignore=tests/test_intel_client.py
  python -m pytest -q tests/test_intel_client.py
  ```

  第二个进程临时组合客户端与服务端两个 `app` 包路径，不能并入第一个 pytest 进程。
- 机器人：在 `bot/` 运行 `uv sync --frozen --extra dev`，再运行 `uv run pytest -q`。生产预警
  由服务端内嵌运行时托管；只有手动战报分析的 worker 仍按需单独启动。
- ESI Gateway：在 `esi-gateway/` 安装 `.[test,storage]` 后运行 `pytest` 和 `ruff`。

`Contract Compatibility` 的本地等价验证为：

```powershell
python -m pytest -q tests/test_http_server.py tests/test_http_resource_limits.py
cd client
python -m pytest -q tests/test_intel_client.py
cd ..\bot
uv run pytest -q tests/test_alerts.py tests/test_sentry_status.py tests/test_sentry_watch.py
```

仅根目录 `.github/workflows/*.yml` 是有效 GitHub Actions 入口。服务端 workflow 只打包
`deploy/ci/` 与 `deploy/linux/`；下载站源码和 `deploy/cloudflare-download/` Worker 由下载站
工作流独立发布，不进入服务端归档。
