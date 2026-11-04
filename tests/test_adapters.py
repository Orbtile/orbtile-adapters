"""Standalone reference-client tests against the included local fake-server handler."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock as mocklib

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock = load_module("mock_orbtile", EXAMPLES / "mock-orbtile.py")
ref = load_module("ref", EXAMPLES / "ref.py")
# Requests are recorded below rather than printed with potentially private session fields.
mock.print = lambda *args, **kwargs: None


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for executable in ("bash", "curl", "plutil", "iconv", "node"):
            if shutil.which(executable) is None:
                raise RuntimeError("Required executable missing: " + executable)
        if sys.platform != "darwin":
            raise RuntimeError("The shell reference requires macOS plutil")

    def setUp(self):
        self.previous_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory(prefix=".adapter-tests-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, self.previous_cwd)
        os.chdir(self.temp.name)
        self.discovery = Path("adapter-api.json").resolve()
        self.records = []
        self.rotate_after = None
        self.corrupt_proof = False
        mock.TOKEN = secrets.token_urlsafe(32)
        mock.adapters.clear()
        mock.sessions.clear()
        mock.EVENTS = mock.EventState()
        mock.DEMO_EVENTS = False
        owner = self

        class RecordingHandler(mock.Handler):
            def reply(self, status, body=None, headers=None):
                if owner.corrupt_proof and body and "proof" in body:
                    body["proof"] = "0" * 64
                self.status = status
                super().reply(status, body, headers)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                self.rfile = io.BytesIO(raw)
                mock.Handler.handle_any(self)
                body = json.loads(raw) if raw else {}
                owner.records.append((self.command, self.path, body, self.status,
                                      copy.deepcopy(mock.sessions)))
                if owner.rotate_after and owner.rotate_after(self.command, self.path):
                    owner.rotate_after = None
                    owner.rotate()

            do_GET = do_PUT = do_POST = do_DELETE = handle_any

        # A relative socket keeps every generated file inside the test directory,
        # even when the repository itself has a long filesystem path.
        self.server = socketserver.UnixStreamServer("mock.sock", RecordingHandler)
        os.chmod("mock.sock", 0o600)
        self.write_discovery()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.env = dict(os.environ, ORBTILE_API_FILE=str(self.discovery),
                        PYTHONDONTWRITEBYTECODE="1", ORBTILE_HEARTBEAT_SECONDS="0.05")
        ref.API_FILE = str(self.discovery)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def write_discovery(self):
        self.discovery.write_text(json.dumps({"api": 1, "socketPath": "mock.sock",
                                              "basePath": "/a/v1", "token": mock.TOKEN,
                                              "pid": os.getpid()}))
        self.discovery.chmod(0o600)

    def rotate(self):
        mock.TOKEN = secrets.token_urlsafe(32)
        mock.adapters.clear()
        mock.sessions.clear()
        mock.EVENTS = mock.EventState()
        self.write_discovery()

    def client(self):
        client = ref.Orbtile()
        client.verify()
        client.register()
        return client

    def run_client(self, args, env=None):
        return subprocess.run(args, env=env or self.env, capture_output=True,
                              text=True, timeout=25)

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.assertTrue(condition(), "Fake-server request did not arrive")

    def test_python_v12_info_and_registration(self):
        client = self.client()
        status, info, _ = client.call("GET", "/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["api"], 1)
        self.assertEqual(info["apiMinor"], 2)
        registration = mock.adapters[ref.ADAPTER]
        self.assertEqual(registration["icon"], {"appBundleId": "com.apple.Terminal"})
        self.assertEqual(registration["palette"]["states"], {
            "thinking": "#eab308", "waitingPermission": "#ef4444",
            "done": "#22c55e", "idle": "#6b7280"})

    def test_mock_v12_registration_errors(self):
        client = self.client()
        cases = [
            ({"name": "Invalid", "palette": {"baseHue": 150, "states": {}}}, "palette.states"),
            ({"name": "Invalid", "palette": {"baseHue": 150,
                                             "states": {"error": "#ff0000"}}}, "palette.states.error"),
            ({"name": "Invalid", "palette": {"baseHue": 150},
              "icon": {"appBundleId": "no-dot"}}, "icon.appBundleId"),
            ({"name": "Invalid", "palette": {"baseHue": 150},
              "icon": {"png": "AAAA"}}, "icon.png"),
        ]
        for body, field in cases:
            with self.subTest(field=field):
                status, error, _ = client.call("PUT", "/adapters/dev.example.invalid", body)
                self.assertEqual(status, 422)
                self.assertEqual(error["code"], "invalid_field")
                self.assertEqual(error["field"], field)
                self.assertNotIn("dev.example.invalid", mock.adapters)

    def test_python_project_merge_and_null_clear(self):
        client = self.client()
        self.assertEqual(client.session("s1", title="Job", activity="thinking", host="jobs-vm",
                                        cwd="/work/jobs", project="Customer jobs")[0], 200)
        self.assertEqual(client.session("s1", activity="done")[0], 200)
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"]["project"], "Customer jobs")
        self.assertEqual(client.session("s1", project=None)[0], 200)
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"], {
            "title": "Job", "activity": "done", "host": "jobs-vm", "cwd": "/work/jobs"})

    def test_python_events_demo_applies_once_and_acknowledges(self):
        mock.DEMO_EVENTS = True
        result = self.run_client([sys.executable, str(EXAMPLES / "ref.py"), "--events"])
        self.assertEqual(result.returncode, 0, result.stderr)
        prefix = "authoritative toy UI applied "
        events = [json.loads(line[len(prefix):]) for line in result.stdout.splitlines()
                  if line.startswith(prefix)]
        self.assertEqual([event["type"] for event in events], ["tap", "approval", "tap", "question"])
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])
        self.assertEqual(events[1]["decision"], "deny")
        self.assertEqual(events[3]["picks"], [[0]])
        polls = [record[2] for record in self.records if record[1].endswith("/events")]
        self.assertEqual(polls, [{"after": 0, "ack": 0, "waitSeconds": 2},
                                 {"after": 2, "ack": 2, "waitSeconds": 2},
                                 {"after": 4, "ack": 4, "waitSeconds": 0}])
        self.assertEqual(mock.EVENTS.adapters[ref.ADAPTER]["ack"], 4)
        self.assertEqual(mock.sessions[ref.ADAPTER], {})
        self.assertTrue(all(record[3] < 400 for record in self.records))

    def test_python_events_wait_for_allow_and_denial(self):
        client = ref.Orbtile(events=True)
        replies = iter([(200, {"enabled": False, "reason": "awaiting_user"}, {}),
                        (200, {"enabled": True}, {})])
        with mocklib.patch.object(client, "call", side_effect=lambda *args, **kwargs: next(replies)) as call:
            with mocklib.patch.object(ref.time, "sleep", return_value=None):
                client.wait_until_enabled(timeout=2)
        self.assertEqual(call.call_count, 2)
        for invocation in call.call_args_list:
            self.assertEqual(invocation.args[:2], ("PUT", "/adapters/" + ref.ADAPTER))
            self.assertEqual(invocation.args[2]["capabilities"], ["tap", "approvals", "questions"])
        with mocklib.patch.object(client, "call", return_value=(
                200, {"enabled": False, "reason": "disabled_by_user"}, {})):
            with self.assertRaises(SystemExit):
                client.wait_until_enabled(timeout=2)

    def test_python_complete_demo(self):
        result = self.run_client([sys.executable, str(EXAMPLES / "ref.py")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: any(r[0] == "DELETE" for r in self.records))
        updates = [r[2]["activity"] for r in self.records if r[0] == "POST"]
        self.assertEqual(updates, ["thinking", "tool", "streaming", "done"])
        self.assertEqual(mock.sessions[ref.ADAPTER], {})
        self.assertTrue(all(r[3] < 400 for r in self.records))

    def test_python_merge_and_null_clear(self):
        client = self.client()
        self.assertEqual(client.session("s1", title="Job", activity="thinking", model="toy")[0], 200)
        self.assertEqual(client.session("s1", activity="done", model=None)[0], 200)
        self.wait_for(lambda: "model" not in mock.sessions[ref.ADAPTER]["s1"])
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"], {"title": "Job", "activity": "done"})

    def test_python_token_rotation_restores_full_session(self):
        client = self.client()
        self.assertEqual(client.session("s1", title="Kept title", activity="thinking")[0], 200)
        self.rotate()
        self.assertEqual(client.session("s1", activity="tool", toolLabel="Read file")[0], 200)
        self.wait_for(lambda: self.records[-1][3] == 200 and self.records[-1][2].get("activity") == "tool")
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"]["title"], "Kept title")
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"]["activity"], "tool")
        statuses = [r[3] for r in self.records]
        self.assertIn(401, statuses)
        self.assertIn(404, statuses)

    def test_python_lost_registration_restores_session(self):
        client = self.client()
        client.session("s1", title="Kept", activity="thinking")
        mock.adapters.clear()
        mock.sessions.clear()
        self.assertEqual(client.session("s1", activity="done")[0], 200)
        self.wait_for(lambda: mock.sessions.get(ref.ADAPTER, {}).get("s1", {}).get("activity") == "done")
        self.assertEqual(mock.sessions[ref.ADAPTER]["s1"]["title"], "Kept")

    def test_python_refuses_invalid_server_proof(self):
        client = ref.Orbtile()
        self.corrupt_proof = True
        with self.assertRaises(SystemExit):
            client.verify()
        self.assertEqual(mock.adapters, {})

    def test_shell_success_and_failure_exit_codes(self):
        for exit_code, activity in ((0, "done"), (3, "error")):
            result = self.run_client(["bash", str(EXAMPLES / "ref-shell.sh"),
                                      "sh", "-c", "exit " + str(exit_code)])
            self.assertEqual(result.returncode, exit_code, result.stderr)
            self.wait_for(lambda: any(s.get("activity") == activity
                                     for s in mock.sessions.get("dev.example.shell", {}).values()))
            latest = list(mock.sessions["dev.example.shell"].values())[-1]
            self.assertEqual(latest["statusDetail"], "exit " + str(exit_code))

    def test_shell_missing_discovery_preserves_command(self):
        env = dict(self.env, ORBTILE_API_FILE=str(Path("missing.json").resolve()))
        result = self.run_client(["bash", str(EXAMPLES / "ref-shell.sh"),
                                  "sh", "-c", "echo ran; exit 4"], env)
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stdout.strip(), "ran")
        self.assertIn("Orbtile adapter API is off", result.stderr)
        self.assertEqual(self.records, [])

    def test_shell_awkward_text_is_valid_json(self):
        result = self.run_client(["bash", str(EXAMPLES / "ref-shell.sh"), "echo",
                                  'tab\there', 'new\nline', 'quote"back\\slash', "é" * 60])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: len(self.records) >= 4)
        session = list(mock.sessions["dev.example.shell"].values())[-1]
        self.assertLessEqual(len(session["title"].encode()), 100)
        self.assertNotIn("\ufffd", session["title"])
        self.assertFalse(any(ord(c) < 32 for c in session["title"]))
        self.assertTrue(all(r[3] < 400 for r in self.records))

    def test_shell_heartbeat_recovers_after_token_rotation(self):
        self.rotate_after = lambda method, path: method == "POST" and "/sessions/" in path
        result = self.run_client(["bash", str(EXAMPLES / "ref-shell.sh"), "sleep", "0.4"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: any(r[3] == 401 for r in self.records))
        self.assertTrue(any(r[1].endswith("/heartbeat") for r in self.records))
        running = [r for r in self.records if r[0] == "POST" and r[2].get("activity") == "tool"]
        self.assertGreaterEqual(len(running), 2)
        self.assertEqual(list(mock.sessions["dev.example.shell"].values())[-1]["activity"], "done")

    def run_node_ticks(self, env=None):
        script = '''let tick;
globalThis.setInterval = (f) => { tick = f; return 0; };
await import(process.argv[1]);
await tick();
await tick();'''
        result = self.run_client(["node", "--input-type=module", "-e", script,
                                  (EXAMPLES / "ref.mjs").as_uri()], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: mock.sessions.get("dev.example.node", {}).get("job-42") is not None)
        self.assertEqual(mock.sessions["dev.example.node"]["job-42"]["activity"], "tool")
        self.assertNotIn("icon", mock.adapters["dev.example.node"])
        self.assertNotIn("states", mock.adapters["dev.example.node"]["palette"])

    def test_node_poller_full_replace(self):
        self.run_node_ticks()
        batches = [r for r in self.records if r[1].endswith("/sessions")]
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(r[3] == 200 for r in batches))

    def test_node_poller_recovers_after_token_rotation(self):
        self.rotate_after = lambda method, path: method == "PUT" and path.endswith("/sessions")
        self.run_node_ticks()
        self.assertIn(401, [r[3] for r in self.records])
        self.assertIn(404, [r[3] for r in self.records])
        self.assertEqual(len(mock.sessions["dev.example.node"]), 1)

    def enterprise_discovery_env(self):
        home = Path("enterprise-home").resolve()
        directory = home / "Library" / "Application Support" / "Orbtile Enterprise"
        directory.mkdir(parents=True)
        discovery = directory / "adapter-api.json"
        discovery.write_text(self.discovery.read_text())
        discovery.chmod(0o600)
        env = dict(self.env, HOME=str(home))
        env.pop("ORBTILE_API_FILE", None)
        return env

    def test_enterprise_discovery_fallback_for_all_clients(self):
        env = self.enterprise_discovery_env()
        expected = Path(env["HOME"]) / "Library" / "Application Support" / "Orbtile Enterprise" / "adapter-api.json"
        script = ("import sys; sys.path.insert(0, " + repr(str(EXAMPLES)) + "); import ref; "
                  "assert ref.API_FILE == " + repr(str(expected)) + "; "
                  "client = ref.Orbtile(); client.verify(); client.register()")
        result = self.run_client([sys.executable, "-c", script], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_client(["bash", str(EXAMPLES / "ref-shell.sh"),
                                  "sh", "-c", "exit 0"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("icon", mock.adapters["dev.example.shell"])
        self.assertNotIn("states", mock.adapters["dev.example.shell"]["palette"])
        self.run_node_ticks(env=env)
        self.assertEqual(set(mock.adapters), {ref.ADAPTER, "dev.example.shell", "dev.example.node"})
        self.assertTrue(all(record[3] < 400 for record in self.records))

    def test_mock_refuses_unregistered_and_unsafe_url_schemes(self):
        client = self.client()
        path = "/adapters/" + ref.ADAPTER + "/sessions/s1"
        for url in ("file:///example", "javascript:alert(1)", "data:text/html,x",
                    "orbtile://x", "x-apple.systempreferences:x", "myagent://x", ""):
            status, error, _ = client.call("POST", path,
                                          {"title": "Test", "activity": "idle", "openURL": url})
            self.assertEqual(status, 422, url)
            self.assertEqual(error["field"], "openURL")
        self.assertEqual(client.call("POST", path, {"title": "Test", "activity": "idle",
                                                    "openURL": "https://example.com/"})[0], 200)
        for schemes in (["file"], ["javascript"], ["data"], ["orbtile"],
                        ["x-apple.systempreferences"], ["Bad"], ["a", "a"],
                        ["a", "b", "c", "d", "e"], "myagent"):
            status, error, _ = client.call("PUT", "/adapters/dev.example.invalid",
                                          {"name": "Invalid", "palette": {"baseHue": 150},
                                           "urlSchemes": schemes})
            self.assertEqual(status, 422)
            self.assertEqual(error["field"], "urlSchemes")

    def test_mock_auth_origin_and_activity_errors(self):
        client = self.client()
        self.assertEqual(client.call("GET", "/info", headers={"Authorization": "Bearer invalid"}, retry=())[0], 401)
        self.assertEqual(client.call("GET", "/info", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(client.session("s1", title="Test", activity="unknown")[0], 422)


if __name__ == "__main__":
    unittest.main()
