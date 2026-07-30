# Gemini 视频分析插件

使用 Gemini Files API 分析视频 URL，并以图片卡片输出摘要。

## 指令

```text
/视频分析 你的问题 https://example.com/video
```

问题可以省略；省略时使用配置中的默认总结提示词。

## 设计说明

AstrBot v4.26.x 的 ProviderRequest 支持图片与音频输入，但没有一等视频媒体输入。因此本插件直接复用 AstrBot 中已配置的 Gemini Provider 的 API Key/模型，然后调用 Gemini Files API 上传视频并分析。

## 默认安全输出

插件会把安全与格式提示词放在用户问题之后，要求模型先判断视频是否适合总结。如果视频疑似包含政治敏感、色情、血腥暴力、违法、仇恨、隐私泄露等内容，只输出 `SAFE=false` 与简短原因，不输出细节总结。此类拒绝仍会消耗每日次数。

## 额度与限制

- `limits.max_duration_minutes`：最大视频分钟数。
- `limits.max_file_size_mb`：最大下载文件大小。
- `limits.daily_limit_per_user`：每人每日次数。
- `access_control.public_domestic_only`：开启后普通用户只允许国内平台 URL；国外/高风险站点仅白名单/管理员/操作员可用。

次数在视频成功下载并进入 Gemini 流程后扣除；URL 解析失败、下载失败、超过时长/大小不扣。
