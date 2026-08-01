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
    "base64_prefix": "data:;base64,",
    "use_temp_oss_upload": true,
    "temp_oss_endpoint": "auto"
  }
}
```

`provider_id` 是 AstrBot 里的完整 Provider ID。也可以直接填写 `qwen.api_key`。

## 输入策略

- 先由 yt-dlp 解析视频元信息。
- 优先尝试最小音画合一 progressive 媒体直链。
- 若直链因 Cookie/Referer/IP 绑定等原因不可访问，则下载最小可用视频，并按以下顺序上送：
  1. **百炼临时存储空间**：文件超过约 7.5MB 时，先 `getPolicy` 上传到百炼免费临时 OSS（48 小时有效），拿到 `oss://` URL 后带 `X-DashScope-OssResourceResolve: enable` 推理；
  2. **Base64 内联**：小文件或临时 OSS 失败时回退。
- B站/抖音等平台可在 `cookies` 配置里填写 cookies.txt / Cookie Header / 文件路径。

## 百炼官方大小 / 时长上限

以下为阿里云百炼硬性限制，插件据此做前置校验，避免白跑一趟撞 `HTTP 413`：

| 传入方式 | 上限 |
| --- | --- |
| Base64 内联 | 编码后 **< 10MB**（原始文件约 ≤ 7.5MB） |
| 公网 / 临时 URL（Qwen3.5-Omni） | 2GB，时长 1 小时 |
| 公网 URL（Qwen3-Omni-Flash） | 256MB，时长 150 秒 |
| 公网 URL（Qwen-Omni-Turbo） | 150MB，时长 40 秒 |

临时存储空间注意事项：`getPolicy` 时的 `model` 必须与推理所用 `model` 完全一致；上传与推理的 API Key 需属于同一阿里云主账号；`getPolicy` 接口有限流。

## 资源限制

- `limits.max_duration_minutes`：最大视频分钟数，默认 60，且会被硬性收敛到官方上限 60；
- `limits.daily_limit_per_user`：每人每日次数；
- `limits.max_concurrent_jobs`：最大并发任务数。

## 自检

```text
/视频分析自检 https://example.com/video
```

会检查当前 Qwen 文本、Qwen 直接视频 URL、Qwen 下载后临时 OSS、Qwen 下载后 base64、yt-dlp 元信息、最小音画合一直链、字幕列表和时长限制。

## 安全输出

插件会把安全规则放在用户问题之后，要求模型先判断视频是否适合总结。如果视频疑似包含政治敏感、色情、血腥暴力、违法、仇恨、隐私泄露等内容，只输出 `SAFE=false` 与简短原因，不输出细节总结。此类拒绝仍会消耗每日次数。
