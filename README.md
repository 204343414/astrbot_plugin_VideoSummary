# Qwen 视频分析插件

专精阿里百炼/Qwen OpenAI 兼容视频理解。插件使用 yt-dlp 获取视频，并以图片卡片输出摘要。

## 指令

```text
/视频分析 你的问题 https://example.com/video
/视频分析自检 https://example.com/video
```

问题可以省略；省略时使用配置中的默认总结提示词。

## 推荐 Qwen 配置

```json
{
  "qwen": {
    "provider_id": "阿里",
    "model": "qwen3.5-omni-flash-2026-03-15",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "fps": 1,
    "stream": true,
    "base64_prefix": "data:;base64,"
  }
}
```

`provider_id` 是 AstrBot 里的完整 Provider ID。也可以直接填写 `qwen.api_key`。

## 输入策略

- 先由 yt-dlp 解析视频元信息。
- 优先尝试最小音画合一 progressive 媒体直链。
- 若直链因 Cookie/Referer/IP 绑定等原因不可访问，则下载最小可用视频并转 base64。
- B站/抖音等平台可在 `cookies` 配置里填写 cookies.txt / Cookie Header / 文件路径。

## 只按时长限制

插件不再按文件大小限制视频。唯一资源限制是：

- `limits.max_duration_minutes`：最大视频分钟数；
- `limits.daily_limit_per_user`：每人每日次数；
- `limits.max_concurrent_jobs`：最大并发任务数。

## 自检

```text
/视频分析自检 https://example.com/video
```

会检查当前 Qwen 文本、Qwen 直接视频 URL、Qwen 下载后 base64 视频、yt-dlp 元信息、最小音画合一直链、字幕列表和时长限制。

## 安全输出

插件会把安全规则放在用户问题之后，要求模型先判断视频是否适合总结。如果视频疑似包含政治敏感、色情、血腥暴力、违法、仇恨、隐私泄露等内容，只输出 `SAFE=false` 与简短原因，不输出细节总结。此类拒绝仍会消耗每日次数。
