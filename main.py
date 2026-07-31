"""Gemini video summary plugin for AstrBot.

This plugin intentionally calls Gemini's Files API directly instead of going
through AstrBot ProviderRequest: AstrBot v4.26.x has image/audio media fields,
but no first-class video part in ProviderRequest.
"""
from __future__ import annotations

import asyncio
import base64
import html
import wave
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import aiohttp
import yt_dlp
from google import genai
from google.genai import types

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


@register(
    PLUGIN_NAME,
    "204343414",
    "Gemini 视频内容分析与安全摘要",
    "0.2.1",
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
        self.max_duration_minutes = max(float(limits.get("max_duration_minutes", 10)), 0.1)
        self.max_file_size_mb = max(float(limits.get("max_file_size_mb", 120)), 1.0)
        self.daily_limit_per_user = max(int(limits.get("daily_limit_per_user", 3)), 0)
        self.max_concurrent_jobs = max(int(limits.get("max_concurrent_jobs", 1)), 1)
        self._semaphore = asyncio.Semaphore(self.max_concurrent_jobs)

        access = config.get("access_control", {}) or {}
        self.public_domestic_only = bool(access.get("public_domestic_only", True))
        self.operator_ids = self._parse_values(access.get("operator_openids", ""))
        self.allowed_group_openids = self._parse_values(access.get("allowed_group_openids", ""))
        self.allowed_instance_ids = self._parse_values(access.get("allowed_qqofficial_instance_ids", ""))

        gemini = config.get("gemini", {}) or {}
        self.provider_id = str(gemini.get("provider_id", "") or "").strip()
        self.model = str(gemini.get("model", "") or "").strip()
        self.api_base_override = str(gemini.get("api_base", "") or "").strip() or None
        self.use_provider_api_base = bool(gemini.get("use_provider_api_base", False))
        self.file_poll_timeout_seconds = max(int(gemini.get("file_poll_timeout_seconds", 180)), 30)
        self.file_poll_interval_seconds = max(float(gemini.get("file_poll_interval_seconds", 3)), 1.0)

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

        stt = config.get("stt", {}) or {}
        self.stt_enabled = bool(stt.get("enabled", False))
        self.stt_api_base_url = str(stt.get("api_base_url", "") or "").strip().rstrip("/")
        self.stt_api_key = str(stt.get("api_key", "") or "").strip()
        self.stt_model = str(stt.get("model", "whisper-1") or "whisper-1").strip()

        backend = config.get("backend", {}) or {}
        self.backend_mode = str(backend.get("mode", "openrouter_video") or "openrouter_video").strip().lower()
        openrouter = config.get("openrouter", {}) or {}
        self.openrouter_provider_id = str(openrouter.get("provider_id", "") or "").strip()
        self.openrouter_api_key = str(openrouter.get("api_key", "") or "").strip()
        self.openrouter_base_url = str(
            openrouter.get("base_url", "https://openrouter.ai/api/v1")
            or "https://openrouter.ai/api/v1"
        ).strip().rstrip("/")
        self.openrouter_model = str(
            openrouter.get("model", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
            or "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
        ).strip()
        # Convenience: allow writing provider/model as one value, e.g.
        # "openai_2/nvidia/nemotron-3-nano-30b-a3b:free".
        if "/" in self.openrouter_provider_id and not self.openrouter_api_key:
            provider_part, model_part = self.openrouter_provider_id.split("/", 1)
            if provider_part:
                self.openrouter_provider_id = provider_part
                if model_part:
                    self.openrouter_model = model_part
        self.openrouter_referer = str(
            openrouter.get("referer", "https://github.com/204343414/astrbot_plugin_VideoSummary")
            or "https://github.com/204343414/astrbot_plugin_VideoSummary"
        ).strip()
        self.openrouter_title = str(openrouter.get("title", "AstrBot VideoSummary") or "AstrBot VideoSummary").strip()
        self.openrouter_youtube_direct_url = bool(openrouter.get("youtube_direct_url", True))
        self.openrouter_non_youtube_base64 = bool(openrouter.get("non_youtube_use_base64", True))
        self.openrouter_max_base64_video_mb = max(float(openrouter.get("max_base64_video_mb", 15)), 1.0)

        self._cleanup_stale_temp()
        logger.info(
            "[VideoSummary] loaded max_duration=%smin max_size=%sMB daily=%s public_domestic_only=%s",
            self.max_duration_minutes,
            self.max_file_size_mb,
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

    def _ydl_opts(self, url: str, *, download: bool, outtmpl: str | None = None) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
        }
        if download:
            opts.update(
                {
                    "outtmpl": outtmpl,
                    "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[ext=mp4]/best",
                    "merge_output_format": "mp4",
                }
            )
        else:
            opts["skip_download"] = True
        return opts

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

        def task():
            with yt_dlp.YoutubeDL(self._ydl_opts(url, download=True, outtmpl=outtmpl)) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = info.get("requested_downloads") or []
                candidates = [x.get("filepath") for x in requested if x.get("filepath")]
                candidates += [info.get("filepath"), ydl.prepare_filename(info)]
                return info, candidates

        info, candidates = await asyncio.to_thread(task)
        files = [Path(x) for x in candidates if x and Path(x).exists()]
        if not files:
            files = sorted(self.temp_dir.glob(f"{task_id}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("下载完成但找不到输出文件")
        path = files[0]
        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > self.max_file_size_mb:
            raise ValueError(f"视频文件 {size_mb:.1f}MB，超过上限 {self.max_file_size_mb:.1f}MB")
        return path, info

    async def _resolve_gemini_provider(self, event: AstrMessageEvent):
        provider = None
        if self.provider_id:
            provider = await self.context.provider_manager.get_provider_by_id(self.provider_id)
        if provider is None:
            try:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            except Exception:
                provider = None
        if provider is None:
            raise RuntimeError("没有可用 Gemini Provider，请在插件配置 gemini.provider_id 或当前会话选择 Gemini Provider")
        api_key = str(getattr(provider, "chosen_api_key", "") or "")
        if not api_key and hasattr(provider, "get_current_key"):
            api_key = str(provider.get_current_key() or "")
        if not api_key:
            keys = getattr(provider, "api_keys", []) or []
            api_key = str(keys[0]) if keys else ""
        model = self.model or str(getattr(provider, "get_model", lambda: "")() or "")
        if not api_key:
            raise RuntimeError("未能从 Gemini Provider 读取 API key")
        if "gemini" not in model.lower():
            raise RuntimeError(f"当前 Provider 模型不像 Gemini: {model}。请配置 gemini.provider_id/model")
        provider_api_base = getattr(provider, "api_base", None) or getattr(provider, "provider_config", {}).get("api_base", None)
        api_base = self.api_base_override or (provider_api_base if self.use_provider_api_base else None)
        timeout = int(getattr(provider, "timeout", 180) or 180)
        return api_key, model, api_base, timeout

    def _has_openrouter_config(self) -> bool:
        return bool(self.openrouter_api_key or self.openrouter_provider_id)

    def _select_backend(self) -> str:
        mode = self.backend_mode
        if mode in {"openrouter", "openrouter_video"}:
            return "openrouter_video"
        if mode in {"gemini", "gemini_files"}:
            return "gemini_files"
        # auto: prefer OpenRouter only when explicitly configured.
        return "openrouter_video" if self._has_openrouter_config() else "gemini_files"

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        try:
            from urllib.parse import urlsplit

            host = urlsplit(url).netloc.lower()
        except Exception:
            return False
        return "youtube.com" in host or "youtu.be" in host

    async def _resolve_openrouter_config(self, event: AstrMessageEvent) -> tuple[str, str, str, int, dict]:
        provider = None
        if self.openrouter_provider_id:
            provider = await self.context.provider_manager.get_provider_by_id(self.openrouter_provider_id)
            if provider is None:
                raise RuntimeError(f"未找到 OpenRouter/OpenAI Provider: {self.openrouter_provider_id}")

        api_key = self.openrouter_api_key
        base_url = self.openrouter_base_url
        model = self.openrouter_model
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
            if not base_url or base_url == "https://openrouter.ai/api/v1":
                # If the provider is already an OpenRouter/OpenAI-compatible
                # AstrBot provider, reuse its base_url.
                client = getattr(provider, "client", None)
                client_base = str(getattr(client, "base_url", "") or "").rstrip("/")
                base_url = client_base or str(provider_config.get("api_base", "") or base_url).rstrip("/")
            timeout = int(getattr(provider, "timeout", provider_config.get("timeout", 120)) or 120)
            custom_headers = dict(getattr(provider, "custom_headers", None) or provider_config.get("custom_headers", {}) or {})

        if not api_key:
            raise RuntimeError("OpenRouter API key 未配置；请填写 openrouter.api_key 或 openrouter.provider_id。")
        if not model:
            raise RuntimeError("OpenRouter model 未配置。")
        base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        return api_key, base_url, model, timeout, custom_headers

    async def _openrouter_chat(self, api_key: str, base_url: str, model: str, timeout_seconds: int, messages: list, custom_headers: dict | None = None) -> str:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.openrouter_referer,
            "X-Title": self.openrouter_title,
        }
        if custom_headers:
            headers.update({str(k): str(v) for k, v in custom_headers.items()})
        payload = {"model": model, "messages": messages}
        timeout = aiohttp.ClientTimeout(total=max(timeout_seconds, 30), connect=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {"raw": raw}
                if resp.status >= 400:
                    err = data.get("error") if isinstance(data, dict) else None
                    msg = err.get("message") if isinstance(err, dict) else raw
                    raise RuntimeError(f"OpenRouter HTTP {resp.status}: {str(msg)[:300]}")
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    raise RuntimeError(f"OpenRouter 未返回 choices: {str(data)[:300]}")
                content = choices[0].get("message", {}).get("content", "")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict):
                            parts.append(str(item.get("text") or item.get("content") or ""))
                    return "\n".join(x for x in parts if x).strip()
                return str(content).strip()

    def _video_data_url(self, video_path: Path) -> str:
        size_mb = video_path.stat().st_size / 1024 / 1024
        if size_mb > self.openrouter_max_base64_video_mb:
            raise ValueError(
                f"OpenRouter base64 视频上限 {self.openrouter_max_base64_video_mb:.1f}MB，当前 {size_mb:.1f}MB"
            )
        encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
        return f"data:video/mp4;base64,{encoded}"

    async def _analyze_with_openrouter(
        self,
        event: AstrMessageEvent,
        url: str,
        video_path: Path | None,
        user_question: str,
        user_id: str,
    ) -> tuple[str, int]:
        api_key, base_url, model, timeout, custom_headers = await self._resolve_openrouter_config(event)
        if self.openrouter_youtube_direct_url and self._is_youtube_url(url):
            video_ref = url
        elif video_path is not None and self.openrouter_non_youtube_base64:
            video_ref = self._video_data_url(video_path)
        else:
            raise RuntimeError("当前 OpenRouter 配置无法为该 URL 构造视频输入。")
        # Charge after we have a valid video reference for OpenRouter.
        used = self._charge_usage(user_id)
        prompt = self._build_prompt(user_question)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": video_ref}},
                ],
            }
        ]
        text = await self._openrouter_chat(api_key, base_url, model, timeout, messages, custom_headers)
        if not text:
            raise RuntimeError("OpenRouter 未返回文本结果")
        return text, used

    async def _upload_and_wait_file(self, client, video_path: Path):
        try:
            uploaded = await asyncio.to_thread(client.files.upload, file=str(video_path))
        except KeyError as exc:
            raise RuntimeError(
                "Gemini Files API 上传初始化失败：当前 api_base/代理可能不支持 /upload/v1beta/files。"
                "请优先留空 gemini.api_base，并关闭 use_provider_api_base；若必须走代理，请确认代理支持 Gemini Files API。"
            ) from exc
        name = getattr(uploaded, "name", "")
        deadline = time.monotonic() + self.file_poll_timeout_seconds
        while True:
            state = str(getattr(uploaded, "state", "") or "").upper()
            if state.endswith("ACTIVE") or state == "ACTIVE":
                return uploaded
            if state.endswith("FAILED") or state == "FAILED":
                raise RuntimeError("Gemini 文件处理失败")
            if time.monotonic() >= deadline:
                raise TimeoutError("等待 Gemini 处理视频超时")
            await asyncio.sleep(self.file_poll_interval_seconds)
            if not name:
                raise RuntimeError("Gemini 文件上传未返回 name，无法轮询状态")
            uploaded = await asyncio.to_thread(client.files.get, name=name)

    def _build_prompt(self, user_question: str) -> str:
        task = str(user_question or "").strip() or self.default_task_prompt
        return f"{task}\n{self.tail_instruction_prompt}"

    async def _analyze_with_gemini(
        self,
        event: AstrMessageEvent,
        video_path: Path,
        user_question: str,
        user_id: str,
    ) -> tuple[str, int]:
        api_key, model, api_base, timeout = await self._resolve_gemini_provider(event)
        http_options = types.HttpOptions(base_url=api_base, timeout=timeout * 1000)
        client = genai.Client(api_key=api_key, http_options=http_options)
        uploaded = await self._upload_and_wait_file(client, video_path)
        # Charge only after the video is accepted and processed by Gemini.
        # Safety refusals generated by Gemini still consume quota.
        used = self._charge_usage(user_id)
        prompt = self._build_prompt(user_question)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[uploaded, prompt],
        )
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise RuntimeError("Gemini 未返回文本结果")
        return text, used

    @staticmethod
    def _strip_command_prefix(text: str) -> str:
        text = str(text or "").strip()
        # Works for raw strings like "/视频分析 ..." or after wake-prefix removal.
        for prefix in ("/视频分析", "视频分析"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text

    def _split_question_and_url(self, text: str) -> tuple[str, str]:
        text = self._strip_command_prefix(text)
        url = self._extract_first_url(text)
        question = str(text or "")
        if url:
            # Remove both raw URL and markdown link forms that point to it.
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
</style></head><body><div class="card">
<h1>{self._escape(title or self.card_title)}</h1>
<div class="badge">{status}</div>
<div class="disclaimer">{self._escape(self.disclaimer)}</div>
<div class="content">{self._escape(body)}</div>
<div class="url">来源：{self._escape(url)}</div>
</div></body></html>
"""
        if html_renderer is None:
            raise RuntimeError("AstrBot T2I 渲染器不可用")
        rendered = await html_renderer.render_custom_template(
            html_doc,
            {},
            return_url=False,
            options={"full_page": True, "type": "jpeg", "quality": 85},
        )
        if isinstance(rendered, bytes):
            out = self.temp_dir / f"summary_{int(time.time() * 1000)}.jpg"
            out.write_bytes(rendered)
            return str(out)
        return str(rendered)

    def _looks_refusal(self, text: str) -> bool:
        lowered = text.lower()
        return "safe=false" in lowered or "safe: false" in lowered or "不适合总结" in text or "已停止" in text

    @filter.command("视频分析")
    async def video_analyze(self, event: AstrMessageEvent, text: GreedyStr):
        """分析一个视频 URL。用法：/视频分析 你的问题 https://..."""
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
            try:
                raw_text = str(event.get_message_str() or getattr(event, "message_str", "") or "")
            except Exception:
                raw_text = str(getattr(event, "message_str", "") or "")
            question, url = self._split_question_and_url(raw_text)
        if not url:
            yield event.plain_result("请提供包含 http/https 的视频链接。")
            return

        async with self._semaphore:
            task_id = str(int(time.time() * 1000))
            video_path: Path | None = None
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

                backend = self._select_backend()
                downloaded_info = info
                if backend == "openrouter_video" and self.openrouter_youtube_direct_url and self._is_youtube_url(url):
                    analysis, used = await self._analyze_with_openrouter(
                        event, url, None, question, user_id
                    )
                else:
                    video_path, downloaded_info = await self._download_video(url, task_id)
                    if backend == "openrouter_video":
                        analysis, used = await self._analyze_with_openrouter(
                            event, url, video_path, question, user_id
                        )
                    else:
                        analysis, used = await self._analyze_with_gemini(
                            event, video_path, question, user_id
                        )
                title = str(downloaded_info.get("title") or info.get("title") or self.card_title)
                image = await self._render_card(title, analysis, url, safe=not self._looks_refusal(analysis))
                yield event.image_result(image)
                logger.info("[VideoSummary] success user=%s used=%s url=%s", user_id, used, url)
            except Exception as exc:
                logger.exception("[VideoSummary] failed")
                yield event.plain_result(f"视频分析失败：{type(exc).__name__}: {exc}")
            finally:
                if video_path:
                    prefix = video_path.name.split("_", 1)[0]
                for path in self.temp_dir.glob(f"{task_id}_*"):
                    try:
                        path.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or "")
        if not value:
            return "<empty>"
        if len(value) <= 8:
            return value[:2] + "***"
        return value[:4] + "..." + value[-4:]

    @staticmethod
    def _short_error(exc: BaseException, limit: int = 240) -> str:
        text = str(exc) or type(exc).__name__
        text = re.sub(r"[A-Za-z0-9_\-]{24,}", lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)
        return f"{type(exc).__name__}: {text[:limit]}"

    def _make_silent_wav(self) -> Path:
        path = self.temp_dir / f"stt_probe_{int(time.time() * 1000)}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)
        return path

    async def _probe_openai_stt(self) -> str:
        if not self.stt_enabled:
            return "SKIP：stt.enabled=false"
        if not self.stt_api_base_url or not self.stt_api_key:
            return "SKIP：stt.api_base_url 或 stt.api_key 未配置"
        wav = self._make_silent_wav()
        try:
            form = aiohttp.FormData()
            form.add_field("model", self.stt_model)
            form.add_field(
                "file",
                wav.read_bytes(),
                filename="stt_probe.wav",
                content_type="audio/wav",
            )
            timeout = aiohttp.ClientTimeout(total=45, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(
                    f"{self.stt_api_base_url}/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.stt_api_key}"},
                    data=form,
                ) as resp:
                    body = await resp.text()
                    if resp.status < 400:
                        return f"OK：HTTP {resp.status}，返回 {body[:120]}"
                    return f"FAIL：HTTP {resp.status}，返回 {body[:180]}"
        finally:
            try:
                wav.unlink()
            except OSError:
                pass

    async def _probe_gemini_text(self, client, model: str) -> str:
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents="请只回复 PONG",
            )
            text = str(getattr(resp, "text", "") or "").strip()
            return f"OK：{text[:80] or '<empty>'}"
        except Exception as exc:
            return "FAIL：" + self._short_error(exc)

    async def _probe_gemini_file_api(self, client) -> str:
        probe = self.temp_dir / f"gemini_file_probe_{int(time.time() * 1000)}.txt"
        probe.write_text("hello", encoding="utf-8")
        try:
            uploaded = await self._upload_and_wait_file(client, probe)
            name = str(getattr(uploaded, "name", "") or "")
            uri = str(getattr(uploaded, "uri", "") or "")
            return f"OK：name={name or '<none>'} uri={uri[:48] or '<none>'}"
        except Exception as exc:
            return "FAIL：" + self._short_error(exc)
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    async def _probe_openrouter_text(self, event: AstrMessageEvent) -> str:
        try:
            api_key, base_url, model, timeout, custom_headers = await self._resolve_openrouter_config(event)
            messages = [{"role": "user", "content": "请只回复 PONG"}]
            text = await self._openrouter_chat(api_key, base_url, model, timeout, messages, custom_headers)
            return f"OK：model={model} key={self._mask_secret(api_key)} base={base_url} resp={text[:80] or '<empty>'}"
        except Exception as exc:
            return "FAIL：" + self._short_error(exc)

    async def _probe_openrouter_video_url(self, event: AstrMessageEvent, url: str) -> str:
        if not url:
            return "SKIP：未提供 URL"
        if not self._is_youtube_url(url):
            return "SKIP：非 YouTube URL；OpenRouter 直接 URL 支持取决于模型/Provider，非 YouTube 通常需下载后 base64。"
        try:
            api_key, base_url, model, timeout, custom_headers = await self._resolve_openrouter_config(event)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请用中文用一句话描述这个视频。"},
                        {"type": "video_url", "video_url": {"url": url}},
                    ],
                }
            ]
            text = await self._openrouter_chat(api_key, base_url, model, timeout, messages, custom_headers)
            return f"OK：{text[:160] or '<empty>'}"
        except Exception as exc:
            return "FAIL：" + self._short_error(exc)

    @filter.command("视频分析自检")
    async def diagnostics(self, event: AstrMessageEvent, text: GreedyStr = ""):
        """自检 Gemini Files API、字幕信息、STT 中转站等链路。"""
        event.stop_event()
        lines: list[str] = ["🧪 视频分析插件自检"]
        raw = str(text or "")
        _question, url = self._split_question_and_url(raw)
        if not url:
            try:
                _question, url = self._split_question_and_url(event.get_message_str())
            except Exception:
                pass
        lines.append(f"URL：{url or '未提供'}")

        if self.backend_mode in {"gemini", "gemini_files"} or self.provider_id:
            try:
                api_key, model, api_base, timeout = await self._resolve_gemini_provider(event)
                lines.append(f"Gemini Provider：OK model={model} key={self._mask_secret(api_key)}")
                lines.append(f"Gemini api_base：{api_base or 'Google 官方默认'} timeout={timeout}s")
                client = genai.Client(api_key=api_key, http_options=types.HttpOptions(base_url=api_base, timeout=timeout * 1000))
                lines.append("Gemini 文本生成：" + await self._probe_gemini_text(client, model))
                lines.append("Gemini Files API：" + await self._probe_gemini_file_api(client))
            except Exception as exc:
                lines.append("Gemini Provider：FAIL " + self._short_error(exc))
        else:
            lines.append("Gemini：SKIP：当前后端为 OpenRouter，未配置 gemini.provider_id")

        if self._has_openrouter_config():
            lines.append("OpenRouter 文本：" + await self._probe_openrouter_text(event))
            lines.append("OpenRouter 视频URL：" + await self._probe_openrouter_video_url(event, url))
        else:
            lines.append("OpenRouter：SKIP：未配置 openrouter.api_key 或 openrouter.provider_id")

        if url:
            try:
                info = await self._extract_info(url)
                duration = float(info.get("duration") or 0)
                domestic = self._is_domestic_url_or_info(url, info)
                subs = sorted((info.get("subtitles") or {}).keys())[:12]
                auto_subs = sorted((info.get("automatic_captions") or {}).keys())[:12]
                lines.append(
                    f"yt-dlp 元信息：OK extractor={info.get('extractor') or info.get('extractor_key')} duration={duration/60:.1f}min domestic={domestic}"
                )
                lines.append(f"字幕：manual={subs or '无'} auto={auto_subs or '无'}")
                if duration and duration > self.max_duration_minutes * 60:
                    lines.append(f"限制：WARN 视频超过 max_duration_minutes={self.max_duration_minutes}")
                else:
                    lines.append("限制：OK 时长未超限或未知")
            except Exception as exc:
                lines.append("yt-dlp 元信息：FAIL " + self._short_error(exc))

        lines.append("OpenAI 兼容 STT：" + await self._probe_openai_stt())
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        self._save_usage()
