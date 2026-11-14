"""Integration test for two separate Windows processes and the real HTTP gateway."""

import argparse
import asyncio
import concurrent.futures
import json
import urllib.error
import urllib.request

import websockets


def get_json(port, path, token=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    request = urllib.request.Request('http://127.0.0.1:%s%s' % (port, path), headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode('utf-8'))


async def check_websocket(port, token):
    async with websockets.connect(
        'ws://127.0.0.1:%s/ws' % port,
        additional_headers={'Authorization': 'Bearer ' + token},
        open_timeout=5,
    ) as websocket:
        raw = await asyncio.wait_for(websocket.recv(), timeout=5)
        payload = json.loads(raw)
        assert payload['type'] == 'quote_snapshot'
        assert 'quotes' in payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18081)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with open(args.config, 'r', encoding='utf-8') as handle:
        token = json.load(handle)['auth_token']

    try:
        get_json(args.port, '/health')
        raise AssertionError('unauthenticated request unexpectedly succeeded')
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, exc.code

    status, health = get_json(args.port, '/health', token)
    assert status == 200
    assert health['http_mode'] == 'shared_memory'
    assert health['listener_ready'] is True
    assert health['shared_memory_name']
    assert health['account_source'] == 'external_config'

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(get_json, args.port, '/health', token) for _ in range(32)]
        results = [future.result() for future in futures]
        assert all(status == 200 for status, _ in results)

    try:
        status, missing = get_json(args.port, '/does-not-exist', token)
    except urllib.error.HTTPError as exc:
        status = exc.code
        missing = json.loads(exc.read().decode('utf-8'))
    assert status == 404 and missing['error'] == 'not_found'
    asyncio.run(check_websocket(args.port, token))
    print('remote integration passed: auth, 32 concurrent requests, routing, WebSocket')


if __name__ == '__main__':
    main()
