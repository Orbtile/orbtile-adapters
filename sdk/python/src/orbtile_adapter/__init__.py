"""Dependency-free Orbtile Adapter API v1.1 clients for Unix sockets and Windows loopback HTTP."""
import asyncio
import copy
import hashlib
import hmac
import http.client
import inspect
import json
import math
import os
import secrets
import socket
import sys
import threading
import time
from typing import TypedDict
from urllib.parse import quote

__version__ = '0.1.0'


class SessionUpdate(TypedDict, total=False):
    """Optional merge fields. project is a trimmed 1...128 character group; None clears."""
    title: str
    activity: str
    cwd: str | None
    project: str | None
    model: str | None
    toolLabel: str | None
    toolKind: str | None
    message: dict | None
    statusDetail: str | None
    subagentCount: int
    host: str
    openURL: str | None
    pid: int | None
    background: bool
    liveText: bool
    ttlSeconds: int
    approval: dict | None
    question: dict | None


class SessionBatchItem(SessionUpdate):
    id: str


class AdapterError(Exception):
    """An SDK error. Credential values and server error messages are never included."""


class APIError(AdapterError):
    def __init__(self, status, code, field=None, retry_after=None):
        self.status, self.code, self.field, self.retry_after = status, code, field, retry_after
        # Do not trust remote text to be free of credentials.
        super().__init__('Orbtile API request failed (HTTP %s)' % status)


class TransportError(AdapterError):
    pass


class DiscoveryError(AdapterError):
    pass


class ProofError(AdapterError):
    pass


class PayloadTooLargeError(AdapterError):
    pass


class LeaseLostError(AdapterError):
    """A fresh registration requires own-UI reconciliation and fresh prompt ids."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _valid_picks(questions, picks):
    if not isinstance(picks, list) or len(picks) != len(questions):
        return False
    for question, selected in zip(questions, picks):
        if (not isinstance(selected, list) or not selected
                or any(type(i) is not int or i < 0 or i >= len(question['options']) for i in selected)
                or len(set(selected)) != len(selected)
                or (not question.get('multiSelect', False) and len(selected) != 1)):
            return False
    return True


def _default_discovery_file():
    """The first existing platform discovery file, or the current product's default path."""
    if sys.platform == 'win32':
        store = os.path.join(os.path.expanduser('~'), '.orbtile', 'adapter-api.json')
        local = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        standard = os.path.join(local, 'Orbtile', 'Windows', 'adapter-api.json')
        enterprise = os.path.join(local, 'Orbtile Enterprise', 'Windows', 'adapter-api.json')
        return next((path for path in (store, standard, enterprise) if os.path.exists(path)), store)
    support = os.path.expanduser('~/Library/Application Support')
    standard = os.path.join(support, 'Orbtile', 'adapter-api.json')
    enterprise = os.path.join(support, 'Orbtile Enterprise', 'adapter-api.json')
    return enterprise if not os.path.exists(standard) and os.path.exists(enterprise) else standard


