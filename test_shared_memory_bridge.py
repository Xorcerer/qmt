import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared_memory_protocol import BridgeTimeout, SharedMemoryChannel


class SharedMemoryChannelTest(unittest.TestCase):
    def setUp(self):
        self.channel = SharedMemoryChannel(
            name='QmtBridgeTest', slot_count=4,
            slot_payload_size=64 * 1024, quote_payload_size=64 * 1024,
        )
        self.channel.open(initialize=True)
        self.stop_event = threading.Event()
        self.worker = None

    def tearDown(self):
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(2)
        self.channel.close()

    @staticmethod
    def handler(request):
        if request.get('method') == 'CONFIG':
            return 200, {'status': 'ok', 'config': request.get('config')}
        if request.get('target') == '/slow':
            time.sleep(0.1)
        return 200, {'target': request.get('target'), 'text': '中文'}

    def start_worker(self):
        def run():
            while not self.stop_event.is_set():
                self.channel.process_requests(self.handler)
                time.sleep(0.001)
        self.worker = threading.Thread(target=run)
        self.worker.daemon = True
        self.worker.start()

    def test_request_response(self):
        self.start_worker()
        status, payload = self.channel.request({'method': 'GET', 'target': '/health'})
        self.assertEqual(200, status)
        self.assertEqual('/health', payload['target'])
        self.assertEqual('中文', payload['text'])

    def test_concurrent_requests(self):
        self.start_worker()
        errors = []

        def call(index):
            try:
                status, payload = self.channel.request(
                    {'method': 'GET', 'target': '/item?id=%s' % index}, timeout=5)
                self.assertEqual(200, status)
                self.assertEqual('/item?id=%s' % index, payload['target'])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual([], errors)

    def test_timeout_slot_is_reclaimed_after_completion(self):
        with self.assertRaises(BridgeTimeout):
            self.channel.request({'method': 'GET', 'target': '/slow'}, timeout=0.01)
        self.channel.process_requests(self.handler)
        # The timed-out request is completed and retired before the new request.
        self.start_worker()
        status, payload = self.channel.request(
            {'method': 'GET', 'target': '/health'}, timeout=1)
        self.assertEqual(200, status)
        self.assertEqual('/health', payload['target'])

    def test_config_and_quotes(self):
        status, payload = self._round_trip({
            'method': 'CONFIG', 'target': '/__bridge/config',
            'config': {'account_id': '123', 'quote_symbols': ['000300.SH']},
        })
        self.assertEqual(200, status)
        self.assertEqual('123', payload['config']['account_id'])
        first_sequence = self.channel.publish_quotes({'quote_sequence': 1, 'quotes': []})
        second_sequence = self.channel.publish_quotes({'quote_sequence': 2, 'quotes': [{'lastPrice': 10}]})
        sequence, snapshot = self.channel.read_quotes()
        self.assertGreater(second_sequence, first_sequence)
        self.assertEqual(second_sequence, sequence)
        self.assertEqual(10, snapshot['quotes'][0]['lastPrice'])

    def _round_trip(self, request):
        def send():
            time.sleep(0.02)
            self.channel.process_requests(self.handler)
        thread = threading.Thread(target=send)
        thread.start()
        result = self.channel.request(request, timeout=1)
        thread.join(1)
        return result


if __name__ == '__main__':
    unittest.main()
