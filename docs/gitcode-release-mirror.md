# GitCode 发布镜像

Windows 客户端版本由 `.github/workflows/release-client.yml` 构建。每个新版本先创建或更新
GitHub Release，再由 `scripts/publish_gitcode_release.ps1` 将完整包、程序包、频道客户端、
OCR 模型包和签名清单上传到 `xiaqiqi/eve-sentry-releases` 的 GitCode Release。源码镜像
`xiaqiqi/eve-sentry` 不支持创建 Release，因此不用于托管客户端附件。

## 配置

GitHub 仓库 Actions Secret 必须包含：

- `GITCODE_TOKEN`：可管理 `xiaqiqi/eve-sentry-releases` Release 的 GitCode PAT。
- `EVE_SENTRY_UPDATE_SIGNING_PRIVATE_KEY_B64`：现有 Ed25519 更新签名私钥。

令牌只通过环境变量传给发布脚本。不得写入仓库、Release 附件、下载 URL或命令行参数。

## 下载顺序

`latest.json` 保留原有 `url` 字段作为 Cloudflare 回退，并在程序包和模型组件中加入已签名
的 `mirrors` 数组。客户端按以下顺序下载：

1. GitCode Release 国内镜像。
2. 原 Cloudflare 下载地址。

切换下载源时保留有效的 `.part` 断点；每个来源下载完成后仍必须通过文件大小和 SHA-256
校验，清单本身仍必须通过 Ed25519 签名校验。

## 发布约束

GitCode CDN 会缓存 Release 附件路径。正式附件使用版本化文件名，每个版本只发布一组确定
内容；不要在相同 Tag 和文件名下替换不同二进制。如需更换内容，应提升 `app/version.py`
中的版本号并发布新 Tag。发布脚本检测到同名附件时会直接失败，不会尝试覆盖 CDN 已缓存的
内容。

发布脚本会在上传后匿名请求每个附件的前 1 KiB，要求服务端返回 `206 Partial Content` 和
正确的 `Content-Range`。GitCode 上传或范围下载校验失败会阻断客户端发布，避免签名清单
指向未就绪的镜像。

GitCode 的源码同步可能晚于 GitHub push。Release 会绑定 GitCode 当前可用的 `main` 分支，
不会因源码镜像延迟阻塞客户端附件发布；客户端实际使用的二进制版本仍由签名清单、文件大小和
SHA-256 唯一约束。
