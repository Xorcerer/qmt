"""Cross-process shared-memory transport for QMT and the external gateway.

Compatible with Python 3.6+ and uses only the standard library. On Windows the
mapping is backed by the page file, not a disk file. The external process owns
slot allocation; the QMT process is the only request consumer.
"""

import json
import mmap
import struct
import sys
import threading
import time


MAGIC = b'QMTSHM01'
VERSION = 1
DEFAULT_NAME = r'Global\QmtAlgoMonitorBridge'
DEFAULT_SLOT_COUNT = 8
DEFAULT_SLOT_PAYLOAD_SIZE = 2 * 1024 * 1024
DEFAULT_QUOTE_PAYLOAD_SIZE = 2 * 1024 * 1024

STATE_FREE = 0
STATE_READY = 1
STATE_PROCESSING = 2
STATE_DONE = 3
STATE_CLAIMED = 4

# magic, version, total bytes, slots, slot payload bytes, quote payload bytes,
# generation, QMT heartbeat (Unix timestamp)
GLOBAL_HEADER = struct.Struct('<8sIIIIIQd')
# request id, state, request bytes, response bytes, HTTP status
SLOT_HEADER = struct.Struct('<QIIII')
# publication sequence, JSON bytes
QUOTE_HEADER = struct.Struct('<QI')


class BridgeError(Exception):
    pass


class BridgeTimeout(BridgeError):
    pass


class BridgeCapacityError(BridgeError):
    pass


