# 1.0.77 预警 SSE 连接修复

`1.0.77` 修复内嵌预警客户端使用持久 HTTP 连接时的 SSE 参数兼容问题。

- 预警 SSE 请求支持 `ack_capability`、`connection_id` 和 `client_id`；
- 请求会携带对应的 ACK、连接和客户端标识请求头；
- 修复客户端因参数不兼容而在发出 SSE 请求前直接显示“连接异常”的问题。
