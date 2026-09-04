# 客户端文档

## 文档入口

- [客户端操作指南](client.md)：下载、连接服务端、选择窗口、监控和常见问题。
- [按需 OCR 查询对接](on-demand-ocr-query.md)：机器人查询时的 heartbeat 命令、单次 OCR、
  `query_id` 上传和兼容性要求。
- [CI/CD 与发布流程](release-process.md)：测试、构建、签名、Release 和回滚边界。

客户端只实现窗口采集、OCR 和可靠上传；敌我分类、军团/联盟/声望判断由服务端完成。
按需查询不会改变常规 OCR 开关，也不会要求客户端自行调用 ESI。

服务端接口字段以
[eve-sentry API 参考](https://github.com/xiaqijun/eve-sentry/blob/main/docs/api-reference.md)
为准。后续默认直接在 `main` 开发并推送，客户端 CI/CD 负责验证和发布。
