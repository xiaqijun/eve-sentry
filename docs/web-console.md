# Web 管理系统

前端是 `frontend/` 下的 React 18 + TypeScript SPA。生产构建由 OpenResty/Nginx
托管，Python 服务只提供 `/api/`。

## 页面

| 路径 | 页面 | 权限 |
| --- | --- | --- |
| `/` | 实时态势图和敌对观察 | 认证关闭时公开，否则需登录 |
| `/reports` | 敌对来袭报表 | 登录用户 |
| `/account/keys` | 设备密钥列表 | 登录用户 |
| `/account/security` | 管理员账号安全 | 管理员 |
| `/admin/users` | 用户管理 | 管理员 |
| `/admin/identity` | 允许军团和用户角色白名单 | 管理员 |
| `/admin/audit` | 中文审计日志 | 管理员 |
| `/login` | 管理员密码和普通用户 EVE SSO 登录 | 未登录用户 |

`/account` 重定向到 `/account/keys`，`/admin` 重定向到 `/admin/users`。管理功能按
职责拆分为独立页面，不在同一页面堆叠用户、身份和审计内容。

## 数据约束

- 态势图节点只显示监控在线状态和确认的敌对数量。
- 敌对观察列表不显示旧威胁分数字段。
- 来袭报表只统计服务端生成的敌对告警，不把友军、未知角色、心跳或 OCR 当前名单直接
  计为来袭。
- 所有用户统计必须来自已解析到真实 EVE 角色 ID 的记录。
- zKillboard 不在当前链路，页面不得显示虚构战报、击毁数或 ISK 数据。

## 开发与构建

```powershell
cd frontend
npm ci
npm run dev
```

开发地址为 `http://127.0.0.1:5173`。测试与生产构建：

```powershell
npm test
npm run build
npm run preview
```

预览地址为 `http://127.0.0.1:4173`。生产代理必须让未知页面路径回退到
`index.html`，否则直接访问 `/reports` 或 `/admin/users` 会返回 404。

## 实时更新

工作台读取 `/api/v1/bootstrap` 获取初始状态，通过 `/api/v1/events` SSE 接收后续
告警和节点变化。来袭报表当前读取兼容接口 `/api/alerts`。代理层必须为事件流关闭
buffering 和 cache，并保留较长的读取超时。

静态资源文件名带内容哈希，可以在 `/assets/` 配置长期缓存；`index.html` 不应使用
同样的 immutable 缓存策略，以免部署后继续引用旧资源。
