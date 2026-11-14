"""External HTTP/WebSocket gateway for the QMT shared-memory bridge.

Run with a normal Python 3.11 installation. QMT never imports this module.
"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import select
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from server_http_utils import (
    build_websocket_handshake_response,
    build_ws_close_frame,
    build_ws_json_frame,
    build_ws_pong_frame,
    is_host_allowed,
    is_request_authorized,
)
from shared_memory_protocol import (
    BridgeError,
    BridgeTimeout,
    DEFAULT_NAME,
    SharedMemoryChannel,
)


WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
CORS_ALLOW_HEADERS = 'Authorization, X-QMT-Token, Content-Type, Sec-WebSocket-Protocol'
CORS_ALLOW_METHODS = 'GET, OPTIONS'
LONG_REQUEST_PATHS = {
    '/candles-bulk', '/divid-factors', '/instrument-bulk', '/turnover-rate',
    '/total-share', '/trading-dates', '/sector',
}


def allow_mapping_user(mapping_name, username):
    """Grant the QMT user access to a mapping created by a Windows service."""
    if sys.platform != 'win32' or not username:
        return
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    sid_size = wintypes.DWORD()
    domain_size = wintypes.DWORD()
    sid_use = wintypes.DWORD()
    lookup = advapi32.LookupAccountNameW
    lookup.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    lookup.restype = wintypes.BOOL
    lookup(None, username, None, ctypes.byref(sid_size), None,
           ctypes.byref(domain_size), ctypes.byref(sid_use))
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value)
    if not lookup(None, username, sid, ctypes.byref(sid_size), domain,
                  ctypes.byref(domain_size), ctypes.byref(sid_use)):
        raise ctypes.WinError()
    sid_text = wintypes.LPWSTR()
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    if not convert_sid(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError()
    try:
        sddl = 'D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)' % sid_text.value
    finally:
        kernel32.LocalFree(sid_text)
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        raise ctypes.WinError()
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(descriptor, ctypes.byref(dacl_present),
                        ctypes.byref(dacl), ctypes.byref(dacl_defaulted)):
            raise ctypes.WinError()
        set_security = advapi32.SetNamedSecurityInfoW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p]
        set_security.restype = wintypes.DWORD
        # SE_KERNEL_OBJECT=6, DACL_SECURITY_INFORMATION=4.
        result = set_security(mapping_name, 6, 4, None, None, dacl, None)
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        kernel32.LocalFree(descriptor)


def normalize_auth_token(value):
    if value in (None, ''):
        return None
    value = str(value).strip()
    return value or None


class ConfigStore(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._payload = {}
        self._mtime = None
        self._revision = 0
        self.reload(force=True)

    def reload(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
            if not force and mtime == self._mtime:
                return False
            with open(self.path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError('QMT config must be a JSON object')
        except Exception:
            if self._mtime is None:
                print('[qmt-gateway] config unavailable; all requests will be denied', flush=True)
            return False
        with self._lock:
            self._payload = payload
            self._mtime = mtime
            self._revision += 1
        print('[qmt-gateway] config loaded revision=%s' % self._revision, flush=True)
        return True

    def snapshot(self):
        with self._lock:
            return dict(self._payload), self._revision


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'QMTSharedMemoryGateway/1.0'
    channel = None
    config_store = None
    allowed_hosts = ()

    def log_message(self, fmt, *args):
        sys.stdout.write('[qmt-gateway] %s - %s\n' % (self.address_string(), fmt % args))
        sys.stdout.flush()

    def _request_metadata(self):
        return {
            'method': self.command.upper(),
            'target': self.path,
            'headers': {key.lower(): value for key, value in self.headers.items()},
        }

    def _authorized(self):
        config, _ = self.config_store.snapshot()
        token = normalize_auth_token(config.get('auth_token'))
        authorized, _ = is_request_authorized(
            self._request_metadata(), token, normalize_auth_token,
        )
        return authorized

    def _send_payload(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_OPTIONS(self):
        if not is_host_allowed(self._request_metadata(), self.allowed_hosts):
            self._send_payload(403, {'error': 'host_not_allowed'})
            return
        self._send_payload(200, {'status': 'ok'})

    def do_GET(self):
        metadata = self._request_metadata()
        if not is_host_allowed(metadata, self.allowed_hosts):
            self._send_payload(403, {'error': 'host_not_allowed'})
            return
        if not self._authorized():
            self._send_payload(401, {'error': 'unauthorized'})
            return
        if (urlparse(self.path).path or '/') == '/ws':
            self._handle_websocket(metadata)
            return
        if self.command != 'GET':
            self._send_payload(405, {'error': 'method_not_allowed', 'method': self.command})
            return
        if not self.channel.qmt_is_alive():
            self._send_payload(503, {'error': 'qmt_unavailable'})
            return
        path = urlparse(self.path).path or '/'
        timeout = 130.0 if path in LONG_REQUEST_PATHS else 15.0
        try:
            status, payload = self.channel.request(
                {'method': 'GET', 'target': self.path}, timeout=timeout,
            )
            self._send_payload(status, payload)
        except BridgeTimeout:
            self._send_payload(503, {'error': 'qmt_timeout'})
        except BridgeError:
            self._send_payload(503, {'error': 'qmt_unavailable'})
        except Exception:
            self.log_error('request bridge failed: %s', sys.exc_info()[1])
            self._send_payload(500, {'error': 'gateway_internal_error'})

    def do_POST(self):
        self._send_payload(405, {'error': 'method_not_allowed', 'method': self.command})

    def _handle_websocket(self, metadata):
        config, _ = self.config_store.snapshot()
        token = normalize_auth_token(config.get('auth_token'))
        handshake, is_websocket = build_websocket_handshake_response(
            metadata, WEBSOCKET_GUID, token, normalize_auth_token,
            CORS_ALLOW_HEADERS, CORS_ALLOW_METHODS, self.allowed_hosts, '',
        )
        if not is_websocket:
            self._send_payload(400, {'error': 'invalid_websocket_request'})
            return
        self.connection.sendall(handshake)
        self.close_connection = True
        self.connection.settimeout(0.1)
        last_sequence = -1
        try:
            while True:
                sequence, snapshot = self.channel.read_quotes()
                if snapshot is not None and sequence != last_sequence:
                    frame = {
                        'type': 'quote_snapshot',
                        'quote_sequence': snapshot.get('quote_sequence'),
                        'quote_count': snapshot.get('quote_count'),
                        'quote_symbols': snapshot.get('quote_symbols'),
                        'quotes': snapshot.get('quotes'),
                        'last_quote_at': snapshot.get('last_quote_at'),
                    }
                    self.connection.sendall(build_ws_json_frame(frame))
                    last_sequence = sequence
                readable, _, _ = select.select([self.connection], [], [], 0.05)
                if readable:
                    opcode, payload = self._read_websocket_frame()
                    if opcode is None:
                        break
                    if opcode == 8:
                        self.connection.sendall(build_ws_close_frame())
                        break
                    if opcode == 9:
                        self.connection.sendall(build_ws_pong_frame(payload))
        except (OSError, socket.timeout):
            pass

    def _read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                raise OSError('websocket closed')
            data.extend(chunk)
        return bytes(data)

    def _read_websocket_frame(self):
        first = self._read_exact(2)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read_exact(8))[0]
        if length > 1024 * 1024:
            raise OSError('websocket frame too large')
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if mask:
            payload = bytes(value ^ mask[index % 4]
                            for index, value in enumerate(payload))
        return opcode, payload


def config_broadcast_loop(channel, config_store, stop_event):
    last_revision = -1
    pending = False
    was_alive = False
    while not stop_event.is_set():
        config_store.reload()
        config, revision = config_store.snapshot()
        alive = channel.qmt_is_alive()
        if alive and not was_alive:
            # A QMT process that starts after the gateway needs a fresh config,
            # even if an earlier attempt timed out or was rejected during reload.
            pending = True
        if pending and not alive:
            was_alive = False
            stop_event.wait(0.5)
            continue
        if revision != last_revision or pending:
            try:
                status, payload = channel.request({
                    'method': 'CONFIG',
                    'target': '/__bridge/config',
                    'config': config,
                }, timeout=3.0)
                if status != 200 or payload.get('error'):
                    pending = True
                    print('[qmt-gateway] QMT rejected config; retrying', flush=True)
                else:
                    last_revision = revision
                    pending = False
                    print('[qmt-gateway] QMT config applied revision=%s' % revision, flush=True)
            except BridgeTimeout:
                pending = True
            except BridgeError:
                pending = True
                stop_event.wait(0.5)
        was_alive = alive
        stop_event.wait(0.5)


def main(argv=None):
    parser = argparse.ArgumentParser(description='QMT shared-memory HTTP gateway')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--config', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'server_config.json'))
    parser.add_argument('--shm-name', default=DEFAULT_NAME)
    parser.add_argument('--mapping-user', default='',
                        help='Windows account allowed to attach from QMT')
    args = parser.parse_args(argv)

    channel = SharedMemoryChannel(name=args.shm_name)
    channel.open(initialize=True)
    allow_mapping_user(args.shm_name, args.mapping_user)
    config_store = ConfigStore(args.config)
    GatewayHandler.channel = channel
    GatewayHandler.config_store = config_store
    GatewayHandler.allowed_hosts = (
        '127.0.0.1:%s' % args.port, 'localhost:%s' % args.port,
        '127.0.0.1', 'localhost',
    )

    stop_event = threading.Event()
    config_thread = threading.Thread(
        target=config_broadcast_loop,
        args=(channel, config_store, stop_event),
        name='qmt-config-broadcaster',
    )
    config_thread.daemon = True
    config_thread.start()

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.daemon_threads = True
    print('[qmt-gateway] listening on http://%s:%s shared_memory=%s' %
          (args.host, args.port, args.shm_name), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        channel.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
