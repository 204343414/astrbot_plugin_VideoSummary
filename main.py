"""Qwen video summary plugin for AstrBot.

Specialized for Alibaba Qwen/OpenAI-compatible ``video_url`` input via
``/chat/completions``.  The plugin intentionally keeps a single Qwen path so
runtime behavior and diagnostics stay easy to reason about.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.core import html_renderer
except Exception:  # pragma: no cover
    html_renderer = None

try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover
    GreedyStr = str

PLUGIN_NAME = "astrbot_plugin_VideoSummary"
DEFAULT_QWEN_MODEL = "qwen3.5-omni-flash-2026-03-15"

# 阿里云百炼硬限制（查档：help.aliyun.com/zh/model-studio/error-code 与 /qwen-omni）
# 本地文件 Base64 编码后必须 < 10MB；Base64 膨胀约 4/3，故原始文件约 7.5MB 封顶。
QWEN_BASE64_LIMIT_BYTES = 10 * 1024 * 1024
QWEN_RAW_BASE64_SAFE_BYTES = int(QWEN_BASE64_LIMIT_BYTES * 3 / 4) - 4096
# 临时存储空间（免费，48 小时有效）
# Qwen3.5-Omni 官方时长上限：1 小时
QWEN_MAX_DURATION_MINUTES = 60.0
DASHSCOPE_UPLOAD_ENDPOINTS = {
    "cn": "https://dashscope.aliyuncs.com/api/v1/uploads",
    "intl": "https://dashscope-intl.aliyuncs.com/api/v1/uploads",
}


@register(
    PLUGIN_NAME,
    "204343414",
    "Qwen 视频内容分析与安全摘要",
    "0.7.3",
    "https://github.com/204343414/astrbot_plugin_VideoSummary",
)
class VideoSummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.data_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.usage_file = self.data_dir / "usage.json"
        self.usage = self._load_usage()

        limits = config.get("limits", {}) or {}
        # 查档：Qwen3.5-Omni 官方时长上限 1 小时，超过再大也无意义，硬性收敛。
        self.max_duration_minutes = min(
            max(float(limits.get("max_duration_minutes", QWEN_MAX_DURATION_MINUTES)), 0.1),
            QWEN_MAX_DURATION_MINUTES,
        )
        self.daily_limit_per_user = max(int(limits.get("daily_limit_per_user", 3)), 0)
        self.max_concurrent_jobs = max(int(limits.get("max_concurrent_jobs", 1)), 1)
        self.download_retries = max(int(limits.get("download_retries", 10) or 10), 1)
        self._semaphore = asyncio.Semaphore(self.max_concurrent_jobs)

        access = config.get("access_control", {}) or {}
        self.public_domestic_only = bool(access.get("public_domestic_only", True))
        self.operator_ids = self._parse_values(access.get("operator_openids", ""))
        self.allowed_group_openids = self._parse_values(access.get("allowed_group_openids", ""))
        self.allowed_instance_ids = self._parse_values(access.get("allowed_qqofficial_instance_ids", ""))

        qwen = config.get("qwen", {}) or {}
        self.qwen_provider_id = str(qwen.get("provider_id", "") or "").strip()
        self.qwen_api_key = str(qwen.get("api_key", "") or "").strip()
        self.qwen_base_url = str(
            qwen.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip().rstrip("/")
        self.qwen_model = str(qwen.get("model", DEFAULT_QWEN_MODEL) or DEFAULT_QWEN_MODEL).strip()
        if "/" in self.qwen_provider_id and not self.qwen_api_key:
            provider_part, model_part = self.qwen_provider_id.split("/", 1)
            if provider_part:
                self.qwen_provider_id = provider_part
                if model_part:
                    self.qwen_model = model_part
        self.qwen_fps = max(float(qwen.get("fps", 1)), 0.1)
        self.qwen_stream = bool(qwen.get("stream", True))
        self.qwen_base64_prefix = str(qwen.get("base64_prefix", "data:;base64,") or "data:;base64,")
        self.try_direct_media_url = bool(qwen.get("try_direct_media_url", True))
        self.use_temp_oss_upload = bool(qwen.get("use_temp_oss_upload", True))
        self.qwen_video_timeout_seconds = max(int(qwen.get("video_timeout_seconds", 300) or 300), 60)
        self.temp_oss_endpoint = str(qwen.get("temp_oss_endpoint", "auto") or "auto").strip().lower()

        cookies = config.get("cookies", {}) or {}
        self.bilibili_cookies = str(cookies.get("bilibili", "") or "").strip()
        self.douyin_cookies = str(cookies.get("douyin", "") or "").strip()
        self.youtube_cookies = str(cookies.get("youtube", "") or "").strip()
        self.generic_cookies = str(cookies.get("generic", "") or "").strip()
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        prompts = config.get("prompts", {}) or {}
        self.default_task_prompt = str(
            prompts.get(
                "default_task_prompt",
                "请总结这个视频的主要内容，提取关键信息、重要画面、人物/事件、结论，并给出简短时间线。",
            )
        ).strip()
        self.tail_instruction_prompt = str(
            prompts.get("tail_instruction_prompt", self._default_tail_prompt())
        ).strip()

        output = config.get("output", {}) or {}
        self.disclaimer = str(
            output.get(
                "disclaimer",
                "您请求的视频内容摘要已生成。以下内容由 AI 基于可见/可听信息自动整理，仅供参考，不代表事实核验或平台立场。",
            )
        ).strip()
        self.card_title = str(output.get("card_title", "AI 视频内容摘要")).strip()

        self._cleanup_stale_temp()
        logger.info(
            "[VideoSummary] loaded qwen_model=%s max_duration=%smin daily=%s public_domestic_only=%s",
            self.qwen_model,
            self.max_duration_minutes,
            self.daily_limit_per_user,
            self.public_domestic_only,
        )

    @staticmethod
    def _default_tail_prompt() -> str:
        return (
            "\n\n---\n"
            "请严格遵守以下输出规则：\n"
            "1. 先判断视频内容是否适合总结。如果包含政治敏感、色情/性暗示、未成年人性相关、血腥暴力、恐怖主义、违法犯罪教学、仇恨骚扰、开盒隐私或其它可能导致平台风险的内容，只输出：SAFE=false、简短原因，不要复述细节。\n"
            "2. 如果内容适合总结，输出 SAFE=true，并给出：一句话概括、要点列表、时间线、可核实信息/不确定信息。\n"
            "3. 不要编造看不到/听不到的信息；不确定就明确说不确定。\n"
            "4. 使用中文，保持克制、客观、适合在 QQ 群展示。\n"
            "5. 输出纯文本，不要 Markdown 表格。"
        )

    @staticmethod
    def _parse_values(value) -> set[str]:
        if isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = re.split(r"[\s,，;；]+", str(value or ""))
        return {str(item).strip() for item in values if str(item).strip()}

    @staticmethod
    def _extract_first_url(text: str) -> str:
        text = html.unescape(str(text or "").strip())
        md = re.search(r"\[[^\]]*\]\((https?://[^\s)]+)\)", text, flags=re.I)
        if md:
            url = md.group(1)
        else:
            m = re.search(r"https?://[^\s<>\[\](){}'\"`]+", text, flags=re.I)
            if not m:
                return ""
            url = m.group(0)
        trailing = "\r\n\t .,，。!！?？;；]})）】》>。"
        while url and url[-1] in trailing:
            url = url[:-1]
        return url

    @staticmethod
    def _normalize_bilibili_url(url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        host = parts.netloc.lower()
        if host.endswith("bilibili.com"):
            match = re.search(r"/video/((?:BV[0-9A-Za-z]+)|(?:av\d+))", parts.path)
            if match:
                return urlunsplit((parts.scheme or "https", parts.netloc, f"/video/{match.group(1)}/", "", ""))
        return url

    def _is_domestic_url_or_info(self, url: str, info: dict | None = None) -> bool:
        text = " ".join(
            [url.lower()]
            + [str((info or {}).get(k, "") or "").lower() for k in ("extractor", "extractor_key", "webpage_url", "original_url")]
        )
        domestic = (
            "bilibili", "b23.tv", "douyin", "ixigua", "weibo", "xiaohongshu",
            "youku", "iqiyi", "tencentvideo", "acfun", "kuaishou",
        )
        return any(k in text for k in domestic)

    def _sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return str(getattr(getattr(event, "message_obj", None), "sender", None).user_id)

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")

    def _platform_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_id() or "")
        except Exception:
            meta = getattr(event, "platform_meta", None)
            return str(getattr(meta, "id", "") or "")

    def _platform_aliases(self, event: AstrMessageEvent) -> set[str]:
        pid = self._platform_id(event)
        aliases = {pid} if pid else set()
        if pid.startswith("default_") and len(pid) > 8:
            aliases.add(pid[8:])
        return aliases

    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return bool(
            self._sender_id(event) in self.operator_ids
            or self._group_id(event) in self.allowed_group_openids
            or (self._platform_aliases(event) & self.allowed_instance_ids)
        )

    def _load_usage(self) -> dict:
        if not self.usage_file.exists():
            return {"date": date.today().isoformat(), "users": {}}
        try:
            data = json.loads(self.usage_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("users", {})
                return data
        except Exception:
            pass
        return {"date": date.today().isoformat(), "users": {}}

    def _save_usage(self) -> None:
        tmp = self.usage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.usage, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.usage_file)

    def _reset_usage_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.usage.get("date") != today:
            self.usage = {"date": today, "users": {}}
            self._save_usage()

    def _usage_count(self, user_id: str) -> int:
        self._reset_usage_if_needed()
        return int(self.usage.get("users", {}).get(str(user_id), 0) or 0)

    def _charge_usage(self, user_id: str) -> int:
        self._reset_usage_if_needed()
        users = self.usage.setdefault("users", {})
        used = int(users.get(str(user_id), 0) or 0) + 1
        users[str(user_id)] = used
        self._save_usage()
        return used

    def _cleanup_stale_temp(self) -> None:
        cutoff = time.time() - 24 * 3600
        for path in self.temp_dir.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def _cookie_key_for_url(self, url: str) -> str:
        try:
            from urllib.parse import urlsplit
            host = urlsplit(url).netloc.lower()
        except Exception:
            host = ""
        if "bilibili" in host or "b23.tv" in host:
            return "bilibili"
        if "douyin" in host:
            return "douyin"
        if "youtube" in host or "youtu.be" in host:
            return "youtube"
        return "generic"

    def _cookie_config_for_url(self, url: str) -> str:
        key = self._cookie_key_for_url(url)
        if key == "bilibili":
            return self.bilibili_cookies or self.generic_cookies
        if key == "douyin":
            return self.douyin_cookies or self.generic_cookies
        if key == "youtube":
            return self.youtube_cookies or self.generic_cookies
        return self.generic_cookies

    @staticmethod
    def _sanitize_cookie_text(value: str) -> str:
        value = html.unescape(str(value or ""))
        if ("\\n" in value or "\\t" in value) and "\n" not in value:
            value = value.replace("\\n", "\n").replace("\\t", "\t")
        value = value.replace("\\[n", "\n").replace("[n", "\n")
        value = re.sub(r"\[([^\]\s]+)\]\(https?://[^)]+\)", r"\1", value)
        cleaned_lines = []
        for line in value.splitlines():
            cleaned_lines.append(line.strip("\ufeff").lstrip("… "))
        return "\n".join(cleaned_lines)

    def _resolve_cookie_for_ytdlp(self, url: str) -> tuple[str | None, str | None]:
        value = self._cookie_config_for_url(url)
        if not value:
            return None, None
        value = self._sanitize_cookie_text(value)
        lowered = value.lower().lstrip()
        key = self._cookie_key_for_url(url)
        if "\n" in value or lowered.startswith("# netscape") or "\t" in value:
            cookie_path = self.cookie_dir / f"{key}.cookies.txt"
            cookie_path.write_text(value.rstrip() + "\n", encoding="utf-8")
            try:
                cookie_path.chmod(0o600)
            except OSError:
                pass
            return str(cookie_path), None
        try:
            expanded = Path(os.path.expanduser(value))
            if expanded.exists():
                return str(expanded), None
        except OSError:
            pass
        if lowered.startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        if ";" in value and "=" in value:
            return None, value
        raise ValueError("cookies 配置既不是已存在路径，也不像 cookies.txt 或 Cookie 请求头")

    def _ydl_headers(self, url: str) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        key = self._cookie_key_for_url(url)
        if key == "bilibili":
            headers["Referer"] = "https://www.bilibili.com/"
        elif key == "douyin":
            headers["Referer"] = "https://www.douyin.com/"
        return headers

    def _ydl_opts(self, url: str, *, download: bool, outtmpl: str | None = None, format_selector: str | None = None) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "http_headers": self._ydl_headers(url),
        }
        cookiefile, cookie_header = self._resolve_cookie_for_ytdlp(url)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        if cookie_header:
            opts["http_headers"]["Cookie"] = cookie_header
        if download:
            opts.update(
                {
                    "outtmpl": outtmpl,
                    "format": format_selector or self._download_format_selectors()[0],
                    "merge_output_format": "mp4",
                    "format_sort": ["+res", "vcodec:avc", "ext:mp4:m4a"],
                    # yt-dlp 默认 retries=None，RetryManager 会解析成 0 次重试：
                    # 跨境链路一次断流就直接 "N bytes read, M more expected" 报错。
                    "retries": self.download_retries,
                    "fragment_retries": self.download_retries,
                    "extractor_retries": 3,
                    "socket_timeout": 30,
                    "continuedl": True,
                    # 分块拉取，单块失败可重试，避免长连接被中途掐断。
                    "http_chunk_size": 5 * 1024 * 1024,
                }
            )
        else:
            opts["skip_download"] = True
        return opts

    @staticmethod
    def _download_format_selectors() -> list[str]:
        return [
            "worst[ext=mp4][vcodec!=none][acodec!=none]/worst[vcodec!=none][acodec!=none]",
            "worstvideo[vcodec^=avc1]+bestaudio[ext=m4a]/worstvideo[vcodec^=avc1]+bestaudio/worst[ext=mp4]/worst",
            "worstvideo+bestaudio/worst",
            "best[height<=240][vcodec!=none][acodec!=none]/bestvideo[height<=240]+bestaudio/best[height<=240]/worst",
            "best[height<=360][vcodec!=none][acodec!=none]/bestvideo[height<=360]+bestaudio/best[height<=360]/worst",
            "best[height<=480][vcodec!=none][acodec!=none]/bestvideo[height<=480]+bestaudio/best[height<=480]/worst",
            "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "bestvideo+bestaudio/best",
        ]

    async def _extract_info(self, url: str) -> dict:
        def task():
            with yt_dlp.YoutubeDL(self._ydl_opts(url, download=False)) as ydl:
                return ydl.extract_info(url, download=False)
        info = await asyncio.to_thread(task)
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            raise ValueError("仅支持单视频，不支持播放列表")
        return info

    async def _download_video(self, url: str, task_id: str) -> tuple[Path, dict]:
        outtmpl = str(self.temp_dir / f"{task_id}_%(id)s.%(ext)s")
        last_error: Exception | None = None
        for index, selector in enumerate(self._download_format_selectors(), start=1):
            for old_path in self.temp_dir.glob(f"{task_id}_*"):
                try:
                    old_path.unlink()
                except OSError:
                    pass
            def task():
                with yt_dlp.YoutubeDL(self._ydl_opts(url, download=True, outtmpl=outtmpl, format_selector=selector)) as ydl:
                    info = ydl.extract_info(url, download=True)
                    requested = info.get("requested_downloads") or []
                    candidates = [x.get("filepath") for x in requested if x.get("filepath")]
                    candidates += [info.get("filepath"), ydl.prepare_filename(info)]
                    return info, candidates
            try:
                info, candidates = await asyncio.to_thread(task)
                files = [Path(x) for x in candidates if x and Path(x).exists()]
                if not files:
                    files = sorted(self.temp_dir.glob(f"{task_id}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not files:
                    raise RuntimeError("下载完成但找不到输出文件")
                if index > 1:
                    logger.info("[VideoSummary] yt-dlp fallback selector #%d succeeded: %s", index, selector)
                return files[0], info
            except Exception as exc:
                last_error = exc
                logger.warning("[VideoSummary] yt-dlp selector #%d failed: %s error=%s", index, selector, exc)
                continue
        assert last_error is not None
        raise last_error

    @staticmethod
    def _format_size(format_info: dict) -> int:
        for key in ("filesize", "filesize_approx"):
            try:
                value = int(format_info.get(key) or 0)
                if value > 0:
                    return value
            except Exception:
                pass
        return 10**18

    def _select_progressive_media_url(self, info: dict) -> tuple[str, dict] | tuple[None, None]:
        candidates = []
        for fmt in info.get("formats") or []:
            if not isinstance(fmt, dict):
                continue
            media_url = str(fmt.get("url") or "")
            if not media_url.startswith(("http://", "https://")):
                continue
            if fmt.get("vcodec") in (None, "none") or fmt.get("acodec") in (None, "none"):
                continue
            ext_score = 0 if str(fmt.get("ext") or "").lower() == "mp4" else 1
            codec_score = 0 if str(fmt.get("vcodec") or "").startswith(("avc1", "h264")) else 1
            height = int(fmt.get("height") or 999999)
            candidates.append((ext_score, codec_score, height, self._format_size(fmt), media_url, fmt))
        if not candidates:
            return None, None
        candidates.sort(key=lambda item: item[:4])
        return candidates[0][4], candidates[0][5]

    def _qwen_video_data_url(self, video_path: Path) -> str:
        raw_size = video_path.stat().st_size
        if raw_size > QWEN_RAW_BASE64_SAFE_BYTES:
            raise RuntimeError(
                f"本地文件 {raw_size/1024/1024:.1f}MB，Base64 编码后会超过百炼 10MB 上限"
                f"（原始文件需 ≤ {QWEN_RAW_BASE64_SAFE_BYTES/1024/1024:.1f}MB）。"
            )
        encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
        if len(encoded) >= QWEN_BASE64_LIMIT_BYTES:
            raise RuntimeError(
                f"Base64 编码后 {len(encoded)/1024/1024:.1f}MB，超过百炼 10MB 上限。"
            )
        return f"{self.qwen_base64_prefix}{encoded}"

    def _dashscope_upload_url(self, base_url: str) -> str:
        if self.temp_oss_endpoint in DASHSCOPE_UPLOAD_ENDPOINTS:
            return DASHSCOPE_UPLOAD_ENDPOINTS[self.temp_oss_endpoint]
        host = (urlparse(base_url).hostname or "").lower()
        if "intl" in host or "ap-southeast" in host:
            return DASHSCOPE_UPLOAD_ENDPOINTS["intl"]
        return DASHSCOPE_UPLOAD_ENDPOINTS["cn"]

    async def _upload_to_temp_oss(self, api_key: str, base_url: str, model: str, video_path: Path) -> str:
        """上传本地文件到百炼免费临时存储空间，返回 oss:// 临时 URL（48 小时有效）。

        查档：https://help.aliyun.com/zh/model-studio/get-temporary-file-url
        注意：getPolicy 的 model 必须与后续推理所用 model 完全一致，且 API Key 需同一主账号。
        """
        uploads_url = self._dashscope_upload_url(base_url)
        timeout = aiohttp.ClientTimeout(total=600, connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                uploads_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                params={"action": "getPolicy", "model": model},
            ) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"getPolicy HTTP {resp.status}: {raw[:300]}")
                policy = (json.loads(raw) or {}).get("data") or {}
            required = ("upload_host", "upload_dir", "oss_access_key_id", "signature", "policy")
            missing = [key for key in required if not policy.get(key)]
            if missing:
                raise RuntimeError(f"getPolicy 返回缺少字段: {missing}")

            safe_name = self._oss_safe_filename(video_path.name)
            key = f"{str(policy['upload_dir']).rstrip('/')}/{safe_name}"
            fields = [
                ("OSSAccessKeyId", str(policy["oss_access_key_id"])),
                ("policy", str(policy["policy"])),
                ("Signature", str(policy["signature"])),
                ("key", key),
                ("x-oss-object-acl", str(policy.get("x_oss_object_acl", "private"))),
                ("x-oss-forbid-overwrite", str(policy.get("x_oss_forbid_overwrite", "true"))),
                ("success_action_status", "200"),
            ]
            prefix, suffix, content_type = self._oss_multipart_parts(fields, safe_name)
            total = len(prefix) + video_path.stat().st_size + len(suffix)

            async def stream_body():
                # 逐块读盘，避免把整个视频（最大可达 2GB）读进内存再拷贝一次。
                yield prefix
                with video_path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        yield block
                yield suffix

            async with session.post(
                str(policy["upload_host"]),
                data=stream_body(),
                headers={"Content-Type": content_type, "Content-Length": str(total)},
            ) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f"OSS 上传 HTTP {resp.status}: {(await resp.text())[:300]}")
        return f"oss://{key}"

    @staticmethod
    def _oss_safe_filename(name: str) -> str:
        """OSS PostObject 的 filename 需为 ASCII，非 ASCII 文件名会导致 MalformedPOSTRequest。"""
        stem, dot, ext = name.rpartition(".")
        ext = (ext if dot else "mp4").lower()
        ext = re.sub(r"[^A-Za-z0-9]", "", ext) or "mp4"
        cleaned = re.sub(r"[^A-Za-z0-9_\-]", "", (stem if dot else name))
        return f"{cleaned or 'video'}.{ext}"

    @staticmethod
    def _oss_multipart_parts(fields: list[tuple[str, str]], filename: str) -> tuple[bytes, bytes, str]:
        """手工拼装 multipart/form-data 的前后缀，文件内容由调用方流式塞在中间。

        不能用 aiohttp.FormData：它会给每个文本字段补 ``Content-Type: text/plain``，
        并把 ``Content-Type`` 排在 ``Content-Disposition`` 之前，OSS PostObject
        解析器对此会返回 400 MalformedPOSTRequest。file 字段必须排在最后，
        boundary 不能加引号。
        """
        boundary = f"----VideoSummary{os.urandom(12).hex()}"
        chunks: list[bytes] = []
        for name, value in fields:
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(f"{value}\r\n".encode())
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        prefix = b"".join(chunks)
        suffix = f"\r\n--{boundary}--\r\n".encode()
        return prefix, suffix, f"multipart/form-data; boundary={boundary}"

    @classmethod
    def _build_oss_multipart(cls, fields: list[tuple[str, str]], filename: str, payload: bytes) -> tuple[bytes, str]:
        prefix, suffix, content_type = cls._oss_multipart_parts(fields, filename)
        return prefix + payload + suffix, content_type

    async def _resolve_qwen_config(self, event: AstrMessageEvent) -> tuple[str, str, str, int, dict]:
        provider = None
        if self.qwen_provider_id:
            provider = await self.context.provider_manager.get_provider_by_id(self.qwen_provider_id)
            if provider is None:
                raise RuntimeError(f"未找到 Qwen/OpenAI Provider: {self.qwen_provider_id}")
        api_key = self.qwen_api_key
        base_url = self.qwen_base_url
        model = self.qwen_model
        timeout = 120
        custom_headers = {}
        if provider is not None:
            if not api_key:
                if hasattr(provider, "get_current_key"):
                    api_key = str(provider.get_current_key() or "")
                if not api_key:
                    api_key = str(getattr(provider, "chosen_api_key", "") or "")
                if not api_key:
                    keys = getattr(provider, "api_keys", []) or []
                    api_key = str(keys[0]) if keys else ""
            provider_config = getattr(provider, "provider_config", {}) or {}
            if not model:
                model = str(getattr(provider, "get_model", lambda: "")() or provider_config.get("model", "") or "")
            if not base_url or base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1":
                client = getattr(provider, "client", None)
                client_base = str(getattr(client, "base_url", "") or "").rstrip("/")
                base_url = client_base or str(provider_config.get("api_base", "") or base_url).rstrip("/")
            timeout = int(getattr(provider, "timeout", provider_config.get("timeout", 120)) or 120)
            custom_headers = dict(getattr(provider, "custom_headers", None) or provider_config.get("custom_headers", {}) or {})
        if not api_key:
            raise RuntimeError("Qwen API key 未配置；请填写 qwen.api_key 或 qwen.provider_id。")
        return api_key, base_url.rstrip("/"), model, timeout, custom_headers

    async def _qwen_chat(self, api_key: str, base_url: str, model: str, timeout_seconds: int, messages: list, custom_headers: dict | None = None, oss_resolve: bool = False) -> str:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if custom_headers:
            headers.update({str(k): str(v) for k, v in custom_headers.items()})
        if oss_resolve:
            # 查档：使用 oss:// 临时 URL 时必须显式携带该 Header，否则服务端无法解析。
            headers["X-DashScope-OssResourceResolve"] = "enable"
        payload = {"model": model, "messages": messages, "modalities": ["text"]}
        if self.qwen_stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        timeout = aiohttp.ClientTimeout(total=max(timeout_seconds, 30), connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    try:
                        data = json.loads(raw)
                        err = data.get("error") if isinstance(data, dict) else None
                        msg = err.get("message") if isinstance(err, dict) else raw
                    except Exception:
                        msg = raw
                    raise RuntimeError(f"Qwen HTTP {resp.status}: {str(msg)[:300]}")
                if self.qwen_stream:
                    parts: list[str] = []
                    reasoning_parts: list[str] = []
                    finish_reason = ""
                    stream_error = ""
                    saw_chunk = False
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload_text = line[5:].strip()
                        if not payload_text or payload_text == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload_text)
                        except Exception:
                            continue
                        saw_chunk = True
                        if isinstance(chunk.get("error"), dict):
                            stream_error = str(chunk["error"].get("message") or chunk["error"])
                        for choice in chunk.get("choices") or []:
                            if choice.get("finish_reason"):
                                finish_reason = str(choice["finish_reason"])
                            delta = choice.get("delta") or choice.get("message") or {}
                            parts.append(self._coerce_content_text(delta.get("content")))
                            reasoning_parts.append(self._coerce_content_text(delta.get("reasoning_content")))
                    text = "".join(parts).strip()
                    if text:
                        return text
                    if stream_error:
                        raise RuntimeError(f"Qwen 流式返回错误：{stream_error[:300]}")
                    reasoning = "".join(reasoning_parts).strip()
                    if reasoning:
                        # 只吐了思考没吐正文，多见于 omni 思考模式；把思考内容当结果总比空手而归好。
                        logger.warning("[VideoSummary] Qwen returned reasoning_content only")
                        return reasoning
                    if not saw_chunk:
                        raise RuntimeError(f"Qwen 流式响应无有效分片，原始响应：{raw[:300] or '<empty>'}")
                    raise RuntimeError(
                        f"Qwen 流式响应未含文本，finish_reason={finish_reason or '未知'}，原始响应：{raw[:300]}"
                    )
                data = json.loads(raw)
                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError(f"Qwen 未返回 choices: {str(data)[:300]}")
                message = choices[0].get("message") or {}
                content = self._coerce_content_text(message.get("content")).strip()
                if not content:
                    content = self._coerce_content_text(message.get("reasoning_content")).strip()
                if not content:
                    raise RuntimeError(
                        f"Qwen 返回空文本，finish_reason={choices[0].get('finish_reason') or '未知'}，"
                        f"原始响应：{raw[:300]}"
                    )
                return content

    @staticmethod
    def _coerce_content_text(content) -> str:
        """OpenAI 兼容响应的 content 可能是 str，也可能是 [{'type':'text','text':...}]。"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for item in content:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    value = item.get("text") or item.get("content") or ""
                    if isinstance(value, str):
                        out.append(value)
            return "".join(out)
        if isinstance(content, dict):
            value = content.get("text") or content.get("content") or ""
            return value if isinstance(value, str) else ""
        return ""

    async def _analyze_with_qwen(
        self,
        event: AstrMessageEvent,
        url: str,
        video_path: Path | None,
        user_question: str,
        user_id: str,
        local_mode: str = "auto",
    ) -> tuple[str, int]:
        """local_mode: auto=先临时OSS后base64；oss=只走临时OSS；base64=只走base64。"""
        api_key, base_url, model, timeout, custom_headers = await self._resolve_qwen_config(event)
        # 视频请求服务端要拉流+抽帧，比纯文本慢得多，单独放宽超时。
        timeout = max(timeout, self.qwen_video_timeout_seconds)
        prompt = self._build_prompt(user_question)

        async def call(video_ref: str, oss_resolve: bool) -> str:
            messages = [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": video_ref}, "fps": self.qwen_fps},
            ]}]
            return await self._qwen_chat(
                api_key, base_url, model, timeout, messages, custom_headers, oss_resolve=oss_resolve
            )

        if video_path is None:
            text = await call(url, False)
        else:
            raw_size = video_path.stat().st_size
            want_oss = local_mode == "oss" or (
                local_mode == "auto"
                and self.use_temp_oss_upload
                and raw_size > QWEN_RAW_BASE64_SAFE_BYTES
            )
            text = ""
            oss_error: Exception | None = None
            if want_oss:
                try:
                    oss_url = await self._upload_to_temp_oss(api_key, base_url, model, video_path)
                    logger.info("[VideoSummary] uploaded to DashScope temp OSS: %s", oss_url)
                    text = await call(oss_url, True)
                except Exception as exc:
                    oss_error = exc
                    if local_mode == "oss":
                        raise
                    logger.warning("[VideoSummary] temp OSS route failed, fallback to base64: %s", exc)
            if not text:
                try:
                    text = await call(self._qwen_video_data_url(video_path), False)
                except Exception as exc:
                    if oss_error is not None:
                        raise RuntimeError(
                            f"临时OSS上传失败（{type(oss_error).__name__}: {oss_error}）；"
                            f"base64 亦失败（{type(exc).__name__}: {exc}）"
                        ) from exc
                    raise
        if not text:
            raise RuntimeError("Qwen 未返回文本结果")
        used = self._charge_usage(user_id)
        return text, used

    def _build_prompt(self, user_question: str) -> str:
        return f"{str(user_question or '').strip() or self.default_task_prompt}\n{self.tail_instruction_prompt}"

    @staticmethod
    def _strip_command_prefix(text: str) -> str:
        text = str(text or "").strip()
        for prefix in ("/视频分析", "视频分析", "/视频分析自检", "视频分析自检"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text

    def _split_question_and_url(self, text: str) -> tuple[str, str]:
        text = self._strip_command_prefix(text)
        url = self._extract_first_url(text)
        question = str(text or "")
        if url:
            question = question.replace(url, " ", 1)
            question = re.sub(r"\[[^\]]*\]\(\s*" + re.escape(url) + r"\s*\)", " ", question)
        question = re.sub(r"\[[^\]]*\]\(\s*\)", " ", question)
        question = re.sub(r"\s+", " ", question).strip(" +，,。")
        return question, self._normalize_bilibili_url(url)

    @staticmethod
    def _escape(text: str) -> str:
        return html.escape(str(text or ""))

    async def _render_card(self, title: str, body: str, url: str, safe: bool = True) -> str:
        status = "摘要已生成" if safe else "已停止生成摘要"
        color = "#2563eb" if safe else "#b42318"
        html_doc = f"""
<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin:0; padding:28px; background:#f6f8fb; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif; color:#172033; }}
.card {{ max-width:880px; background:#fff; border:1px solid #e5e9f2; border-radius:18px; padding:28px; box-shadow:0 12px 34px rgba(31,41,55,.08); }}
h1 {{ margin:0 0 8px; font-size:28px; }}
.badge {{ display:inline-block; padding:5px 10px; border-radius:999px; background:{color}18; color:{color}; font-weight:700; margin:8px 0 16px; }}
.disclaimer {{ border-left:4px solid #94a3b8; padding:10px 14px; color:#475569; background:#f8fafc; border-radius:8px; line-height:1.55; }}
.content {{ white-space:pre-wrap; line-height:1.72; font-size:17px; margin-top:18px; }}
.url {{ margin-top:20px; color:#64748b; font-size:12px; word-break:break-all; }}
</style></head><body><div class="card"><h1>{self._escape(title or self.card_title)}</h1><div class="badge">{status}</div><div class="disclaimer">{self._escape(self.disclaimer)}</div><div class="content">{self._escape(body)}</div><div class="url">来源：{self._escape(url)}</div></div></body></html>
"""
        if html_renderer is None:
            raise RuntimeError("AstrBot T2I 渲染器不可用")
        rendered = await html_renderer.render_custom_template(html_doc, {}, return_url=False, options={"full_page": True, "type": "jpeg", "quality": 85})
        if isinstance(rendered, bytes):
            out = self.temp_dir / f"summary_{int(time.time() * 1000)}.jpg"
            out.write_bytes(rendered)
            return str(out)
        return str(rendered)

    def _looks_refusal(self, text: str) -> bool:
        lowered = text.lower()
        return "safe=false" in lowered or "safe: false" in lowered or "不适合总结" in text or "已停止" in text

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or "")
        if not value:
            return "<empty>"
        return value[:4] + "..." + value[-4:] if len(value) > 8 else value[:2] + "***"

    @staticmethod
    def _short_error(exc: BaseException, limit: int = 240) -> str:
        text = str(exc) or type(exc).__name__
        text = re.sub(r"[A-Za-z0-9_\-]{24,}", lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)
        return f"{type(exc).__name__}: {text[:limit]}"

    async def _probe_qwen_text(self, event: AstrMessageEvent) -> str:
        try:
            api_key, base_url, model, timeout, custom_headers = await self._resolve_qwen_config(event)
            text = await self._qwen_chat(api_key, base_url, model, timeout, [{"role": "user", "content": "请只回复 PONG"}], custom_headers)
            return f"OK：model={model} key={self._mask_secret(api_key)} base={base_url} resp={text[:80] or '<empty>'}"
        except Exception as exc:
            return "FAIL：" + self._short_error(exc)

    async def _probe_video(
        self,
        event: AstrMessageEvent,
        url: str,
        direct: bool,
        local_mode: str = "auto",
        cached: dict | None = None,
    ) -> str:
        """cached: 跨探针共享的下载结果，避免同一视频被重复下载三次。"""
        if not url:
            return "SKIP：未提供 URL"
        try:
            if direct:
                info = await self._extract_info(url)
                media_url, fmt = self._select_progressive_media_url(info)
                if not media_url:
                    return "SKIP：该站点无音画合一直链，本就应走下载路径"
                text, _ = await self._analyze_with_qwen(event, media_url, None, "请用中文用一句话描述这个视频。", "__diag__")
                self.usage.get("users", {}).pop("__diag__", None); self._save_usage()
                return f"OK：format={fmt.get('format_id')} resp={text[:160] or '<empty>'}"

            store = cached if cached is not None else {}
            if "error" in store:
                return "FAIL：（复用上一次下载结果）" + store["error"]
            if "path" not in store:
                try:
                    path, _info = await self._download_video(url, store.setdefault("task_id", f"diag_{int(time.time() * 1000)}"))
                    store["path"] = path
                except Exception as exc:
                    store["error"] = self._short_error(exc)
                    return "FAIL：" + store["error"]
            video_path = store["path"]
            size_mb = video_path.stat().st_size / 1024 / 1024
            if local_mode == "base64" and video_path.stat().st_size > QWEN_RAW_BASE64_SAFE_BYTES:
                return f"SKIP：本地文件 {size_mb:.1f}MB 超过 base64 安全上限 {QWEN_RAW_BASE64_SAFE_BYTES/1024/1024:.1f}MB"
            text, _ = await self._analyze_with_qwen(
                event, url, video_path, "请用中文用一句话描述这个视频。", "__diag__", local_mode=local_mode
            )
            self.usage.get("users", {}).pop("__diag__", None); self._save_usage()
            return f"OK：file={size_mb:.1f}MB resp={text[:160] or '<empty>'}"
        except Exception as exc:
            self.usage.get("users", {}).pop("__diag__", None); self._save_usage()
            return "FAIL：" + self._short_error(exc)

    @filter.command("视频分析")
    async def video_analyze(self, event: AstrMessageEvent, text: GreedyStr):
        event.stop_event()
        user_id = self._sender_id(event)
        if not user_id:
            yield event.plain_result("无法识别当前用户。")
            return
        if self.daily_limit_per_user > 0 and self._usage_count(user_id) >= self.daily_limit_per_user:
            yield event.plain_result(f"今日视频分析次数已用完（{self.daily_limit_per_user} 次）。")
            return
        raw_text = str(text or "")
        question, url = self._split_question_and_url(raw_text)
        if not url:
            try: question, url = self._split_question_and_url(event.get_message_str())
            except Exception: pass
        if not url:
            yield event.plain_result("请提供包含 http/https 的视频链接。")
            return
        async with self._semaphore:
            task_id = str(int(time.time() * 1000))
            try:
                info = await self._extract_info(url)
                domestic = self._is_domestic_url_or_info(url, info)
                if self.public_domestic_only and not domestic and not self._is_privileged(event):
                    yield event.plain_result("公开视频分析模式仅支持国内平台；国外或高风险站点仅限管理员、操作员或白名单群使用。")
                    return
                duration = float(info.get("duration") or 0)
                if duration and duration > self.max_duration_minutes * 60:
                    yield event.plain_result(f"视频时长 {duration/60:.1f} 分钟，超过上限 {self.max_duration_minutes:.1f} 分钟。")
                    return
                downloaded_info = info
                direct_media_url, direct_fmt = (None, None)
                if self.try_direct_media_url:
                    direct_media_url, direct_fmt = self._select_progressive_media_url(info)
                if direct_media_url:
                    try:
                        analysis, used = await self._analyze_with_qwen(event, direct_media_url, None, question, user_id)
                    except Exception as direct_exc:
                        logger.warning("[VideoSummary] Qwen direct media URL failed, fallback to base64: %s", direct_exc)
                        video_path, downloaded_info = await self._download_video(url, task_id)
                        analysis, used = await self._analyze_with_qwen(event, url, video_path, question, user_id)
                else:
                    video_path, downloaded_info = await self._download_video(url, task_id)
                    analysis, used = await self._analyze_with_qwen(event, url, video_path, question, user_id)
                title = str(downloaded_info.get("title") or info.get("title") or self.card_title)
                image = await self._render_card(title, analysis, url, safe=not self._looks_refusal(analysis))
                yield event.image_result(image)
                logger.info("[VideoSummary] success user=%s used=%s url=%s", user_id, used, url)
            except Exception as exc:
                logger.exception("[VideoSummary] failed")
                yield event.plain_result(f"视频分析失败：{type(exc).__name__}: {exc}")
            finally:
                for path in self.temp_dir.glob(f"{task_id}_*"):
                    try: path.unlink()
                    except OSError: pass

    @filter.command("视频分析自检")
    async def diagnostics(self, event: AstrMessageEvent, text: GreedyStr = ""):
        event.stop_event()
        lines = ["🧪 视频分析插件自检（qwen_video）"]
        _q, url = self._split_question_and_url(str(text or ""))
        if not url:
            try: _q, url = self._split_question_and_url(event.get_message_str())
            except Exception: pass
        lines.append(f"URL：{url or '未提供'}")
        lines.append("Qwen 文本：" + await self._probe_qwen_text(event))
        cached: dict = {}
        try:
            lines.append("Qwen 直接视频URL：" + await self._probe_video(event, url, direct=True))
            lines.append("Qwen 下载后临时OSS：" + (
                await self._probe_video(event, url, direct=False, local_mode="oss", cached=cached)
                if self.use_temp_oss_upload else "SKIP：qwen.use_temp_oss_upload=false"
            ))
            lines.append("Qwen 下载后base64：" + await self._probe_video(event, url, direct=False, local_mode="base64", cached=cached))
        finally:
            task_id = cached.get("task_id")
            if task_id:
                for path in self.temp_dir.glob(f"{task_id}_*"):
                    try: path.unlink()
                    except OSError: pass
        lines.append(
            f"百炼上限：base64 编码后 <10MB（原始 ≤{QWEN_RAW_BASE64_SAFE_BYTES/1024/1024:.1f}MB）；"
            f"URL 方式 Qwen3.5-Omni ≤2GB/1小时。临时OSS端点={self._dashscope_upload_url(self.qwen_base_url)}"
        )
        if url:
            try:
                info = await self._extract_info(url)
                duration = float(info.get("duration") or 0)
                domestic = self._is_domestic_url_or_info(url, info)
                subs = sorted((info.get("subtitles") or {}).keys())[:12]
                auto_subs = sorted((info.get("automatic_captions") or {}).keys())[:12]
                direct_url, direct_fmt = self._select_progressive_media_url(info)
                direct_desc = "无" if not direct_url else f"format={direct_fmt.get('format_id')} height={direct_fmt.get('height')} ext={direct_fmt.get('ext')}"
                lines.append(f"yt-dlp 元信息：OK extractor={info.get('extractor') or info.get('extractor_key')} duration={duration/60:.1f}min domestic={domestic}")
                lines.append(f"最小音画合一直链：{direct_desc}")
                lines.append(f"字幕：manual={subs or '无'} auto={auto_subs or '无'}")
                lines.append("限制：" + (f"WARN 视频超过 max_duration_minutes={self.max_duration_minutes}" if duration and duration > self.max_duration_minutes * 60 else "OK 时长未超限或未知"))
            except Exception as exc:
                lines.append("yt-dlp 元信息：FAIL " + self._short_error(exc))
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        self._save_usage()
