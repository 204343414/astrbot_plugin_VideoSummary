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

## URL 提取说明

如果 AstrBot 没有把整段参数作为 GreedyStr 传入，插件会回退读取完整消息原文再提取第一条 URL。因此下面这种前面带问题、后面带 Markdown 链接的格式也支持：

```text
/视频分析 这个视频内容 [https://www.bilibili.com/video/BVxxxx](https://www.bilibili.com/video/BVxxxx)
```

## Gemini Files API 注意

视频分析依赖 Gemini Files API 上传视频。默认情况下，插件只复用 AstrBot Gemini Provider 的 API Key 和模型，不继承 Provider 的 `api_base`，避免把不支持 Files API 的 OpenAI 兼容代理用于视频上传。

如需走代理，请确认代理支持 Gemini 原生上传接口 `/upload/v1beta/files`，然后配置：

```json
{
  "gemini": {
    "api_base": "https://your-gemini-compatible-base",
    "use_provider_api_base": false
  }
}
```

如果看到 `Upload URL did not returned from the create file request`，通常就是当前 `api_base`/代理不支持 Gemini Files API。

## 自检与链路定位

使用：

```text
/视频分析自检 https://example.com/video
```

该指令会自动检查：

1. 是否能读取 AstrBot Gemini Provider 的 API Key / 模型；
2. Gemini 文本生成是否可用；
3. Gemini Files API 是否支持上传；
4. yt-dlp 是否能解析 URL、视频时长、字幕/自动字幕列表；
5. 可选 OpenAI 兼容 STT `/v1/audio/transcriptions` 是否可用。

自检会脱敏显示 API Key。若中转站不支持 Gemini Files API，通常会在 “Gemini Files API” 步骤显示失败；如果配置了 `stt.enabled=true`，还会测试语音转文字中转接口。
