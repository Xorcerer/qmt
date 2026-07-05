import base64
import hashlib
import json
from urllib.parse import urlparse


def build_json_response(status_code, payload, allow_headers, allow_methods, extra_headers=None):
    reason_map = {
        200: 'OK',
        204: 'No Content',
        401: 'Unauthorized',
        404: 'Not Found',
        405: 'Method Not Allowed',
        413: 'Payload Too Large',
        500: 'Internal Server Error',
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    header_lines = [
        'HTTP/1.1 %s %s' % (status_code, reason_map.get(status_code, 'OK')),
        'Content-Type: application/json; charset=utf-8',
        'Content-Length: %s' % len(body),
        'Connection: close',
    ]
    if extra_headers:
        header_lines.extend(extra_headers)
    header_lines.extend([
        'Access-Control-Allow-Origin: *',
        'Access-Control-Allow-Headers: %s' % allow_headers,
        'Access-Control-Allow-Methods: %s' % allow_methods,
        '',
        '',
    ])
    return '\r\n'.join(header_lines).encode('utf-8') + body


def extract_request_token(headers, normalize_auth_token):
    token = normalize_auth_token(headers.get('x-qmt-token'))
    if token is not None:
        return token, None
    authorization = headers.get('authorization')
    if authorization:
        prefix = 'bearer '
        lowered = authorization.lower()
        if lowered.startswith(prefix):
            token = normalize_auth_token(authorization[len(prefix):])
            if token is not None:
                return token, None
    protocol_header = headers.get('sec-websocket-protocol')
    if protocol_header:
        for item in protocol_header.split(','):
            protocol = item.strip()
            if not protocol.startswith('qmt-token.'):
                continue
            token = normalize_auth_token(protocol[len('qmt-token.'):])
            if token is not None:
                return token, protocol
    return None, None


def is_request_authorized(request, configured_auth_token, normalize_auth_token):
    if not configured_auth_token:
        return True, None
    provided_token, ws_protocol = extract_request_token(request['headers'], normalize_auth_token)
    if provided_token is None:
        return False, None
    return provided_token == configured_auth_token, ws_protocol


def build_unauthorized_response(allow_headers, allow_methods):
    return build_json_response(401, {'error': 'unauthorized'}, allow_headers, allow_methods)


def parse_http_request(request_bytes):
    header_end = request_bytes.find(b'\r\n\r\n')
    if header_end < 0:
        return None
    request_head = request_bytes[:header_end].decode('iso-8859-1', 'replace')
    lines = request_head.split('\r\n')
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    headers = {}
    for line in lines[1:]:
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        headers[key.strip().lower()] = value.strip()
    return {
        'method': parts[0].upper(),
        'target': parts[1],
        'headers': headers,
    }


def build_websocket_handshake_response(
    request,
    websocket_guid,
    configured_auth_token,
    normalize_auth_token,
    allow_headers,
    allow_methods,
):
    if request is None:
        return None, False
    if request['method'] != 'GET':
        return None, False
    parsed = urlparse(request['target'])
    if (parsed.path or '/') != '/ws':
        return None, False
    headers = request['headers']
    if headers.get('upgrade', '').lower() != 'websocket':
        return None, False
    websocket_key = headers.get('sec-websocket-key')
    if not websocket_key:
        return None, False
    authorized, ws_protocol = is_request_authorized(request, configured_auth_token, normalize_auth_token)
    if not authorized:
        return build_unauthorized_response(allow_headers, allow_methods), False
    accept_source = (websocket_key + websocket_guid).encode('utf-8')
    accept_value = base64.b64encode(hashlib.sha1(accept_source).digest()).decode('ascii')
    response_lines = [
        'HTTP/1.1 101 Switching Protocols',
        'Upgrade: websocket',
        'Connection: Upgrade',
        'Sec-WebSocket-Accept: %s' % accept_value,
    ]
    if ws_protocol is not None:
        response_lines.append('Sec-WebSocket-Protocol: %s' % ws_protocol)
    response_lines.extend(['', ''])
    return '\r\n'.join(response_lines).encode('utf-8'), True


def build_ws_frame(payload_bytes, opcode=1):
    payload_length = len(payload_bytes)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    if payload_length < 126:
        header.append(payload_length)
    elif payload_length < 65536:
        header.append(126)
        header.extend([(payload_length >> 8) & 0xFF, payload_length & 0xFF])
    else:
        header.append(127)
        for shift in [56, 48, 40, 32, 24, 16, 8, 0]:
            header.append((payload_length >> shift) & 0xFF)
    return bytes(header) + payload_bytes


def build_ws_json_frame(payload):
    return build_ws_frame(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def build_ws_close_frame():
    return build_ws_frame(b'', opcode=8)


def build_ws_pong_frame(payload_bytes):
    return build_ws_frame(payload_bytes, opcode=10)


def queue_client_response(client, payload_bytes):
    if client['response_bytes']:
        return
    client['response_bytes'] = payload_bytes
    client['response_offset'] = 0
