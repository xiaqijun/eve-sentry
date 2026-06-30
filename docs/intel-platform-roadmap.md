# EVE Sentry 情报平台开发路线

> 日期: 2026-06-30  
> 配套文档: `docs/intel-platform-architecture.md`

## 阶段 0: 当前状态

已经具备:

- 桌面检测客户端。
- EVE 窗口识别和区域截图。
- PaddleOCR 本地频道识别。
- 后台截图能力。
- 本地弹窗可关闭。
- 简易服务端和地图页面。
- 检测客户端与预警客户端初步拆分。
- `/api/intel` 和 `/api/reports` 形式的基础上报/查询。

主要不足:

- 情报仍以字符串名字为核心，缺少 `character_id`。
- 服务端还不是完整判断中心。
- 没有预警频道日志解析。
- 没有 ESI 补全。
- 没有击毁画像。
- 没有多源评分和证据链。
- JSON 存储很快会不够用。

## 阶段 1: 统一情报模型

目标:

- 引入 `Observation` 和 `ThreatEvent`。
- 让 OCR、预警频道、手工输入、击毁记录都能进入同一条服务端管线。

任务:

- 新增 `app/core/models.py`。
- 新增 `app/intel/observations.py`。
- 服务端新增 `POST /api/observations`。
- 保留 `/api/intel`，内部转换为 `Observation`。
- 新增 `GET /api/alerts`，先返回由简单规则生成的事件。

验收:

- 检测客户端上报后，服务端能保存 observation。
- 预警客户端只消费 alert，不再直接理解 report。
- 单元测试覆盖 JSON 序列化、去重、基础规则。

## 阶段 2: 预警频道解析

目标:

- 增加第二采集源，解析 EVE chatlogs 中的预警频道。

任务:

- 新增 `app/channels/log_watcher.py`。
- 新增 `app/channels/parser.py`。
- 新增 `app/channel_client.py` 或 `python -m app.channels` 入口。
- 支持配置日志目录和频道名过滤。
- 支持 UTF-16/UTF-8 探测。
- 支持断点续读。
- 支持基础文本模式:
  - `Tama +3`
  - `Tama 有红`
  - `Oijanen Some Pilot`
  - `ABC-123 reds`

验收:

- 给定样例 chatlog 文件，解析出 observation。
- 重启采集器不会重复上报历史行。
- 解析失败的行仍能作为 raw observation 保存。

## 阶段 3: ESI 公开数据补全

目标:

- 把名字字符串解析为稳定 ID。
- 补全角色、军团、联盟、星系基础信息。

任务:

- 新增 `app/esi/client.py`。
- 新增 `app/esi/resolver.py`。
- 新增 `app/esi/cache.py`。
- 实现 name -> id 批量解析。
- 实现 id -> name 批量反查。
- 实现角色公开资料缓存。
- 实现星系名/星系 ID 补全。

验收:

- 输入角色名，服务端能保存 `character_id`。
- 输入星系名，服务端能保存 `system_id`。
- 网络失败不会阻塞检测主流程。
- 相同名字重复解析命中本地缓存。

## 阶段 4: 击毁查询和行为画像

目标:

- 接入 zKillboard，生成近期威胁行为画像。

任务:

- 新增 `app/killboard/zkill_client.py`。
- 新增 `app/killboard/analyzer.py`。
- 支持按角色查询近期 kills/losses。
- 支持按军团/联盟聚合查询。
- 支持按星系查询近期击毁热度。
- 生成 `KillActivity`。
- 请求设置 `User-Agent` 和 gzip。
- 实现退避和缓存。

验收:

- 给定 `character_id`，可以得到近 24 小时/7 天击毁统计。
- 网络失败时返回旧缓存或空画像，不影响报警。
- 评分引擎能使用击毁画像作为 evidence。

## 阶段 5: 多源威胁评分

目标:

- 服务端成为最终判断中心。
- 所有报警带分数和证据链。

任务:

- 新增 `app/intel/scoring.py`。
- 新增 `app/intel/evidence.py`。
- 引入白名单、黑名单、敌对军团/联盟规则。
- 引入频道时效性规则。
- 引入击毁活跃规则。
- 引入冷却和去重。

验收:

- 单个角色可以输出完整 `ThreatEvent`。
- 白名单命中能抑制报警。
- 黑名单命中能直接高危。
- 同一角色在冷却窗口内不重复轰炸预警端。
- 预警端显示 evidence 摘要。

## 阶段 6: ESI SSO

目标:

- 支持用户登录 EVE SSO。
- 自动读取自己的位置和 standings。

任务:

- 新增 `app/esi/sso.py`。
- 桌面或本地 Web 完成 OAuth2 + PKCE。
- 保存 token 和 refresh token。
- 保存 `CharacterOwnerHash`。
- 读取当前位置。
- 读取 contacts/standings。

验收:

- 用户能完成登录授权。
- 服务端能刷新 token。
- 检测端不再必须手工配置当前星系。
- standings 能参与评分。

当前进展:

- 已提供 `app.esi.sso` 本地 PKCE 登录和 `app.esi.session` token refresh/session
  snapshot。
- 服务端已提供 `/api/esi/status` 和 `/api/esi/session`，并能把 contacts/standings
  注入角色 profile 参与 `hostile_standing` 评分。
- 待继续: 把当前位置同步接入检测/预警工作流，完善登录引导和 token 安全存储。

## 阶段 7: SQLite 和事件推送

目标:

- 替换 JSON 存储。
- 让预警端接近实时。

任务:

- 新增 SQLite store。
- 设计 schema 和迁移脚本。
- 从 `intel_reports.json` 导入旧数据。
- 增加 SSE 或 WebSocket。
- 预警客户端从轮询切换为订阅，保留轮询 fallback。

验收:

- 旧 JSON 数据可导入。
- 多个客户端同时连接稳定。
- 服务端重启后历史情报、缓存、ack 状态仍存在。

## 优先级建议

短期最高优先级:

1. 统一 `Observation` / `ThreatEvent`。
2. 预警频道解析。
3. ESI 公开名字解析。
4. 多源评分基础版。

中期:

1. zKillboard 击毁画像。
2. SQLite。
3. Web 面板证据链展示。

后期:

1. ESI SSO。
2. standings 自动同步。
3. SSE/WebSocket。
4. 多检测端部署和健康检查。

## 风险和约束

- OCR 结果有误识别，需要服务端按置信度和 ESI 解析结果降噪。
- 预警频道文本格式没有统一标准，解析器必须保留 raw text。
- ESI 和 zKillboard 都需要缓存、限速和退避。
- SSO scopes 要最小化，避免过早引入复杂授权。
- 击毁活跃不等于当前威胁，只能作为证据之一。
- JSON 存储会限制查询和去重能力，应尽早迁移 SQLite。
