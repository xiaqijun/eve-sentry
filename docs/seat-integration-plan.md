# EVE Sentry 与 GloryNavy_Seat 接入

状态：2026-10-04。当前收费合同只按服务端确认的认证客户端有效在线时长计费；监控奖励只按每个星系主节点的确认在线时长发放。事件次数、事件投递、客户端 ACK、预付授权、释放和退款不参与收费，也不保留兼容接口或历史账表。

## 当前数据流

- Sentry 保存密钥状态、主节点监控贡献区间和客户端认证心跳在线区间。一个在线心跳区间会按其时间窗内服务端确认的主节点监控星系展开为多条 `client-usage` 证据；每条证据只对应一个星系，便于 Seat 按星系扣费。
- Seat 保存账号归属、星系归因、按小时的预警价格与监控奖励价格，并在 exchange 内完成果壳币入账/扣款。
- 每个星系单独计量；同一星系只有主节点产生监控奖励。预警选择 N 个星系时，在线时长费用按 N 个星系分别计算。
- 相邻有效心跳形成一个在线区间，重复提交按客户端和时间边界幂等；断线、失败、授权代次变化或军团变化会切断区间。

## Sentry 接口

服务端接口使用 Seat 集成 Bearer Token：

- `GET/PUT /api/v1/integrations/seat/alert-consumption`：读取或同步收费开关。
- `GET /api/v1/integrations/seat/monitor-contributions`：分页导出主节点监控贡献。
- `GET /api/v1/integrations/seat/client-usage`：分页导出认证客户端在线区间；当同一时段覆盖多个服务端确认的监控星系时，每个 `system_id` 返回一条记录，`usage_id` 以星系归因为幂等边界。

事件、投递、ACK、alert-grants 及其对账接口已删除。接口不接收价格和币额，Sentry 不写 Seat 币账。

## Seat 结算约束

Seat 以 UTC 区间秒数和当前生效的小时价格计算果壳币，按账号和 `system_id` 独立幂等入账。价格修改只影响新的结算区间；前端只展示余额、预警价格、监控奖励价格和累计花费/奖励。

## 发布检查

1. 两端迁移完成且新库只创建 `seat_monitor_contributions`、`seat_client_usage` 与 `seat_integration_settings`。
2. 收费开关默认关闭，生产由管理员在 Seat 前端开启并同步 Sentry。
3. 使用真实测试账号验证一个星系的在线扣款、一个主节点的监控奖励和重复区间幂等。
4. 不再执行事件/ACK/释放/退款验收，也不保留旧阶段文档或数据表。