class SharedMemoryChannel(object):
    def __init__(self, name=DEFAULT_NAME, slot_count=DEFAULT_SLOT_COUNT,
                 slot_payload_size=DEFAULT_SLOT_PAYLOAD_SIZE,
                 quote_payload_size=DEFAULT_QUOTE_PAYLOAD_SIZE):
        self.name = name
        self.slot_count = int(slot_count)
        self.slot_payload_size = int(slot_payload_size)
        self.quote_payload_size = int(quote_payload_size)
        self.slot_stride = SLOT_HEADER.size + self.slot_payload_size
        self.slots_offset = GLOBAL_HEADER.size
        self.quote_offset = self.slots_offset + self.slot_count * self.slot_stride
        self.quote_payload_offset = self.quote_offset + QUOTE_HEADER.size
        self.total_size = self.quote_payload_offset + self.quote_payload_size
        self.mm = None
        self._lock = threading.Lock()
        self._next_request_id = 1
        self._abandoned = set()

    def open(self, initialize=False):
        if self.mm is not None:
            return
        # fileno=-1 creates an anonymous, named page-file mapping on Windows.
        if sys.platform == 'win32':
            self.mm = mmap.mmap(-1, self.total_size, tagname=self.name,
                                access=mmap.ACCESS_WRITE)
        else:
            # Same-process unit tests on development hosts; production is Windows.
            self.mm = mmap.mmap(-1, self.total_size, access=mmap.ACCESS_WRITE)
        if initialize:
            self.mm.seek(0)
            self.mm.write(b'\x00' * self.total_size)
            GLOBAL_HEADER.pack_into(
                self.mm, 0, MAGIC, VERSION, self.total_size, self.slot_count,
                self.slot_payload_size, self.quote_payload_size, int(time.time()), 0.0,
            )
            return
        values = GLOBAL_HEADER.unpack_from(self.mm, 0)
        expected = (
            MAGIC, VERSION, self.total_size, self.slot_count,
            self.slot_payload_size, self.quote_payload_size,
        )
        if values[:6] != expected:
            self.mm.close()
            self.mm = None
            raise BridgeError('shared-memory layout is not initialized or is incompatible')

    def close(self):
        if self.mm is not None:
            self.mm.close()
            self.mm = None

    def _slot_offset(self, index):
        return self.slots_offset + index * self.slot_stride

    def update_qmt_heartbeat(self):
        # The final field in GLOBAL_HEADER is the QMT heartbeat.
        struct.pack_into('<d', self.mm, GLOBAL_HEADER.size - 8, time.time())

    def qmt_heartbeat(self):
        return struct.unpack_from('<d', self.mm, GLOBAL_HEADER.size - 8)[0]

    def qmt_is_alive(self, max_age=5.0):
        heartbeat = self.qmt_heartbeat()
        return heartbeat > 0 and time.time() - heartbeat <= max_age

    def _reclaim_abandoned_locked(self):
        for index in range(self.slot_count):
            offset = self._slot_offset(index)
            request_id, state = struct.unpack_from('<QI', self.mm, offset)
            if state == STATE_DONE and request_id in self._abandoned:
                self._abandoned.discard(request_id)
                struct.pack_into('<I', self.mm, offset + 8, STATE_FREE)

    def request(self, payload, timeout=10.0):
        """Send one request and return (HTTP status, decoded response)."""
        request_bytes = json.dumps(payload, ensure_ascii=False,
                                   separators=(',', ':')).encode('utf-8')
        if len(request_bytes) > self.slot_payload_size:
            raise BridgeCapacityError('request exceeds shared-memory slot capacity')

        deadline = time.time() + timeout
        while True:
            with self._lock:
                self._reclaim_abandoned_locked()
                slot_index = None
                for index in range(self.slot_count):
                    state = struct.unpack_from('<I', self.mm, self._slot_offset(index) + 8)[0]
                    if state == STATE_FREE:
                        slot_index = index
                        break
                if slot_index is not None:
                    request_id = self._next_request_id
                    self._next_request_id += 1
                    offset = self._slot_offset(slot_index)
                    struct.pack_into('<I', self.mm, offset + 8, STATE_CLAIMED)
                    payload_offset = offset + SLOT_HEADER.size
                    self.mm[payload_offset:payload_offset + len(request_bytes)] = request_bytes
                    # Publish only after the ID, payload and length are complete.
                    SLOT_HEADER.pack_into(self.mm, offset, request_id, STATE_READY,
                                          len(request_bytes), 0, 0)
                    break
            if time.time() >= deadline:
                raise BridgeTimeout('timed out waiting for a shared-memory slot')
            time.sleep(0.001)

        while True:
            response_id, state, _, response_length, status = SLOT_HEADER.unpack_from(self.mm, offset)
            if state == STATE_DONE and response_id == request_id:
                response_bytes = bytes(self.mm[payload_offset:payload_offset + response_length])
                try:
                    response = json.loads(response_bytes.decode('utf-8'))
                except Exception:
                    response = {'error': 'invalid_bridge_response'}
                    status = 500
                with self._lock:
                    struct.pack_into('<I', self.mm, offset + 8, STATE_FREE)
                return status, response
            if time.time() >= deadline:
                # Do not reclaim a slot that QMT may still be writing. A later
                # request reclaims it after observing STATE_DONE.
                with self._lock:
                    self._abandoned.add(request_id)
                raise BridgeTimeout('QMT request timed out')
            time.sleep(0.001)

    def process_requests(self, handler, on_error=None):
        """QMT side: process every currently ready slot once."""
        self.update_qmt_heartbeat()
        processed = 0
        for index in range(self.slot_count):
            offset = self._slot_offset(index)
            request_id, state, request_length = struct.unpack_from('<QII', self.mm, offset)
            if state != STATE_READY:
                continue
            struct.pack_into('<I', self.mm, offset + 8, STATE_PROCESSING)
            payload_offset = offset + SLOT_HEADER.size
            status = 500
            try:
                if request_length > self.slot_payload_size:
                    raise BridgeCapacityError('invalid request length')
                request_bytes = bytes(self.mm[payload_offset:payload_offset + request_length])
                request = json.loads(request_bytes.decode('utf-8'))
                status, response = handler(request)
            except Exception:
                if on_error is not None:
                    on_error()
                status, response = 500, {'error': 'internal_error'}
            try:
                response_bytes = json.dumps(response, ensure_ascii=False,
                                            separators=(',', ':')).encode('utf-8')
                if len(response_bytes) > self.slot_payload_size:
                    status = 500
                    response_bytes = b'{"error":"response_too_large"}'
                self.mm[payload_offset:payload_offset + len(response_bytes)] = response_bytes
                # Response state is published last; readers never parse partial JSON.
                SLOT_HEADER.pack_into(self.mm, offset, request_id, STATE_DONE,
                                      request_length, len(response_bytes), status)
                processed += 1
            except Exception:
                if on_error is not None:
                    on_error()
        self.update_qmt_heartbeat()
        return processed

    def publish_quotes(self, payload):
        quote_bytes = json.dumps(payload, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8')
        if len(quote_bytes) > self.quote_payload_size:
            raise BridgeCapacityError('quote snapshot exceeds shared-memory capacity')
        sequence = QUOTE_HEADER.unpack_from(self.mm, self.quote_offset)[0] + 1
        end = self.quote_payload_offset + len(quote_bytes)
        self.mm[self.quote_payload_offset:end] = quote_bytes
        QUOTE_HEADER.pack_into(self.mm, self.quote_offset, sequence, len(quote_bytes))
        return sequence

    def read_quotes(self):
        # QMT increments the sequence for every publication. Re-reading the
        # header detects a publication that overlapped this read.
        sequence, length = QUOTE_HEADER.unpack_from(self.mm, self.quote_offset)
        if sequence == 0:
            return sequence, None
        if length > self.quote_payload_size:
            return sequence, None
        end = self.quote_payload_offset + length
        quote_bytes = bytes(self.mm[self.quote_payload_offset:end])
        if QUOTE_HEADER.unpack_from(self.mm, self.quote_offset) != (sequence, length):
            return sequence, None
        try:
            return sequence, json.loads(quote_bytes.decode('utf-8'))
        except Exception:
            return sequence, None
