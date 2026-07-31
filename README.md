# Qwen / OpenRouter 视频分析插件

默认专精阿里百炼/Qwen OpenAI 兼容视频理解，也可切换 OpenRouter Omni。插件会使用 yt-dlp 获取视频，并以图片卡片输出摘要。

## 指令

```text
/视频分析 你的问题 https://example.com/video
/视频分析自检 https://example.com/video
```

问题可以省略；省略时使用配置中的默认总结提示词。

## 推荐 Qwen 配置

```json
{
  "backend": {"mode": "qwen_video"},
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

`provider_id` 是 AstrBot 里的完整 Provider ID，不是序号。也可以直接填写 `qwen.api_key`。

## 输入策略

- 先由 yt-dlp 解析视频元信息。
- 优先尝试最小音画合一 progressive 媒体直链。
- 若直链因 Cookie/Referer/IP 绑定等原因不可访问，则下载最小可用视频并转 base64。
- B站/抖音等平台可在 `cookies` 配置里填写 cookies.txt / Cookie Header / 文件路径。

## OpenRouter 可选后端

若要继续试 OpenRouter：

```json
{
  "backend": {"mode": "openrouter_video"},
  "openrouter": {
    "provider_id": "openai_2",
    "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
  }
}
```

## 自检

```text
/视频分析自检 https://example.com/video
```

会检查当前后端文本、直接视频 URL、下载后 base64 视频、yt-dlp 元信息、字幕列表、限制条件。

## 安全输出

插件会把安全规则放在用户问题之后，要求模型先判断视频是否适合总结。如果视频疑似包含政治敏感、色情、血腥暴力、违法、仇恨、隐私泄露等内容，只输出 `SAFE=false` 与简短原因，不输出细节总结。此类拒绝仍会消耗每日次数。
