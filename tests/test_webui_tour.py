import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "webui" / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (ROOT / "webui" / "static" / "style.css").read_text(encoding="utf-8")


class WebUiTourTests(unittest.TestCase):
    def test_tour_can_start_and_be_reopened(self):
        self.assertIn('id="btn-guide"', HTML)
        self.assertIn('id="tour-layer"', HTML)
        self.assertIn("$('#btn-guide').onclick = ()=>startGuide(0)", APP)
        self.assertIn("if(!guideStorageCompleted())", APP)
        self.assertIn("localStorage.setItem(GUIDE_STORAGE_KEY, 'complete')", APP)

    def test_tour_covers_required_beginner_workflow(self):
        for step_id in (
            "network",
            "clash",
            "residential",
            "platform-proxy",
            "network-test",
            "env-overview",
            "browser",
            "outlook-recovery",
            "env-test",
            "asset-scan",
            "asset-call",
            "chatgpt-email",
            "temp-email",
            "sms",
            "task",
            "platforms",
            "task-options",
            "run",
            "logs",
        ):
            self.assertIn(f"id:'{step_id}'", APP)
        self.assertIn("Clash API 配置方法", APP)
        self.assertIn("External Controller", APP)
        self.assertIn("控制密码", APP)
        self.assertIn("住宅代理填写方法", APP)
        self.assertIn("继承全局", APP)
        self.assertIn("提取 RT 前配置辅助邮箱", APP)
        self.assertIn("同一平台账号即使切换输出格式也不会再次返回", APP)
        self.assertIn("Plus 免费试用资格", APP)
        self.assertIn("每次输出前都会在线校验", APP)
        self.assertIn("跳过网络配置", APP)
        self.assertIn("跳过浏览器配置", APP)
        self.assertIn("跳过邮箱接码", APP)
        self.assertIn("跳过短信接码", APP)

    def test_dynamic_controls_have_stable_tour_targets(self):
        self.assertIn("f.dataset.argFlag = a.flag", APP)
        self.assertIn("box.dataset.guideGroup = 'browser'", APP)
        self.assertIn("box.dataset.guideGroup = 'outlook-recovery'", APP)
        self.assertIn("box.dataset.guideGroup = 'chatgpt-email'", APP)
        self.assertIn("box.dataset.guideGroup = 'temp-email'", APP)
        self.assertIn("box.dataset.guideGroup = 'sms'", APP)
        self.assertIn('data-guide="platform-proxy-overrides"', HTML)
        self.assertIn('data-guide="asset-scan"', HTML)
        self.assertIn('data-guide="asset-call"', HTML)
        self.assertIn("previewGuideProxyMode('residential')", APP)
        self.assertIn("restoreGuideProxyMode", APP)

    def test_tour_has_responsive_spotlight_and_plain_text(self):
        self.assertIn(".tour-spotlight", STYLE)
        self.assertIn(".tour-curtain", STYLE)
        self.assertIn("@media (max-width:600px)", STYLE)
        start = APP.index("const GUIDE_STEPS")
        end = APP.index("function guideStorageCompleted")
        guide_copy = APP[start:end]
        self.assertNotIn("？", guide_copy)
        self.assertNotIn("\ufffd", HTML + APP + STYLE)


if __name__ == "__main__":
    unittest.main()
