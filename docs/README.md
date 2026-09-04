# 机器人文档

这里记录 QQ 机器人当前可用行为和发布约束。接口字段以服务端仓库的
[API 参考](https://github.com/xiaqijun/eve-sentry/blob/main/docs/api-reference.md) 为准；
客户端按需 OCR 协议以客户端仓库的
[对接说明](https://github.com/xiaqijun/eve-sentry-client/blob/main/docs/on-demand-ocr-query.md) 为准。

## 文档入口

- [机器人 CI/CD](ci-cd.md)：`main` 推送、自动部署、生产环境变量和回滚。
- [SSE 重连约束](sse-reconnect-guardrails.md)：首字节、心跳、游标和节点快照去重规则。
- [指令新增约束](command-guidelines.md)：命名、标准化解析、参数校验、面板同步和测试要求。

## 查询命令

```text
@机器人 查询预警
@机器人 查询预警 人员 Alice
@机器人 查询预警 军团 Blue Corp
@机器人 查询预警 联盟 Example Alliance
@机器人 查询人员 Alice
@机器人 查询军团 Blue Corp
@机器人 查询联盟 Example Alliance
```

查询由服务端创建一次性 OCR 任务，在线客户端执行并上传单次快照；机器人不直接读取
客户端屏幕，也不把 OCR 原始名单当作敌对名单。结果只展示服务端已确认的 active 敌对人员。

## 维护规则

- 事件字段、游标或认证变化时，先更新服务端 API 文档，再同步本仓库说明。
- 预警去重、SSE 重连和节点快照行为必须同时满足 `sse-reconnect-guardrails.md`。
- 默认直接在 `main` 开发并推送，由工作流自动验证和部署；不创建额外分支或 PR。
