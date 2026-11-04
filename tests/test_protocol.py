"""Self-contained tests for the v1.2 schema and the mock's validation rules."""
import base64
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "mock_orbtile_rules", ROOT / "examples" / "mock-orbtile.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)

PNG_HEADER = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
              + (16).to_bytes(4, "big") + (16).to_bytes(4, "big"))
PNG_BASE64 = base64.b64encode(PNG_HEADER).decode("ascii")


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "protocol" / "adapter-protocol-v1.schema.json").read_text())
        cls.defs = cls.schema["$defs"]

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

    def test_schema_v12_icon_and_nullable_registration_field(self):
        self.assertEqual(self.schema["title"], "Orbtile Adapter Protocol v1.2")
        icon = self.defs["Icon"]
        png = icon["properties"]["png"]
        self.assertEqual(png["minLength"], 1)
        self.assertEqual(png["maxLength"], 43696)
        self.assertEqual(png["contentEncoding"], "base64")
        self.assertEqual(png["pattern"],
                         "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
        bundle = icon["properties"]["appBundleId"]
        self.assertEqual((bundle["minLength"], bundle["maxLength"]), (3, 128))
        self.assertEqual(bundle["pattern"], "^[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$")
        self.assertEqual(icon["anyOf"], [{"required": ["png"]}, {"required": ["appBundleId"]}])
        registration_icon = self.defs["AdapterRegistration"]["properties"]["icon"]
        self.assertIn({"type": "null"}, registration_icon["oneOf"])
        self.assertIn({"$ref": "#/$defs/Icon"}, registration_icon["oneOf"])

    def test_schema_v12_state_colours_and_inheritance_contract(self):
        states = self.defs["StateColours"]
        editable = {"idle", "starting", "thinking", "tool", "streaming", "done",
                    "compacting", "offline", "waitingPermission", "waitingQuestion"}
        self.assertEqual(set(states["propertyNames"]["enum"]), editable)
        self.assertEqual((states["minProperties"], states["maxProperties"]), (1, 10))
        self.assertEqual(states["additionalProperties"]["pattern"], "^#[0-9A-Fa-f]{6}$")
        self.assertNotIn("error", editable)
        self.assertNotIn("empty", editable)
        self.assertIn("thinking supplies omitted tool and streaming colours", states["description"])
        self.assertIn("waitingPermission supplies omitted waitingQuestion", states["description"])
        self.assertIn("anchorAttention ignores both waiting overrides", states["description"])
        palette_states = self.defs["Palette"]["properties"]["states"]
        self.assertIn({"$ref": "#/$defs/StateColours"}, palette_states["oneOf"])
        self.assertIn({"type": "null"}, palette_states["oneOf"])

    def test_schema_project_and_v11_event_extensions(self):
        update = self.defs["SessionUpdate"]["properties"]
        project = update["project"]
        self.assertEqual(set(project["type"]), {"string", "null"})
        self.assertEqual((project["minLength"], project["maxLength"]), (1, 128))
        self.assertIn("Orbtile 0.2.17+", project["description"])
        registration = self.defs["AdapterRegistration"]
        capabilities = registration["properties"]["capabilities"]["items"]["enum"]
        self.assertEqual(capabilities, ["tap", "approvals", "questions"])
        self.assertEqual(registration["properties"]["tapBehavior"]["enum"], ["alsoOpen", "eventOnly"])
        poll = self.defs["EventsPoll"]["properties"]
        self.assertEqual(poll["waitSeconds"]["maximum"], 2)
        self.assertEqual(poll["after"]["minimum"], 0)
        self.assertEqual(poll["ack"]["minimum"], 0)
        reply = self.defs["EventsReply"]
        self.assertEqual(reply["required"], ["events", "lastSequence", "acknowledged"])
        self.assertEqual(reply["properties"]["events"]["maxItems"], 128)
        self.assertEqual(self.defs["ApprovalEvent"]["properties"]["decision"]["enum"],
                         ["allow", "deny"])
        self.assertIn("question", update)
        self.assertIn("approval", update)

    def test_schema_api_minor_and_additive_error_extensions(self):
        info = self.defs["Info"]["properties"]
        self.assertEqual(info["api"]["const"], 1)
        self.assertEqual(info["apiMinor"]["minimum"], 0)
        self.assertIn("v1.2", info["apiMinor"]["description"])
        self.assertIn("icon and palette.states", info["apiMinor"]["description"])
        codes = self.defs["Error"]["properties"]["error"]["properties"]["code"]["enum"]
        for code in ("invalid_cursor", "events_not_supported", "poll_pending"):
            self.assertIn(code, codes)

    def test_protocol_documents_v12_discovery_and_every_activity_key_text(self):
        text = " ".join((ROOT / "protocol" / "README.md").read_text().split())
        for phrase in ("Orbtile Adapter Protocol v1.2", "Orbtile Enterprise/adapter-api.json",
                       "%USERPROFILE%\\.orbtile\\adapter-api.json",
                       "%LOCALAPPDATA%\\Orbtile\\Windows\\adapter-api.json",
                       "loopback TCP on Windows", "`project` (optional, Orbtile 0.2.17+)",
                       "While `streaming`", "While `tool`", "`done` shows Done",
                       "`starting` shows the model", "`thinking` and `compacting` show `statusDetail`",
                       "`waitingPermission` shows", "`waitingQuestion` shows Question",
                       "`error` shows `statusDetail`", "`idle` and `offline` show the status only"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_mock_accepts_and_normalizes_v12_registration_fields(self):
        body = {"icon": {"png": PNG_BASE64, "appBundleId": "com.example.agent"},
                "palette": {"states": {"thinking": "#EAB308", "waitingPermission": "#EF4444"}}}
        self.assertIsNone(mock.check_icon(body))
        self.assertIsNone(mock.check_state_colours(body["palette"]))
        self.assertEqual(body["palette"]["states"], {"thinking": "#eab308",
                                                     "waitingPermission": "#ef4444"})
        self.assertIsNone(mock.check_icon({"icon": None}))
        self.assertIsNone(mock.check_state_colours({"states": None}))

    def test_mock_rejects_invalid_icons(self):
        rectangular = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                       + (16).to_bytes(4, "big") + (32).to_bytes(4, "big"))
        invalid = ({"icon": {}}, {"icon": {"unknown": "x"}}, {"icon": {"png": ""}},
                   {"icon": {"png": "not base64"}}, {"icon": {"png": "AAAA"}},
                   {"icon": {"png": base64.b64encode(rectangular).decode("ascii")}},
                   {"icon": {"appBundleId": "no-dot"}},
                   {"icon": {"appBundleId": "com..example"}},
                   {"icon": {"appBundleId": "/tmp/example"}})
        for body in invalid:
            with self.subTest(body=body):
                error = mock.check_icon(body)
                self.assertEqual(error[0], 422)
                self.assertEqual(error[1], "invalid_field")
                self.assertTrue(error[3].startswith("icon"))

    def test_mock_enforces_icon_size_limits(self):
        too_long = mock.check_icon({"icon": {"png": "A" * 43697}})
        self.assertEqual(too_long[:2], (422, "limit_exceeded"))
        self.assertEqual(too_long[3], "icon.png")
        oversized_png = PNG_HEADER + bytes(32769 - len(PNG_HEADER))
        encoded = base64.b64encode(oversized_png).decode("ascii")
        self.assertLessEqual(len(encoded), 43696)
        too_large = mock.check_icon({"icon": {"png": encoded}})
        self.assertEqual(too_large[:2], (422, "limit_exceeded"))
        self.assertEqual(too_large[3], "icon.png")

    def test_mock_rejects_invalid_state_colours(self):
        invalid = ([], {}, {"error": "#ff0000"}, {"empty": "#000000"},
                   {"unknown": "#aabbcc"}, {"thinking": "red"}, {"thinking": None},
                   {str(i): "#aabbcc" for i in range(11)})
        for states in invalid:
            with self.subTest(states=states):
                error = mock.check_state_colours({"states": states})
                self.assertEqual(error[0], 422)
                self.assertEqual(error[1], "invalid_field")
                self.assertTrue(error[3].startswith("palette.states"))


if __name__ == "__main__":
    unittest.main()
