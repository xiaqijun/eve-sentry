# QQ 群消息事件兼容

机器人继续处理 QQ 的 `GROUP_AT_MESSAGE_CREATE`。开启群消息全量接收后，QQ 通过
`GROUP_MESSAGE_CREATE` 投递群消息；botpy 1.2 不识别该事件，客户端在登录后注册
兼容分发器。

全量事件可能是普通聊天。机器人仅处理消息开头明确 @ 自己的命令：先调用 QQ 的
`GET /v2/groups/{group_openid}/bot_state` 获取机器人在该群的 `member_openid`，与
消息中的首个 @ 标记比较。身份成功查询后在内存中缓存 5 分钟；查询失败或身份不匹配
时不处理消息，避免误回复。通过验证的消息复用原有命令处理和 Redis 消息 ID 去重，
不改变仅接收 @ 消息的群的行为。该身份接口需要 QQ 开放对应权限。

QQ 接口文档：[群消息（全量模式）](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)、
[获取机器人群内状态](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_bot_state.get.html)。
