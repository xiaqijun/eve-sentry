# EVE Online 敌对舰队分析 QQ 机器人

面向 EVE Online 国际服 Tranquility 的军团战前情报工具。机器人接收最多 30 个角色名，按角色分别结合 CCP ESI 与 zKillboard 的公开数据，生成可解释的单人战斗画像。

## 功能

- QQ 官方机器人群聊 `@` 指令接入，不依赖 OneBot 或非官方客户端。
- 订阅 EVE Sentry 实时 SSE 状态，按星系向已启用的 QQ 群主动推送来敌与清空通知。
- 近 90 天公开战报分析，并按最近 30 个 KM、近 7 天、近 30 天、近 90 天统计小队规模。
- 舰船角色、舰队规模、单收比例、北京时间活跃热力图、共现关系与重复构成。
- 指挥视角的可解释威胁指数、近 7 天击杀/损失、舰队体系识别和关键人物画像。
- 星期×小时活跃热力图，以及使用 CCP 官方 SDE 映射的主要星域和常见星系。
- 舰队构成按“同星系且相邻不超过 20 分钟”合并为交战；单场内按角色去重，报告使用“通常数量/较大场次数量”的自然语言，不累计同场重复战报。
- 分析卡片使用威胁、作战角色和常见规模标签，不再展示“候选人员”；小队规模按 0–4、4–8、8–12、12 人以上分桶。
- 最近一场交战只展示查询角色作为攻击方出现的来犯编队；最近战报显示可观测交战规模、参与击杀或交火互损、主要目标舰船、公开战斗价值和舰队配置。
- 可通过角色、军团和联盟 ID 过滤己方蓝加/绿星成员，过滤项不进入敌方舰队配置。
- 中文摘要和 960 像素纵向 PNG 情报卡，自动加载官方角色头像与舰船图标。
- 舰船、舰船组和星系名称使用 CCP 官方 SDE 简体中文映射；玩家自定义的角色、军团和联盟名称保持原名。
- Redis 消息幂等、成员/群/全局限流，以及 240 秒抓取截止和部分结果降级。
- PostgreSQL 持久化公开 EVE 数据，不持久化 QQ 用户或群 OpenID。

## 使用方式

```text
@机器人 分析
```

来敌后只发送上面的两个字，机器人会读取当前预警中 ESI 已确认的敌对人员，并按人员分别生成报告。也可以手工指定角色：

```text
@机器人 分析
Character One
Character Two
Character Three
```

角色名也可用英文/中文逗号或分号分隔；一条消息包含多个角色时，会为每个角色分别排队并发送独立报告，不会合并成小队报告。只有用户主动 `@机器人 分析` 才会进入分析队列，普通消息和主动预警不会触发图片分析。当前没有 ESI 已确认敌对人员时，裸 `分析` 不会创建任务。使用 `@机器人 帮助` 查看帮助。

群内预警管理：

```text
@机器人 开启预警
@机器人 预警状态
@机器人 关闭预警
```

QQ 群聊可配置指令面板，让用户直接选择常用指令。面板配置由一次性命令幂等维护，
不会在 bot 启动时修改 QQ 平台：

```bash
docker compose run --rm bot python -m eve_risk.qq_panel
```

该命令使用生产 `.env` 中的 `QQ_APP_ID` 和 `QQ_APP_SECRET`，将全局群聊面板设置为
`分析`、`查询预警`、`帮助`、`开启预警`、`关闭预警`、`预警状态`。QQ 面板按钮只会把命令
填入输入框，用户仍需发送；分析仍按当前 ESI 已确认人员逐人生成报告。

查询当前在线监控节点及敌对详情：

```text
@机器人 查询预警
```

也可使用 `预警详情` 或 `敌对详情`。结果只统计当前 active 且已被服务端判定为
敌对的 OCR 人员，安全节点会显示为 `敌 0`。

安全节点示例：

```text
预警节点｜在线 1｜敌对 0 人
🟢 S-KSWL｜敌 0｜监控节点 1
```

在线节点使用匿名编号，不公开本地角色名。存在敌对时，每个红色节点会继续列出来敌角色名、威胁等级、首次发现时间、军团和联盟。
查询使用单次 EVE Sentry bootstrap 快照，节点人数和人员详情来自同一时刻。红色图标人数
立即进入星系告警；detector OCR 姓名只有在 ESI 返回 `resolved` 身份和 `character_id` 后
才会进入人员表。`pending`、`unresolved` 和空人员快照不会发送人员消息，避免错误 OCR
名称污染群消息；异步解析完成后会补发一次已确认名单。

机器人只从执行“开启预警”的群开始推送，不会在启动时补发历史告警。同一星系从
安全变为有敌时发送一次带人数的 `❗ 星系 来敌`；人数以 EVE Sentry 告警中的
`hostile_count` 为准，不按当前已到达的 OCR 人员条目数量推断。同一波次内人员名单、军团、联盟或
威胁信息变化时发送一次 `⚠️ 敌对事件` 明细；重复的 bootstrap 和仅时间变化不会重复推送。
最后一名敌对离开后发送 `✅ 星系 清空`，清空后再次出现会作为新波次重新发送来敌提示。
敌对事件明细使用表格展示人员、星系和 zKill 链接，人员移动时
星系列显示 `原星系 → 新星系`，zKill 只显示可点击图标。人员、军团和联盟详情也可通过“查询预警”获取。订阅群、
星系状态和告警去重游标保存在 Redis 中，不写入 PostgreSQL 或应用日志；QQ 发送
临时失败时不会提前推进星系状态，后续状态同步会继续重试。zKillboard 成功结果默认
缓存 30 分钟，同一 key 的并发请求会合并；QQ access token 会在有效期前 60 秒刷新并
通过锁合并并发刷新。机器人运行时的军团、联盟和舰船 group 资料缓存默认 6 小时、
最多 4096 条，静态 SDE 舰船类型不设置短 TTL。

