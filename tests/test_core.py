import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def load_module():
    for name in list(sys.modules):
        if name == "videosummary_under_test" or name.startswith("astrbot"):
            sys.modules.pop(name, None)

    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api.AstrBotConfig = dict
    class Logger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
        def exception(self, *args, **kwargs): pass
    astrbot_api.logger = Logger()

    event_mod = types.ModuleType("astrbot.api.event")
    class AstrMessageEvent: pass
    class Filter:
        @staticmethod
        def command(_name):
            def deco(func): return func
            return deco
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = Filter

    star_mod = types.ModuleType("astrbot.api.star")
    class Context: pass
    class Star:
        def __init__(self, context=None): self.context = context
    class StarTools:
        @staticmethod
        def get_data_dir(name): return tempfile.mkdtemp()
    def register(*args, **kwargs):
        def deco(cls): return cls
        return deco
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = register

    cmd_mod = types.ModuleType("astrbot.core.star.filter.command")
    cmd_mod.GreedyStr = str
    core_mod = types.ModuleType("astrbot.core")
    core_mod.html_renderer = None

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.star"] = types.ModuleType("astrbot.core.star")
    sys.modules["astrbot.core.star.filter"] = types.ModuleType("astrbot.core.star.filter")
    sys.modules["astrbot.core.star.filter.command"] = cmd_mod

    spec = importlib.util.spec_from_file_location("videosummary_under_test", Path(__file__).resolve().parents[1] / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class VideoSummaryCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_extract_first_url_markdown_share(self):
        text = "标题 ++[https://b23.tv/abc](https://b23.tv/abc)++"
        self.assertEqual(self.mod.VideoSummaryPlugin._extract_first_url(text), "https://b23.tv/abc")

    def test_split_question_and_url(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        question, url = plugin._split_question_and_url("帮我看重点 https://www.bilibili.com/video/BV1xx/?spm_id_from=1")
        self.assertEqual(question, "帮我看重点")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1xx/")

    def test_parse_values(self):
        self.assertEqual(self.mod.VideoSummaryPlugin._parse_values("a,b，c\nd"), {"a", "b", "c", "d"})


    def test_split_question_and_url_from_raw_command_with_question(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        raw = "/视频分析 这个视频内容 [https://www.bilibili.com/video/BV1kW411d7cD?spm_id_from=1](https://www.bilibili.com/video/BV1kW411d7cD?spm_id_from=1)"
        question, url = plugin._split_question_and_url(raw)
        self.assertEqual(question, "这个视频内容")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1kW411d7cD/")
    def test_download_format_prefers_progressive_then_split_streams(self):
        selectors = self.mod.VideoSummaryPlugin._download_format_selectors()
        self.assertIn("acodec!=none", selectors[0])
        self.assertTrue(any("+bestaudio" in item for item in selectors[1:]))

    def test_escaped_cookie_text_supported(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        plugin.data_dir = Path(tempfile.mkdtemp())
        plugin.cookie_dir = plugin.data_dir / "cookies"
        plugin.cookie_dir.mkdir(parents=True, exist_ok=True)
        plugin.bilibili_cookies = "# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc"
        plugin.douyin_cookies = plugin.youtube_cookies = plugin.generic_cookies = ""
        cookiefile, header = plugin._resolve_cookie_for_ytdlp("https://b23.tv/abc")
        self.assertIsNone(header)
        self.assertIn("SESSDATA", Path(cookiefile).read_text())

    def test_sanitize_markdown_polluted_cookie_domain(self):
        dirty = "….[bilibili.com](http://bilibili.com)\tTRUE\t/\tFALSE\t0\tb_lsid\tabc"
        clean = self.mod.VideoSummaryPlugin._sanitize_cookie_text(dirty)
        self.assertEqual(clean, ".bilibili.com\tTRUE\t/\tFALSE\t0\tb_lsid\tabc")

    def test_select_progressive_media_url_prefers_small_mp4_with_audio(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        info = {"formats": [
            {"url": "https://cdn/high.mp4", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4", "height": 720, "filesize": 1000},
            {"url": "https://cdn/low.mp4", "vcodec": "avc1", "acodec": "mp4a", "ext": "mp4", "height": 144, "filesize": 500},
            {"url": "https://cdn/videoonly.mp4", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 144, "filesize": 100},
        ]}
        url, fmt = plugin._select_progressive_media_url(info)
        self.assertEqual(url, "https://cdn/low.mp4")
        self.assertEqual(fmt["height"], 144)


    def test_base64_limit_matches_dashscope_10mb(self):
        # 查档：百炼多模态本地文件 Base64 编码后必须 < 10MB
        self.assertEqual(self.mod.QWEN_BASE64_LIMIT_BYTES, 10 * 1024 * 1024)
        self.assertLess(self.mod.QWEN_RAW_BASE64_SAFE_BYTES, self.mod.QWEN_BASE64_LIMIT_BYTES * 3 / 4)

    def test_qwen_video_data_url_rejects_oversized_file(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        plugin.qwen_base64_prefix = "data:;base64,"
        big = Path(tempfile.mkdtemp()) / "big.mp4"
        with big.open("wb") as handle:
            handle.write(b"\0" * (self.mod.QWEN_RAW_BASE64_SAFE_BYTES + 1))
        with self.assertRaises(RuntimeError) as ctx:
            plugin._qwen_video_data_url(big)
        self.assertIn("10MB", str(ctx.exception))

    def test_qwen_video_data_url_accepts_small_file(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        plugin.qwen_base64_prefix = "data:;base64,"
        small = Path(tempfile.mkdtemp()) / "small.mp4"
        small.write_bytes(b"hello world")
        self.assertTrue(plugin._qwen_video_data_url(small).startswith("data:;base64,"))

    def test_dashscope_upload_endpoint_selection(self):
        plugin = self.mod.VideoSummaryPlugin.__new__(self.mod.VideoSummaryPlugin)
        plugin.temp_oss_endpoint = "auto"
        self.assertIn("dashscope.aliyuncs.com", plugin._dashscope_upload_url("https://dashscope.aliyuncs.com/compatible-mode/v1"))
        self.assertIn("dashscope-intl", plugin._dashscope_upload_url("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"))
        plugin.temp_oss_endpoint = "cn"
        self.assertIn("//dashscope.aliyuncs.com", plugin._dashscope_upload_url("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"))

    def test_duration_cap_converges_to_official_limit(self):
        self.assertEqual(self.mod.QWEN_MAX_DURATION_MINUTES, 60.0)


if __name__ == "__main__":
    unittest.main()

