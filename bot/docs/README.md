# 机器人文档

这里记录 QQ 机器人当前可用行为和发布约束。接口字段以服务端仓库的
[API 参考](https://github.com/xiaqijun/eve-sentry/blob/main/docs/api-reference.md) 为准；
客户端按需 OCR 协议以本单体仓库的
[对接说明](../../client/docs/on-demand-ocr-query.md) 为准。

## 文档入口

- [机器人 CI/CD](ci-cd.md)：`main` 推送、自动部署、生产环境变量和回滚。
- [SSE 重连约束](sse-reconnect-guardrails.md)：首字节、心跳、游标和节点快照去重规则。
- [指令新增约束](command-guidelines.md)：命名、标准化解析、参数校验、面板同步和测试要求。

## 查询命令

```text
@机器人 查询
@机器人 查询星系 S-KSWL
@机器人 查询节点敌情
@机器人 查询人员 Alice
@机器人 查询军团 Blue Corp
@机器人 查询联盟 Example Alliance
@机器人 查询所有节点
@机器人 查询预警节点
```

`查询` 打开 Markdown 菜单；配置 `QQ_QUERY_KEYBOARD_ID` 后附带 QQ 静态按钮模板，未配置时
仍显示可复制的文字指令。`查询节点敌情` 和 `查询预警节点` 直接读取服务端现有状态，
不下发 OCR。指定星系只查询该星系窗口；人员、军团、联盟和所有节点查询会创建一次性
OCR 任务，机器人先确认任务已下发，再异步发送本次结果。

## 上线监测

“上线”表示目标出现在客户端当前 OCR 名单中，不表示游戏账号登录状态：

```text
@机器人 上线监测 人员 Alice 30s
@机器人 上线监测 军团 Rat Nation 1m
@机器人 上线监测 联盟 Example Alliance 5m
@机器人 上线监测列表
@机器人 取消上线监测 M001
@机器人 取消上线监测 人员 Alice
@机器人 取消全部上线监测
```

最低间隔 30 秒，默认 60 秒。任务保存在 PostgreSQL；同一周期的到期规则合并为一次
全节点 OCR，完整结果可短时复用。只在“未出现 → 出现”时通知；连续两次完整扫描未发现
后重新布防。节点离线、部分响应或 OCR 失败不会被当作目标消失。

## 维护规则

- 事件字段、游标或认证变化时，先更新服务端 API 文档，再同步本仓库说明。
- 预警去重、SSE 重连和节点快照行为必须同时满足 `sse-reconnect-guardrails.md`。
- 默认直接在 `main` 开发并推送，由工作流自动验证和部署；不创建额外分支或 PR。
