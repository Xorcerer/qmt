"""Test-only QMT worker; never installed as a production service."""

import os
import sys
import time

import server


class FakeContext(object):
    barpos = 0
    start = ''
    end = ''
    period = '1d'
    dividend_type = 'none'

    @staticmethod
    def run_time(*args):
        return None


def main():
    mapping_name = os.environ.get('QMT_TEST_SHM_NAME')
    if not mapping_name or mapping_name == server.SHARED_MEMORY_NAME:
        raise RuntimeError('QMT_TEST_SHM_NAME must select an isolated test mapping')
    server.SHARED_MEMORY_NAME = mapping_name
    context = FakeContext()
    server.init(context)
    while True:
        server.server_tick(context)
        time.sleep(0.002)


if __name__ == '__main__':
    sys.exit(main())