class Client:
    """One adapter lease. Use as a context manager or call connect()/close().

    Session updates are merges; cache keys include host. events() yields without
    acknowledging: commit using apply(), or deliberately discard using acknowledge().
    Own UI callbacks must be synchronous, atomic, and have no side effects on failure.
    """
    def __init__(self, adapter_id, registration, *, discovery_file=None,
                 heartbeat_interval=5, timeout=5):
        if not 0 < heartbeat_interval <= 10:
            raise ValueError('heartbeat_interval must be greater than 0 and at most 10')
        self.adapter_id = adapter_id
        self.registration = copy.deepcopy(registration)
        self.discovery_file = discovery_file or os.environ.get('ORBTILE_API_FILE') or _default_discovery_file()
        self.heartbeat_interval, self.timeout = heartbeat_interval, timeout
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._token = self._adapter_token = None
        self._connected = False
        self.lease_generation = 0
        self.reconciliation_required = False
        self._cursor = self._delivered = 0
        self._received = {}
        self._sessions, self._pending, self._history = {}, {}, set()
        self.heartbeat_error = None
        self.info = None
        self.registration_reply = None
        self._registered_descriptor = None

    @property
    def cursor(self):
        with self._lock:
            return self._cursor

    @property
    def capabilities(self):
        return tuple(self.registration.get('capabilities', ()))

    def _load(self):
        try:
            with open(self.discovery_file, encoding='utf-8') as file:
                discovery = json.load(file)
            port = discovery.get('port')
            windows_tcp = (sys.platform == 'win32' and type(port) is int and 0 < port <= 65535
                           and discovery.get('url') == 'http://127.0.0.1:%s' % port)
            if (discovery.get('api') != 1 or discovery.get('basePath') != '/a/v1'
                    or (not isinstance(discovery.get('socketPath'), str) and not windows_tcp)
                    or not isinstance(discovery.get('token'), str) or not discovery['token']
                    or '\r' in discovery['token'] or '\n' in discovery['token']):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise DiscoveryError('Adapter API discovery unavailable or invalid') from None
        changed = self._token is not None and self._token != discovery['token']
        self._token = discovery['token']
        self._socket_path = discovery.get('socketPath') if isinstance(discovery.get('socketPath'), str) else None
        self._port = port if windows_tcp else None
        self._base = discovery['basePath']
        if changed:
            self._adapter_token = None
        return changed

    def _encode(self, body):
        data = b'' if body is None else json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(data) > 65536:
            raise PayloadTooLargeError('Request exceeds the 64 KiB UTF-8 body limit')
        return data

    def _request(self, method, path, body=None, nonce=None):
        data = self._encode(body)
        headers = {'Authorization': 'Bearer ' + self._token,
                   'Content-Type': 'application/json', 'Content-Length': str(len(data)),
                   'Host': 'localhost'}
        if self._adapter_token and path.startswith('/adapters/'):
            headers['X-Orbtile-Adapter-Token'] = self._adapter_token
        if nonce:
            headers['X-Orbtile-Nonce'] = nonce
        connection = (_UnixHTTPConnection(self._socket_path, self.timeout) if self._socket_path is not None
                      else http.client.HTTPConnection('127.0.0.1', self._port, timeout=self.timeout))
        try:
            connection.request(method, self._base + path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status, retry = response.status, response.getheader('Retry-After')
        except (OSError, http.client.HTTPException):
            message = 'Unix HTTP transport failed' if self._socket_path is not None else 'Loopback HTTP transport failed'
            raise TransportError(message) from None
        finally:
            connection.close()
        try:
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise AdapterError('Invalid JSON response from Orbtile') from None
        if status >= 400:
            error = result.get('error', {})
            try:
                delay = float(retry) if retry is not None else None
                if delay is not None and (not math.isfinite(delay) or delay < 0):
                    delay = None
            except ValueError:
                delay = None
            raise APIError(status, error.get('code'), error.get('field'), delay)
        return result

    def verify(self):
        with self._lock:
            self._load()
            nonce = secrets.token_hex(16)
            try:
                info = self._request('GET', '/info', nonce=nonce)
            except APIError as error:
                if error.status != 401:
                    raise
                self._load()
                info = self._request('GET', '/info', nonce=nonce)
            expected = hmac.new(self._token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            if not isinstance(info.get('proof'), str) or not hmac.compare_digest(expected, info['proof']):
                raise ProofError('Listener proof failed; refusing to send session data')
            if self.capabilities and (info.get('api') != 1 or info.get('apiMinor', 0) < 1):
                raise AdapterError('Interactive capabilities require API v1.1')
            self.info = info
            return info

    def _path(self):
        return '/adapters/' + quote(self.adapter_id, safe='')

    def _reset_lease(self):
        self.lease_generation += 1
        self._cursor = self._delivered = 0
        self._received.clear()
        self._pending.clear()
        self._history.clear()
        for state in self._sessions.values():
            state.pop('approval', None)
            state.pop('question', None)
            if state.get('activity') in ('waitingPermission', 'waitingQuestion'):
                state['activity'] = 'idle'

    def register(self):
        with self._lock:
            previous = self._adapter_token
            descriptor = {field: copy.deepcopy(self.registration.get(field)) for field in
                          ('name', 'shortName', 'palette', 'urlSchemes', 'homepage', 'capabilities', 'tapBehavior')}
            changed = self._registered_descriptor is not None and descriptor != self._registered_descriptor
            reply = self._request('PUT', self._path(), self.registration)
            token = reply.get('adapterToken')
            if self.capabilities and (not isinstance(token, str) or not token):
                raise AdapterError('Interactive registration omitted ownership token')
            if token is not None and (not isinstance(token, str) or '\r' in token or '\n' in token):
                raise AdapterError('Invalid ownership token')
            if token != previous:
                self._reset_lease()
            if changed:
                self.reconciliation_required = self.reconciliation_required or bool(self._pending)
                self._pending.clear()
                self._received.clear()
                for state in self._sessions.values():
                    state.pop('approval', None)
                    state.pop('question', None)
            self._registered_descriptor = descriptor
            self._adapter_token = token
            self.registration_reply = reply
            return reply

    def connect(self):
        with self._lock:
            if self._connected:
                return self.registration_reply
            self.verify()
            try:
                result = self.register()
            except APIError as error:
                if error.status != 401:
                    raise
                self.verify()
                result = self.register()
            self._connected = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._renew, name='orbtile-lease', daemon=True)
            self._thread.start()
            return result

    def _call(self, method, path, body=None, *, full=None):
        self._encode(body)  # Reject oversize before any I/O, including recovery.
        if not self._connected:
            raise AdapterError('Call connect() before using the adapter')
        refreshed = False
        recovered = False
        while True:
            try:
                return self._request(method, path, body)
            except APIError as error:
                if error.status == 401 and not refreshed:
                    refreshed = True
                    self.verify()  # Re-read discovery and prove the new listener before sending data.
                    continue
                if error.code == 'adapter_not_registered' and not recovered:
                    recovered = True
                    with self._lock:
                        self._adapter_token = None
                        self._reset_lease()
                        self.register()
                        for (sid, host), state in self._sessions.items():
                            self._request('POST', self._path() + '/sessions/' + quote(sid, safe=''), {**state, 'host': host})
                        self.reconciliation_required = bool(self.capabilities)
                    if self.capabilities:
                        raise LeaseLostError('Registration renewed; reconcile own UI and use fresh request ids') from None
                    body = full if full is not None else body
                    continue
                raise

    def heartbeat(self):
        with self._lock:
            return self._call('POST', self._path() + '/heartbeat')

    def _renew(self):
        while not self._stop.wait(self.heartbeat_interval):
            try:
                self.heartbeat()
                if not self.reconciliation_required:
                    self.heartbeat_error = None
            except Exception as error:
                # Inspect this property; nothing is logged and prompts never auto-Allow.
                self.heartbeat_error = error

    def upsert_session(self, session_id, **fields):
        with self._lock:
            host = fields.get('host', 'local')
            key = (session_id, host)
            full = {**self._sessions.get(key, {}), **copy.deepcopy(fields)}
            new_pending = None
            for kind in ('approval', 'question'):
                request = full.get(kind)
                if request is not None:
                    if new_pending is not None:
                        raise ValueError('Only one interactive request may be pending')
                    if ('approvals' if kind == 'approval' else 'questions') not in self.capabilities:
                        raise ValueError('Declare the requested interactive capability first')
                    old = self._pending.get(key)
                    if old and old[1]['id'] == request['id']:
                        if old[0] != kind or old[1] != request:
                            raise ValueError('A pending id cannot change its contents')
                        new_pending = old
                    else:
                        if request['id'] in self._history:
                            raise ValueError('Request id already used in this registration')
                        if len(self._history) >= 4096:
                            raise ValueError('Request id history is full')
                        new_pending = (kind, copy.deepcopy(request), time.time() + (120 if kind == 'approval' else 600))
            reply = self._call('POST', self._path() + '/sessions/' + quote(session_id, safe=''), fields, full=full)
            self._sessions[key] = full
            if new_pending:
                self._history.add(new_pending[1]['id'])
                self._pending[key] = new_pending
            elif 'approval' in fields or 'question' in fields:
                self._pending.pop(key, None)
            return reply

    def request_approval(self, session_id, request, **fields):
        return self.upsert_session(session_id, **{**fields, 'activity': 'waitingPermission', 'question': None, 'approval': request})

    def ask_question(self, session_id, request, **fields):
        return self.upsert_session(session_id, **{**fields, 'activity': 'waitingQuestion', 'approval': None, 'question': request})

    def reconcile(self):
        """After reconciling own UI, explicitly clear the lease-loss notification."""
        with self._lock:
            self.reconciliation_required = False
            self.heartbeat_error = None

    def replace_sessions(self, sessions):
        """Display-only full replacement. Publish interactive requests via upsert_session()."""
        rows = copy.deepcopy(sessions)
        if any(row.get('approval') is not None or row.get('question') is not None for row in rows):
            raise ValueError('Publish interactive requests with upsert_session')
        with self._lock:
            reply = self._call('PUT', self._path() + '/sessions', {'sessions': rows})
            self._sessions = {(row['id'], row.get('host', 'local')): {k: v for k, v in row.items() if k != 'id'} for row in rows}
            self._pending.clear()
            return reply

    def remove_session(self, session_id, host='local'):
        with self._lock:
            reply = self._call('DELETE', self._path() + '/sessions/' + quote(session_id, safe='') + '?host=' + quote(host, safe=''))
            self._sessions.pop((session_id, host), None)
            self._pending.pop((session_id, host), None)
            return reply

    def poll_events(self, wait_seconds=2):
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 2:
            raise ValueError('wait_seconds must be an integer from 0 to 2')
        if not self.capabilities:
            raise AdapterError('Events require an interactive registration')
        if not self._poll_lock.acquire(blocking=False):
            raise AdapterError('Only one outstanding event poll is allowed')
        try:
            with self._lock:
                cursor = self._cursor
                generation = self.lease_generation
            # No authoritative UI lock during the long poll: own UI can answer meanwhile.
            reply = self._call('POST', self._path() + '/events', {'after': cursor, 'ack': cursor, 'waitSeconds': wait_seconds})
            with self._lock:
                if generation != self.lease_generation:
                    raise LeaseLostError('Event poll belongs to an ended registration')
                for event in reply.get('events', []):
                    sequence = event.get('sequence')
                    if type(sequence) is not int or sequence <= 0:
                        raise AdapterError('Invalid event sequence')
                    self._delivered = max(self._delivered, sequence)
                    self._received[sequence] = copy.deepcopy(event)
            return reply
        finally:
            self._poll_lock.release()

    def acknowledge(self, event):
        """Explicitly discard an event after processing; never authorizes execution."""
        with self._lock:
            sequence = event.get('sequence')
            if type(sequence) is not int or sequence <= 0:
                raise ValueError('Only delivered events can be acknowledged')
            if sequence <= self._cursor:
                return
            if self._received.get(sequence) != event:
                raise ValueError('Only unchanged delivered events can be acknowledged')
            self._cursor = sequence
            self._received = {seq: value for seq, value in self._received.items() if seq > sequence}

    def apply(self, event, commit):
        """Validate, atomically commit in authoritative UI, then acknowledge.

        False means duplicate/stale/expired/invalid decision, deliberately discarded.
        No event text is executed; Allow-hold is enforced by Orbtile, never fabricated.
        """
        with self._lock:
            sequence = event.get('sequence')
            if type(sequence) is not int or sequence <= 0:
                raise ValueError('Only delivered events can be applied')
            if sequence <= self._cursor:
                return False
            if self._received.get(sequence) != event:
                raise ValueError('Only unchanged delivered events can be applied')
            kind = event.get('type')
            key = (event.get('sessionId'), event.get('host'))
            pending = self._pending.get(key)
            valid = kind == 'tap'
            if kind in ('approval', 'question'):
                expiry = event.get('expiresAt')
                valid = (pending is not None and pending[0] == kind
                         and pending[1]['id'] == event.get('requestId')
                         and type(expiry) in (int, float) and math.isfinite(expiry)
                         and time.time() < min(expiry, pending[2]))
                if valid and kind == 'approval':
                    valid = event.get('decision') in ('allow', 'deny')
                if valid and kind == 'question':
                    valid = _valid_picks(pending[1]['questions'], event.get('picks'))
            if valid:
                self._commit(commit, copy.deepcopy(event))
                if kind != 'tap':
                    self._pending.pop(key, None)
                    if key in self._sessions:
                        self._sessions[key][kind] = None
            self.acknowledge(event)
            return valid

    @staticmethod
    def _commit(callback, *args):
        if inspect.iscoroutinefunction(callback):
            raise TypeError('Use a synchronous atomic UI callback')
        result = callback(*args)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError('UI callback returned an awaitable; use a synchronous transaction')
        return result

    def answer_locally(self, session_id, commit, host='local', *, request_id=None):
        """Commit an own-screen answer and retire the id under the same decision lock.

        Local retirement precedes network clearing, so delayed keypad answers are safe
        even if the clear fails. Retry the null update separately after reconciling.
        """
        with self._lock:
            pending = self._pending.get((session_id, host))
            if pending is None or (request_id is not None and pending[1]['id'] != request_id):
                return False
            self._commit(commit)
            self._pending.pop((session_id, host), None)
            return self.upsert_session(session_id, host=host, approval=None, question=None)

    def events(self, wait_seconds=2):
        """Iterator with manual commit/ack and unchanged cursor on transport failures."""
        while not self._stop.is_set():
            try:
                reply = self.poll_events(wait_seconds)
            except TransportError:
                if self._stop.wait(0.25):
                    return
                continue
            except APIError as error:
                if error.code not in ('poll_limit', 'rate_limited', 'limit_exceeded'):
                    raise
                if self._stop.wait(error.retry_after if error.retry_after is not None else 1):
                    return
                continue
            for event in reply.get('events', []):
                yield event
            if not reply.get('events') and self._stop.wait(0.05):
                return

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(self.timeout + 1)
        with self._lock:
            if self._connected:
                try:
                    self._request('DELETE', self._path())
                finally:
                    self._connected = False
                    self._adapter_token = None
                    self._reset_lease()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


class AsyncClient:
    """asyncio facade; stdlib worker threads keep Unix HTTP off the event loop.

    Callbacks to apply()/answer_locally() are synchronous atomic UI transactions,
    executed in a worker thread. Do not pass coroutine callbacks.
    """
    def __init__(self, *args, **kwargs):
        self._client = Client(*args, **kwargs)

    @property
    def cursor(self):
        return self._client.cursor

    @property
    def info(self):
        return self._client.info

    @property
    def capabilities(self):
        return self._client.capabilities

    @property
    def registration_reply(self):
        return self._client.registration_reply

    @property
    def heartbeat_error(self):
        return self._client.heartbeat_error

    async def connect(self):
        return await asyncio.to_thread(self._client.connect)

    async def verify(self):
        return await asyncio.to_thread(self._client.verify)

    async def register(self):
        return await asyncio.to_thread(self._client.register)

    async def heartbeat(self):
        return await asyncio.to_thread(self._client.heartbeat)

    async def upsert_session(self, session_id, **fields):
        return await asyncio.to_thread(self._client.upsert_session, session_id, **fields)

    @property
    def reconciliation_required(self):
        return self._client.reconciliation_required

    @property
    def lease_generation(self):
        return self._client.lease_generation

    async def reconcile(self):
        return await asyncio.to_thread(self._client.reconcile)

    async def request_approval(self, session_id, request, **fields):
        return await asyncio.to_thread(self._client.request_approval, session_id, request, **fields)

    async def ask_question(self, session_id, request, **fields):
        return await asyncio.to_thread(self._client.ask_question, session_id, request, **fields)

    async def replace_sessions(self, sessions):
        return await asyncio.to_thread(self._client.replace_sessions, sessions)

    async def remove_session(self, session_id, host='local'):
        return await asyncio.to_thread(self._client.remove_session, session_id, host)

    async def poll_events(self, wait_seconds=2):
        return await asyncio.to_thread(self._client.poll_events, wait_seconds)

    async def acknowledge(self, event):
        return await asyncio.to_thread(self._client.acknowledge, event)

    async def apply(self, event, commit):
        if asyncio.iscoroutinefunction(commit):
            raise TypeError('Use a synchronous atomic UI callback')
        return await asyncio.to_thread(self._client.apply, event, commit)

    async def answer_locally(self, session_id, commit, host='local', *, request_id=None):
        if asyncio.iscoroutinefunction(commit):
            raise TypeError('Use a synchronous atomic UI callback')
        return await asyncio.to_thread(self._client.answer_locally, session_id, commit, host, request_id=request_id)

    async def events(self, wait_seconds=2):
        while not self._client._stop.is_set():
            try:
                reply = await self.poll_events(wait_seconds)
            except TransportError:
                await asyncio.sleep(0.25)
                continue
            except APIError as error:
                if error.code not in ('poll_limit', 'rate_limited', 'limit_exceeded'):
                    raise
                await asyncio.sleep(error.retry_after if error.retry_after is not None else 1)
                continue
            for event in reply.get('events', []):
                yield event
            if not reply.get('events'):
                await asyncio.sleep(0.05)

    async def close(self):
        return await asyncio.to_thread(self._client.close)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()