预警群在敌对进入新星系、发生星系移动或人员名单变化时，只推送文字和已确认人员名单，
不会自动提交分析任务或发送分析图片。分析图片仅来自用户主动 `@机器人 分析` 的请求；
遗留的旧版主动分析任务会被 worker 拒绝并释放 admission。

监控节点上线、下线或移动到新星系时，同一批订阅群会收到完整在线节点列表；移动事件
同时使用“去向｜原星系 → 新星系”表达，不公开
本地角色名。机器人同时兼容独立的 `monitoring_node` SSE 事件和 Bootstrap 节点快照，
通过快照版本与 Redis 去重；即使实时移动事件恰逢断线，也会在重连后补发最新列表。
机器人也处理独立的 `alert` SSE 事件：已包含在 active bootstrap 的事件不会重复推送，
在下一次快照前已经清空的短暂敌对会通过该事件补发，成功投递后才推进告警游标。
SSE 默认保持长期连接；正常 EOF 立即重连，只有网络或协议异常才按 `0.2、1、3、5` 秒退避。

机器人会轮询 CCP 官方 ESI 服务器状态。连续 6 次检查不可用后才确认停服，服务器恢复时
向所有已开启主动预警的群推送一次开服通知，包括在线人数、服务器版本和启动时间。
机器人启动时若服务器已经在线只建立状态基线，不会重复群发；状态和投递去重记录保存在
Redis 中。可通过 `EVE_SERVER_STATUS_ENABLED=false` 关闭，轮询间隔和停服确认次数可分别用
`EVE_SERVER_STATUS_POLL_SECONDS`、`EVE_SERVER_OFFLINE_THRESHOLD` 调整。

## 部署

1. 在 QQ 开放平台创建机器人，启用群聊消息能力，取得 `AppID` 与 `AppSecret`。
2. 复制 `.env.example` 为 `.env`，填写 QQ 凭据和包含真实维护者联系方式的 `ZKILL_USER_AGENT`。
3. 设置一个非默认的 `POSTGRES_PASSWORD`。
   如需排除己方成员，可填写逗号分隔的 `FRIENDLY_CHARACTER_IDS`、`FRIENDLY_CORPORATION_IDS` 和 `FRIENDLY_ALLIANCE_IDS`。公共战报不包含游戏内联系人声望，因此需要显式配置这些 ID。
   如需主动预警，还应配置：

   ```dotenv
   EVE_SENTRY_EVENTS_URL=http://host.docker.internal:8765/api/v1/events
   EVE_SENTRY_API_KEY=eve_请填写管理员签发的只读服务密钥
   EVE_SENTRY_PUBLIC_URL=http://YOUR_EVE_SENTRY_HOST
   EVE_SENTRY_ALERT_MIN_LEVEL=
   EVE_SENTRY_PERSONNEL_PUSH_INTERVAL_SECONDS=10
   ```

   `EVE_SENTRY_API_KEY` 必须使用 EVE Sentry 管理员页面签发的只读服务密钥，
   仅用于 Bootstrap 和 SSE。`EVE_SENTRY_ALERT_MIN_LEVEL` 留空表示推送所有等级，
   也可设置为 `low`、`medium`、`high` 或 `critical`。
   `EVE_SENTRY_PERSONNEL_PUSH_INTERVAL_SECONDS` 控制同一星系名单更新的最小推送间隔；
   间隔内的变化会合并并在到期后推送最新完整名单，设为 `0` 可关闭合并。
4. 启动服务：

   ```bash
   docker compose up -d --build
   ```

5. 检查状态：

   ```bash
   curl http://127.0.0.1:8080/health/live
   curl http://127.0.0.1:8080/health/ready
   ```

镜像构建严格使用仓库中的 `uv.lock`。`migrate` 服务会在 bot 与 worker 启动前自动执行 Alembic 迁移。`sde-sync` 服务会从 CCP 官方 JSONL SDE 生成持久化的精简中文索引，构建号未变化时不会重复下载。QQ 群被动回复有效期为 5 分钟，因此不要将抓取截止时间配置到 240 秒以上。

向默认分支 `codex/main` 推送后，GitHub Actions 会自动执行测试、镜像构建、SSH 部署和生产健康检查。生产 `.env` 不进入部署包，Compose 项目名保持固定以复用现有数据卷。所需 Secrets、Variables、失败回滚和手动触发方式见 [机器人 CI/CD](docs/ci-cd.md)。

## 本地开发

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

核心分析和报告测试使用固定样本，不需要 QQ、ESI、zKillboard、PostgreSQL 或 Redis 的真实连接。

## 数据与合规

- 只支持 Tranquility；Serenity 不在首版范围内。
- zKillboard 数据可能缺失或延迟，报告始终显示样本量、覆盖率和置信度。
- 不推断未公开的舰船装配，不断言舰队指挥身份，不输出自动接战建议。
- 威胁指数由近期活跃、舰队规模、击杀效率、体系完整度、旗舰风险和样本稳定性六项公开指标组成，并在报告中逐项展示。
- QQ 消息正文、用户 OpenID 与群 OpenID 不写入数据库或应用日志；任务完成后 Redis 中的回复上下文自动过期。
