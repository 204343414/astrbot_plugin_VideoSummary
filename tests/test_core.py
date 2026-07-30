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

if __name__ == "__main__":
    unittest.main()
