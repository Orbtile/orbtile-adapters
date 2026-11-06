import asyncio
import hashlib
import hmac
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler

from orbtile_adapter import SessionUpdate, Client, AsyncClient, AdapterError, APIError, LeaseLostError, ProofError, PayloadTooLargeError


class Fake:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oa-')
        self.path = self.temp.name + '/api.json'
        self.calls = []
        self.events = []
        self.heartbeat = threading.Event()
        self.token = 'test-secret'
        self.owner = 'owner-secret'
        self.registered = False
        self.bad_proof = False
        self.cursor = 0
        self.delivered = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_api()

            def do_PUT(self):
                self.handle_api()

            def do_POST(self):
                self.handle_api()

            def do_DELETE(self):
                self.handle_api()

            def handle_api(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                body = json.loads(raw) if raw else None
                fake.calls.append((self.command, self.path, body, dict(self.headers)))
                self.assert_headers()
                status, reply = 200, {}
                if self.headers['Authorization'] != 'Bearer ' + fake.token:
                    status, reply = 401, {'error': {'code': 'unauthorized', 'message': fake.token}}
                elif self.path.endswith('/info'):
                    nonce = self.headers['X-Orbtile-Nonce']
                    reply = {'api': 1, 'apiMinor': 1, 'proof': ('invalid-proof' if fake.bad_proof else hmac.new(fake.token.encode(), nonce.encode(), hashlib.sha256).hexdigest())}
                elif self.command == 'PUT' and self.path == '/a/v1/adapters/dev.example.test':
                    if fake.registered:
                        assert self.headers['X-Orbtile-Adapter-Token'] == fake.owner
                    fake.registered = True
                    status = 201
                    reply = {'adapter': {'id': 'dev.example.test'}, 'enabled': True, 'adapterToken': fake.owner}
                elif not fake.registered:
                    status, reply = 404, {'error': {'code': 'adapter_not_registered'}}
                else:
                    assert self.headers['X-Orbtile-Adapter-Token'] == fake.owner
                    if self.path.endswith('/heartbeat'):
                        fake.heartbeat.set()
                        status = 204
                    elif self.path.endswith('/events'):
                        assert fake.cursor <= body['ack'] <= body['after'] <= fake.delivered
                        fake.cursor = body['ack']
                        fake.events = [e for e in fake.events if e['sequence'] > fake.cursor]
                        events = [e for e in fake.events if e['sequence'] > body['after']]
                        if events:
                            fake.delivered = max(fake.delivered, max(e['sequence'] for e in events))
                        reply = {'events': events, 'lastSequence': 99, 'acknowledged': body['ack']}
                    elif self.command == 'DELETE':
                        status = 204
                    else:
                        reply = {'session': {'uid': 'external:test', 'visible': True}}
                encoded = json.dumps(reply).encode() if status != 204 else b''
                self.send_response(status)
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Connection', 'close')
                self.end_headers()
                if encoded:
                    self.wfile.write(encoded)

            def assert_headers(self):
                assert self.headers['Host'] == 'localhost'
                assert self.headers['Content-Type'] == 'application/json'
                assert self.headers['Authorization'].startswith('Bearer ')
                assert not self.headers.get('Origin')
                assert not self.headers.get('Transfer-Encoding')

        windows = os.name == 'nt'
        server_base = socketserver.TCPServer if windows else socketserver.UnixStreamServer

        class Server(socketserver.ThreadingMixIn, server_base):
            daemon_threads = True

        address = ('127.0.0.1', 0) if windows else self.temp.name + '/s'
        self.server = Server(address, Handler)

        def write_discovery(token):
            discovery = {'api': 1, 'basePath': '/a/v1', 'token': token}
            if windows:
                port = self.server.server_address[1]
                discovery.update(url='http://127.0.0.1:%s' % port, port=port)
            else:
                discovery['socketPath'] = self.temp.name + '/s'
            with open(self.path, 'w') as f:
                json.dump(discovery, f)

        self.write_discovery = write_discovery
        self.write_discovery('test-secret')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def client(self, **kwargs):
        return Client('dev.example.test', {'name': 'Test', 'palette': {'baseHue': 150}, 'capabilities': ['tap', 'approvals', 'questions']}, discovery_file=self.path, **kwargs)

    def restart(self):
        self.token, self.owner = 'rotated-secret', 'new-owner'
        self.registered = False
        self.events = []
        self.cursor = self.delivered = 0
        self.write_discovery(self.token)


class SDKTests(unittest.TestCase):
    def test_project_merge_clear_batch_and_recovery(self):
        with Fake() as fake, fake.client() as client:
            update: SessionUpdate = {'title': 'Job', 'activity': 'thinking', 'project': 'Customer jobs'}
            client.upsert_session('s', **update)
            self.assertEqual(fake.calls[-1][2]['project'], 'Customer jobs')
            client.upsert_session('s', activity='idle')
            self.assertNotIn('project', fake.calls[-1][2])
            client.upsert_session('s', project=None)
            self.assertIsNone(fake.calls[-1][2]['project'])
            fake.restart()
            with self.assertRaises(LeaseLostError):
                client.heartbeat()
            self.assertTrue(client.reconciliation_required)
            restored = [body for method, path, body, headers in fake.calls
                        if method == 'POST' and '/sessions/' in path][-1]
            self.assertIn('project', restored)
            self.assertIsNone(restored['project'])
            client.replace_sessions([{'id': 'job', 'title': 'Job', 'activity': 'idle', 'project': 'Batch jobs'}])
            self.assertEqual(fake.calls[-1][2]['sessions'][0]['project'], 'Batch jobs')

    def test_resume_only_acknowledges_applied_events_not_last_sequence(self):
        with Fake() as fake, fake.client() as client:
            event = {'sequence': 3, 'type': 'tap', 'sessionId': 's', 'host': 'local'}
            fake.events = [event]
            self.assertEqual(client.poll_events(0)['events'], [event])
            self.assertEqual(client.poll_events(0)['events'], [event])
            client.apply(event, lambda value: None)
            self.assertEqual(client.poll_events(0)['events'], [])
            polls = [c[2] for c in fake.calls if c[1].endswith('/events')]
            self.assertEqual(polls, [{'after': 0, 'ack': 0, 'waitSeconds': 0}] * 2 + [{'after': 3, 'ack': 3, 'waitSeconds': 0}])

    def test_automatic_lease_renewal(self):
        with Fake() as fake, fake.client(heartbeat_interval=0.02) as client:
            self.assertTrue(fake.heartbeat.wait(1))
            self.assertIsNone(client.heartbeat_error)

    def test_utf8_oversize_rejected_before_connect(self):
        with Fake() as fake, fake.client() as client:
            count = len(fake.calls)
            with self.assertRaises(PayloadTooLargeError):
                client.upsert_session('s', title='Demo', activity='streaming', message={'id': 'm', 'text': 'é' * 40000})
            self.assertEqual(len(fake.calls), count)

    def test_callback_failure_and_own_ui_answer_never_allow(self):
        with Fake() as fake, fake.client() as client:
            client.upsert_session('s', title='Demo', activity='waitingPermission', approval={'id': 'a1', 'toolName': 'Read', 'commandSummary': 'README'})
            event = {'sequence': 1, 'type': 'approval', 'sessionId': 's', 'host': 'local', 'requestId': 'a1', 'decision': 'allow', 'expiresAt': time.time() + 100}
            fake.events = [event]
            client.poll_events(0)
            def fail(value):
                raise RuntimeError('UI transaction failed')
            with self.assertRaises(RuntimeError):
                client.apply(event, fail)
            self.assertEqual(client.cursor, 0)
            client.answer_locally('s', lambda: None)
            seen = []
            self.assertFalse(client.apply(event, seen.append))
            self.assertEqual(seen, [])
            self.assertEqual(client.cursor, 1)

    def test_cursor_gaps_and_mutated_events_are_not_delivered(self):
        with Fake() as fake, fake.client() as client:
            fake.events = [{'sequence': 3, 'type': 'tap', 'sessionId': 's', 'host': 'local'}]
            event = client.poll_events(0)['events'][0]
            with self.assertRaises(ValueError):
                client.acknowledge({**event, 'sequence': 2})
            with self.assertRaises(ValueError):
                client.apply({**event, 'sessionId': 'other'}, lambda e: None)
            self.assertEqual(client.cursor, 0)

    def test_tap_does_not_consume_pending_question_and_invalid_picks_discard(self):
        with Fake() as fake, fake.client() as client:
            question = {'id': 'q1', 'questions': [{'text': 'Docs?', 'options': [{'label': 'Readme'}, {'label': 'API'}]}]}
            client.ask_question('s', question, title='Demo')
            fake.events = [{'sequence': 1, 'type': 'tap', 'sessionId': 's', 'host': 'local'},
                           {'sequence': 2, 'type': 'question', 'sessionId': 's', 'host': 'local', 'requestId': 'q1', 'picks': [[0, 1]], 'expiresAt': time.time() + 100},
                           {'sequence': 3, 'type': 'question', 'sessionId': 's', 'host': 'local', 'requestId': 'q1', 'picks': [[1]], 'expiresAt': time.time() + 100}]
            events = client.poll_events(0)['events']
            seen = []
            self.assertTrue(client.apply(events[0], seen.append))
            self.assertFalse(client.apply(events[1], seen.append))
            self.assertTrue(client.apply(events[2], seen.append))
            self.assertEqual([event['type'] for event in seen], ['tap', 'question'])
            with self.assertRaises(ValueError):
                client.ask_question('s', question)

    def test_wrong_host_expiry_and_reused_id_are_rejected(self):
        with Fake() as fake, fake.client() as client:
            approval = {'id': 'a1', 'toolName': 'Read', 'commandSummary': 'README'}
            client.request_approval('s', approval, host='vm', title='Demo')
            fake.events = [{'sequence': 1, 'type': 'approval', 'sessionId': 's', 'host': 'local', 'requestId': 'a1', 'decision': 'allow', 'expiresAt': time.time() + 100},
                           {'sequence': 2, 'type': 'approval', 'sessionId': 's', 'host': 'vm', 'requestId': 'a1', 'decision': 'allow', 'expiresAt': time.time() - 1}]
            seen = []
            for event in client.poll_events(0)['events']:
                self.assertFalse(client.apply(event, seen.append))
            self.assertEqual(seen, [])
            client.answer_locally('s', lambda: None, 'vm')
            with self.assertRaises(ValueError):
                client.request_approval('other', approval, title='Other')

    def test_restart_restores_display_only_and_signals_reconciliation(self):
        with Fake() as fake, fake.client() as client:
            client.upsert_session('other', title='Other', activity='thinking', host='vm', project='Customer jobs')
            client.request_approval('s', {'id': 'old', 'toolName': 'Read', 'commandSummary': 'README'}, title='Demo')
            fake.restart()
            with self.assertRaises(LeaseLostError):
                client.heartbeat()
            self.assertTrue(client.reconciliation_required)
            self.assertEqual(client.cursor, 0)
            restores = [body for method, path, body, headers in fake.calls if method == 'POST' and '/sessions/' in path and headers.get('Authorization') == 'Bearer rotated-secret']
            self.assertEqual(len(restores), 2)
            self.assertEqual(restores[0]['title'], 'Other')
            self.assertEqual(restores[0]['host'], 'vm')
            self.assertEqual(restores[0]['project'], 'Customer jobs')
            self.assertNotIn('approval', restores[1])
            self.assertEqual(restores[1]['activity'], 'idle')
            client.reconcile()
            self.assertFalse(client.reconciliation_required)

    def test_failed_proof_prevents_registration(self):
        with Fake() as fake:
            fake.bad_proof = True
            client = fake.client()
            with self.assertRaises(ProofError):
                client.connect()
            self.assertFalse(fake.registered)
            self.assertEqual(len(fake.calls), 1)

    def test_replace_remove_and_encoded_identity(self):
        with Fake() as fake, fake.client() as client:
            client.replace_sessions([{'id': 'remote run', 'host': 'vm', 'title': 'Remote', 'activity': 'idle'}])
            client.remove_session('remote run', 'vm')
            calls = [(method, path, body) for method, path, body, headers in fake.calls]
            self.assertIn(('PUT', '/a/v1/adapters/dev.example.test/sessions', {'sessions': [{'id': 'remote run', 'host': 'vm', 'title': 'Remote', 'activity': 'idle'}]}), calls)
            self.assertIn(('DELETE', '/a/v1/adapters/dev.example.test/sessions/remote%20run?host=vm', None), calls)

    def test_awaitable_callback_is_not_acknowledged(self):
        with Fake() as fake, fake.client() as client:
            fake.events = [{'sequence': 1, 'type': 'tap', 'sessionId': 's', 'host': 'local'}]
            event = client.poll_events(0)['events'][0]
            async def callback():
                pass
            with self.assertRaises(TypeError):
                client.apply(event, lambda e: callback())
            self.assertEqual(client.cursor, 0)

    def test_keypad_winner_blocks_late_own_ui_and_replaced_prompt(self):
        with Fake() as fake, fake.client() as client:
            question = {'id': 'q1', 'questions': [{'text': 'Docs?', 'options': [{'label': 'Readme'}, {'label': 'API'}]}]}
            client.ask_question('s', question, title='Demo')
            fake.events = [{'sequence': 1, 'type': 'question', 'sessionId': 's', 'host': 'local', 'requestId': 'q1', 'picks': [[0]], 'expiresAt': time.time() + 100}]
            event = client.poll_events(0)['events'][0]
            seen = []
            self.assertTrue(client.apply(event, seen.append))
            self.assertFalse(client.answer_locally('s', lambda: seen.append('late'), request_id='q1'))
            client.ask_question('s', {**question, 'id': 'q2'})
            self.assertFalse(client.answer_locally('s', lambda: seen.append('old'), request_id='q1'))
            self.assertEqual(len(seen), 1)

    def test_descriptor_change_retires_pending_preserves_cursor_history(self):
        with Fake() as fake, fake.client() as client:
            approval = {'id': 'a1', 'toolName': 'Read', 'commandSummary': 'README'}
            client.request_approval('s', approval, title='Demo')
            fake.events = [{'sequence': 1, 'type': 'tap', 'sessionId': 's', 'host': 'local'},
                           {'sequence': 2, 'type': 'approval', 'sessionId': 's', 'host': 'local', 'requestId': 'a1', 'decision': 'allow', 'expiresAt': time.time() + 100}]
            events = client.poll_events(0)['events']
            client.apply(events[0], lambda e: None)
            client.registration['name'] = 'Changed Test'
            client.register()
            self.assertEqual(client.cursor, 1)
            self.assertTrue(client.reconciliation_required)
            with self.assertRaises(ValueError):
                client.apply(events[1], lambda e: None)
            self.assertFalse(client.answer_locally('s', lambda: self.fail('Retired request must not commit')))
            with self.assertRaises(ValueError):
                client.request_approval('s', approval)

    def test_merge_clear_of_other_kind_preserves_pending_request(self):
        with Fake() as fake, fake.client() as client:
            client.request_approval('s', {'id': 'a1', 'toolName': 'Read', 'commandSummary': 'README'}, title='Demo')
            client.upsert_session('s', question=None, statusDetail='Still waiting')
            fake.events = [{'sequence': 1, 'type': 'approval', 'sessionId': 's', 'host': 'local', 'requestId': 'a1', 'decision': 'deny', 'expiresAt': time.time() + 100}]
            event = client.poll_events(0)['events'][0]
            seen = []
            self.assertTrue(client.apply(event, seen.append))
            self.assertEqual(seen[0]['decision'], 'deny')

    def test_api_error_does_not_reveal_server_message_or_tokens(self):
        error = APIError(401, 'unauthorized', 'token')
        self.assertEqual(error.status, 401)
        self.assertEqual(error.code, 'unauthorized')
        self.assertNotIn('token', str(error))

    def test_async_iterator_and_own_ui_callback(self):
        with Fake() as fake:
            async def exercise():
                registration = {'name': 'Test', 'palette': {'baseHue': 150}, 'capabilities': ['tap']}
                async with AsyncClient('dev.example.test', registration, discovery_file=fake.path) as client:
                    await client.upsert_session('s', title='Async', activity='streaming')
                    fake.events = [{'sequence': 1, 'type': 'tap', 'sessionId': 's', 'host': 'local'}]
                    seen = []
                    async for event in client.events(0):
                        await client.apply(event, seen.append)
                        break
                    self.assertEqual(client.cursor, 1)
                    self.assertEqual(len(seen), 1)
                    await client.poll_events(0)
                    await client.remove_session('s')
            asyncio.run(exercise())


class DiscoveryTests(unittest.TestCase):
    """Explicit file, then ORBTILE_API_FILE, then the platform's Standard and Enterprise defaults."""
    platform = 'darwin'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = temp.name
        support = self.home + '/Library/Application Support'
        self.standard = os.path.join(support, 'Orbtile', 'adapter-api.json')
        self.enterprise = os.path.join(support, 'Orbtile Enterprise', 'adapter-api.json')
        env = {k: v for k, v in os.environ.items() if k != 'ORBTILE_API_FILE'}
        env.update(HOME=self.home, USERPROFILE=self.home, LOCALAPPDATA=os.path.join(self.home, 'AppData', 'Local'))
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        platform = unittest.mock.patch('sys.platform', self.platform)
        platform.start()
        self.addCleanup(platform.stop)

    def touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()

    def chosen(self, **kwargs):
        return Client('dev.example.test', {'name': 'Test', 'palette': {'baseHue': 150}}, **kwargs).discovery_file

    def test_enterprise_file_when_standard_is_missing(self):
        self.touch(self.enterprise)
        self.assertEqual(self.chosen(), self.enterprise)

    def test_standard_file_stays_first(self):
        self.touch(self.standard)
        self.touch(self.enterprise)
        self.assertEqual(self.chosen(), self.standard)

    def test_standard_path_when_neither_exists(self):
        self.assertEqual(self.chosen(), self.standard)

    def test_explicit_file_and_environment_never_fall_back(self):
        self.touch(self.enterprise)
        missing = os.path.join(self.home, 'missing.json')
        self.assertEqual(self.chosen(discovery_file=missing), missing)
        os.environ['ORBTILE_API_FILE'] = missing
        self.assertEqual(self.chosen(), missing)


class WindowsDiscoveryTests(DiscoveryTests):
    platform = 'win32'

    def setUp(self):
        super().setUp()
        self.store = os.path.join(self.home, '.orbtile', 'adapter-api.json')
        self.standard = os.path.join(os.environ['LOCALAPPDATA'], 'Orbtile', 'Windows', 'adapter-api.json')
        self.enterprise = os.path.join(os.environ['LOCALAPPDATA'], 'Orbtile Enterprise', 'Windows', 'adapter-api.json')

    def test_standard_path_when_neither_exists(self):
        self.assertEqual(self.chosen(), self.store)

    def test_store_file_stays_first(self):
        self.touch(self.store)
        self.touch(self.standard)
        self.touch(self.enterprise)
        self.assertEqual(self.chosen(), self.store)


if __name__ == '__main__':
    unittest.main()
