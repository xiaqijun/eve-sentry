# EVE Online 敌对舰队分析 QQ 机器人

面向 EVE Online 国际服 Tranquility 的军团战前情报工具。机器人接收最多 30 个角色名，结合 CCP ESI 与 zKillboard 的公开数据，生成可解释的舰队构成、活跃时段、常见队友和关键成员画像。

## 功能

- QQ 官方机器人群聊 `@` 指令接入，不依赖 OneBot 或非官方客户端。
- 订阅 EVE Sentry 实时 SSE 告警，向已启用的 QQ 群主动推送敌对预警。
- 近 90 天公开战报分析，近 30 天数据按 2 倍权重计算。
- 舰船角色、舰队规模、单收比例、北京时间活跃热力图、共现关系与重复构成。
- 指挥视角的可解释威胁指数、近 7 天击杀/损失、舰队体系识别和关键人物画像。
- 星期×小时活跃热力图，以及使用 CCP 官方 SDE 映射的主要星域和常见星系。
- 舰队构成按“同星系且相邻不超过 20 分钟”合并为交战；单场内按角色去重，报告使用“通常数量/较大场次数量”的自然语言，不累计同场重复战报。
- 单人查询会关联共同参战的可能队友；单场偶遇不展示，跨场次按“固定队友/经常同行”分级，并参考不同活动日期和军团联盟关系。
- 最近一场交战只展示查询角色作为攻击方出现的来犯编队；显示参与击杀或交火互损、主要目标舰船、公开战斗价值和可观察舰队配置。
- 可通过角色、军团和联盟 ID 过滤己方蓝加/绿星成员，过滤项不进入敌方舰队配置。
- 中文摘要和 1440 像素 PNG 报告。
- 舰船、舰船组和星系名称使用 CCP 官方 SDE 简体中文映射；玩家自定义的角色、军团和联盟名称保持原名。
- Redis 消息幂等、成员/群/全局限流，以及 240 秒抓取截止和部分结果降级。
- PostgreSQL 持久化公开 EVE 数据，不持久化 QQ 用户或群 OpenID。

## 使用方式

```text
@机器人 分析
Character One
Character Two
Character Three
```

角色名也可用英文/中文逗号或分号分隔。使用 `@机器人 帮助` 查看帮助。

群内预警管理：

```text
@机器人 开启预警
@机器人 预警状态
@机器人 关闭预警
```

机器人只从执行“开启预警”的群开始推送，不会在启动时补发历史告警。订阅群和告警去重游标保存在 Redis 中，不写入 PostgreSQL 或应用日志。

## 部署

1. 在 QQ 开放平台创建机器人，启用群聊消息能力，取得 `AppID` 与 `AppSecret`。
2. 复制 `.env.example` 为 `.env`，填写 QQ 凭据和包含真实维护者联系方式的 `ZKILL_USER_AGENT`。
3. 设置一个非默认的 `POSTGRES_PASSWORD`。
   如需排除己方成员，可填写逗号分隔的 `FRIENDLY_CHARACTER_IDS`、`FRIENDLY_CORPORATION_IDS` 和 `FRIENDLY_ALLIANCE_IDS`。公共战报不包含游戏内联系人声望，因此需要显式配置这些 ID。
   如需主动预警，还应配置：

   ```dotenv
   EVE_SENTRY_EVENTS_URL=http://host.docker.internal:8765/api/v1/events
   EVE_SENTRY_PUBLIC_URL=http://YOUR_EVE_SENTRY_HOST
   EVE_SENTRY_ALERT_MIN_LEVEL=
   ```

   `EVE_SENTRY_ALERT_MIN_LEVEL` 留空表示推送所有等级，也可设置为 `low`、`medium`、`high` 或 `critical`。
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
