"""Self-contained tests for the mock's palette and URL validation rules."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "mock_orbtile_rules", Path(__file__).resolve().parents[1] / "examples" / "mock-orbtile.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class ProtocolTests(unittest.TestCase):
    def test_url_scheme_registration(self):
        for schemes in (["file"], ["javascript"], ["data"], ["orbtile"],
                        ["x-apple.systempreferences"], ["Bad"], ["a", "a"],
                        ["a", "b", "c", "d", "e"], "https", [None]):
            with self.subTest(schemes=schemes):
                error = mock.check_url_schemes({"urlSchemes": schemes})
                self.assertEqual(error[0], 422)
                self.assertEqual(error[3], "urlSchemes")
        for body in ({}, {"urlSchemes": []}, {"urlSchemes": ["https", "myagent"]}):
            self.assertIsNone(mock.check_url_schemes(body))

    def test_unsafe_or_undeclared_links(self):
        for url in ("file:///example", "javascript:alert(1)", "data:text/html,x",
                    "orbtile://x", "x-apple.systempreferences:x", "https://example.com",
                    "myagent2://x", "no-scheme", "", 42):
            with self.subTest(url=url):
                error = mock.check_open_url({"openURL": url}, "sessions[0].", ["myagent"])
                self.assertEqual(error[0], 422)
                self.assertEqual(error[3], "sessions[0].openURL")

    def test_valid_links_and_null(self):
        for body in ({}, {"openURL": None}, {"openURL": "myagent://run/1"},
                     {"openURL": "MyAgent://run/2"}):
            self.assertIsNone(mock.check_open_url(body, "", ["myagent"]))
        self.assertEqual(mock.check_open_url({"openURL": "myagent://x"}, "", [])[0], 422)

    def test_reserved_working_hue_arc(self):
        self.assertAlmostEqual(mock.resolved_hue(30), 345.9)
        self.assertAlmostEqual(mock.resolved_hue(40), 75.9)
        for hue in (150, 200, 320):
            self.assertEqual(mock.resolved_hue(hue), hue)


if __name__ == "__main__":
    unittest.main()
