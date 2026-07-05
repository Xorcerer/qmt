def accept_new_clients(runtime, max_accepts_per_tick, now, log):
    listener = runtime.listener
    if listener is None:
        return
    accept_count = 0
    while accept_count < max_accepts_per_tick:
        try:
            client_socket, address = listener.accept()
        except BlockingIOError:
            break
        except Exception:
            runtime.state['last_error_at'] = now()
            return
        client_socket.setblocking(False)
        client_key = '%s:%s:%s' % (address[0], address[1], now())
        runtime.clients[client_key] = {
            'socket': client_socket,
            'address': address,
            'mode': 'http',
            'request_bytes': b'',
            'response_bytes': b'',
            'response_offset': 0,
            'ws_handshake_complete': False,
            'ws_close_after_send': False,
            'last_quote_sequence': -1,
            'initial_push_sent': False,
            'created_at': now(),
            'last_active_at': now(),
        }
        accept_count += 1
    runtime.state['active_client_count'] = len(runtime.clients)


def consume_ws_frames(client, queue_client_response, build_ws_close_frame, build_ws_pong_frame):
    while True:
        payload = client['request_bytes']
        if len(payload) < 2:
            return True
        first = payload[0]
        second = payload[1]
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        payload_length = second & 0x7F
        index = 2
        if payload_length == 126:
            if len(payload) < index + 2:
                return True
            payload_length = (payload[index] << 8) | payload[index + 1]
            index += 2
        elif payload_length == 127:
            if len(payload) < index + 8:
                return True
            payload_length = 0
            for offset in range(8):
                payload_length = (payload_length << 8) | payload[index + offset]
            index += 8
        if masked:
            if len(payload) < index + 4:
                return True
            mask_key = payload[index:index + 4]
            index += 4
        else:
            mask_key = None
        if len(payload) < index + payload_length:
            return True
        frame_payload = payload[index:index + payload_length]
        client['request_bytes'] = payload[index + payload_length:]
        if mask_key is not None:
            frame_payload = bytes(frame_payload[offset] ^ mask_key[offset % 4] for offset in range(payload_length))
        if opcode == 8:
            queue_client_response(client, build_ws_close_frame())
            client['ws_close_after_send'] = True
            return True
        if opcode == 9:
            queue_client_response(client, build_ws_pong_frame(frame_payload))
            continue
        if opcode in (10, 1, 2):
            continue
        return False


def recv_from_client(
    client,
    max_reads_per_client,
    max_request_size,
    now,
    build_json_response,
    parse_http_request,
    build_websocket_handshake_response,
    build_http_response,
    consume_ws_frames_fn,
):
    read_count = 0
    while read_count < max_reads_per_client:
        try:
            chunk = client['socket'].recv(4096)
        except BlockingIOError:
            return True
        except Exception:
            return False
        if not chunk:
            return False
        client['request_bytes'] += chunk
        client['last_active_at'] = now()
        if len(client['request_bytes']) > max_request_size:
            client['response_bytes'] = build_json_response(413, {'error': 'request_too_large'})
            client['response_offset'] = 0
            return True
        if client['mode'] == 'ws':
            if not consume_ws_frames_fn(client):
                return False
            read_count += 1
            continue
        if b'\r\n\r\n' in client['request_bytes']:
            request = parse_http_request(client['request_bytes'])
            handshake, is_websocket = build_websocket_handshake_response(request)
            if is_websocket:
                client['mode'] = 'ws'
                client['ws_handshake_complete'] = False
                client['response_bytes'] = handshake
                client['response_offset'] = 0
                client['request_bytes'] = b''
                return True
            if handshake is not None:
                client['response_bytes'] = handshake
                client['response_offset'] = 0
                client['request_bytes'] = b''
                return True
            client['response_bytes'] = build_http_response(client['request_bytes'])
            client['response_offset'] = 0
            return True
        read_count += 1
    return True


def send_to_client(client, now):
    response_bytes = client['response_bytes']
    if not response_bytes:
        return True
    try:
        sent = client['socket'].send(response_bytes[client['response_offset']:])
    except BlockingIOError:
        return True
    except Exception:
        return False
    if sent <= 0:
        return False
    client['response_offset'] += sent
    client['last_active_at'] = now()
    if client['response_offset'] >= len(response_bytes):
        client['response_bytes'] = b''
        client['response_offset'] = 0
        if client['mode'] != 'ws':
            return False
        if not client['ws_handshake_complete']:
            client['ws_handshake_complete'] = True
            return True
        if client['ws_close_after_send']:
            return False
        return True
    return True


def push_ws_updates(runtime, build_quotes_payload, queue_client_response, build_ws_json_frame):
    snapshot = build_quotes_payload()
    for client in list(runtime.clients.values()):
        if client['mode'] != 'ws':
            continue
        if not client['ws_handshake_complete']:
            continue
        if client['response_bytes']:
            continue
        if client['initial_push_sent'] and client['last_quote_sequence'] == runtime.quote_sequence:
            continue
        queue_client_response(client, build_ws_json_frame({
            'type': 'quote_snapshot',
            'quote_sequence': snapshot['quote_sequence'],
            'quote_count': snapshot['quote_count'],
            'quote_symbols': snapshot['quote_symbols'],
            'quotes': snapshot['quotes'],
            'last_quote_at': snapshot['last_quote_at'],
        }))
        client['last_quote_sequence'] = runtime.quote_sequence
        client['initial_push_sent'] = True


def poll_clients(runtime, client_timeout_seconds, now, recv_from_client_fn, send_to_client_fn, close_client):
    current_time = now()
    to_close = []
    for client_key, client in list(runtime.clients.items()):
        if current_time - client['last_active_at'] > client_timeout_seconds:
            to_close.append(client_key)
            continue
        if not client['response_bytes']:
            if not recv_from_client_fn(client):
                to_close.append(client_key)
                continue
        if client['response_bytes']:
            if not send_to_client_fn(client):
                to_close.append(client_key)
    for client_key in to_close:
        close_client(client_key)
