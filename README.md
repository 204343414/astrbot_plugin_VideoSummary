# OpenRouter Omni 视频分析插件

专精 OpenRouter `video_url` 输入，用 NVIDIA Omni 等支持视频输入的模型分析视频 URL，并以图片卡片输出摘要。

## 指令

```text
/视频分析 你的问题 https://example.com/video
/视频分析自检 https://example.com/video
```

问题可以省略；省略时使用配置中的默认总结提示词。

## 推荐配置

```json
{
  "openrouter": {
    "provider_id": "openai_2",
    "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "base_url": "https://openrouter.ai/api/v1"
  }
}
```

`provider_id` 是 AstrBot 里的完整 Provider ID，例如 `openai_2`，不是数字 `2`。也可以直接填写 `openrouter.api_key`。

注意：`nvidia/nemotron-3-nano-30b-a3b:free` 文本可用，但 OpenRouter 自检显示它不支持 video input。视频理解请使用带 `omni` 的模型或其它 OpenRouter 上明确支持 video input 的模型。

## URL 策略

- 默认所有平台都先由 yt-dlp 下载低清 MP4，再在 `openrouter.max_base64_video_mb` 限制内转成 `data:video/mp4;base64,...` 传给 OpenRouter。
- 如果开启 `openrouter.youtube_direct_url`，YouTube 会先尝试直接 URL；若上游无法打开视频流，会自动回退到下载后 base64。

## 安全输出

插件会把安全规则放在用户问题之后，要求模型先判断视频是否适合总结。如果视频疑似包含政治敏感、色情、血腥暴力、违法、仇恨、隐私泄露等内容，只输出 `SAFE=false` 与简短原因，不输出细节总结。此类拒绝仍会消耗每日次数。

## 自检

```text
/视频分析自检 https://www.youtube.com/watch?v=...
```

自检会检查：

1. OpenRouter 文本是否可用；
2. OpenRouter 直接 `video_url` 是否可用；
3. OpenRouter 下载后 base64 视频是否可用；
4. yt-dlp 是否能解析 URL、视频时长、字幕/自动字幕列表；
5. 时长限制是否会拦截。

## 额度与限制

- `limits.max_duration_minutes`：最大视频分钟数。
- `limits.max_file_size_mb`：最大下载文件大小。
- `limits.daily_limit_per_user`：每人每日次数。
- `openrouter.max_base64_video_mb`：非 YouTube base64 视频最大体积。
- `access_control.public_domestic_only`：开启后普通用户只允许国内平台 URL；国外/高风险站点仅白名单/管理员/操作员可用。

次数在视频成功构造为 OpenRouter 输入后扣除；URL 解析失败、下载失败、超过时长/大小不扣。安全拒绝扣除次数。
