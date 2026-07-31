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

## 下载格式回退

对 B站等格式组合不稳定的平台，yt-dlp 会使用多轮格式 fallback：优先小体积/低分辨率 MP4，失败后逐步放宽到站点可用的 best/worst 格式，并继续受 `limits.max_file_size_mb` 约束。

## 低清下载优先

为了降低 OpenRouter base64 请求体大小，插件下载视频时会优先选择最小可用分辨率的视频流并合并音频；若站点没有对应格式，会逐步放宽格式选择。最终文件仍受 `limits.max_file_size_mb` 与 `openrouter.max_base64_video_mb` 限制。

## yt-dlp Cookies

和下载插件一致，视频分析也支持在 JSON 中配置 cookies：

```json
{
  "cookies": {
    "bilibili": "# Netscape HTTP Cookie File...",
    "douyin": "...",
    "youtube": "...",
    "generic": "..."
  }
}
```

可以粘贴 Netscape cookies.txt 全文、浏览器 Cookie 请求头，或服务器上的 cookies.txt 文件路径。用于解决 B站/b23 412、抖音风控、YouTube 登录限制等。

## 直链优先与 base64 兜底

插件会先尝试从 yt-dlp 元信息里寻找最小的音画合一 progressive 媒体直链，并把该 URL 直接交给 OpenRouter。若上游模型无法访问该直链（常见原因：Cookie/Referer/IP 绑定、签名过期、防盗链），会自动回退到 yt-dlp 下载并 base64 传输。

注意：OpenRouter video input 只有 URL 和 base64 data URL 两种形式；没有独立的“文件上传后传 file_id”接口。
