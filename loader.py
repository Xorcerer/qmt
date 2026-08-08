# -*- coding: gbk -*-

import importlib.util
import os
import sys
import traceback


def _resolve_watch_dir():
    env_watch_dir = os.environ.get('QMT_WATCH_DIR')
    candidates = []
    if env_watch_dir:
        candidates.append(env_watch_dir)
    candidates.append(r'C:\server')
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        server_file = os.path.join(candidate, 'server.py')
        if os.path.isfile(server_file):
            return candidate
    if env_watch_dir:
        return env_watch_dir
    return r'C:\server'


WATCH_DIR = _resolve_watch_dir()
SERVER_FILE = os.path.join(WATCH_DIR, 'server.py')
SERVER_MODULE_NAME = 'qmt_live_server'
SERVER_NAMESPACE_EXCLUDES = {
    'LoaderState',
    'STATE',
    'WATCH_DIR',
    'SERVER_FILE',
    'SERVER_MODULE_NAME',
    'SERVER_NAMESPACE_EXCLUDES',
    'importlib',
    'os',
    'sys',
    'traceback',
    '_resolve_watch_dir',
    '_normalize_path',
    '_iter_python_files',
    '_build_signature',
    '_needs_reload',
    '_purge_watch_modules',
    '_build_server_namespace',
    '_load_server',
    '_dispatch',
    'init',
    'after_init',
    'handlebar',
    'server_tick',
    'stop',
    'account_callback',
    'order_callback',
    'deal_callback',
    'position_callback',
    'orderError_callback',
    'order_error_callback',
    'credit_opvolume_callback',
    'reload_server',
}


class LoaderState(object):
    def __init__(self):
        self.module = None
        self.signature = None
        self.loading = False


STATE = LoaderState()


def _normalize_path(path):
    return os.path.normcase(os.path.abspath(path))


def _iter_python_files():
    if not os.path.isdir(WATCH_DIR):
        return []
    result = []
    for root, _, names in os.walk(WATCH_DIR):
        for name in names:
            if not name.endswith('.py'):
                continue
            result.append(os.path.join(root, name))
    result.sort()
    return result


def _build_signature():
    signature = []
    for path in _iter_python_files():
        try:
            stat_result = os.stat(path)
            signature.append((path, int(stat_result.st_mtime * 1000000), stat_result.st_size))
        except OSError:
            signature.append((path, None, None))
    return tuple(signature)


def _needs_reload():
    if STATE.module is None:
        return True
    return STATE.signature != _build_signature()


def _purge_watch_modules():
    watch_dir = _normalize_path(WATCH_DIR)
    for name, module in list(sys.modules.items()):
        if name == __name__:
            continue
        module_file = getattr(module, '__file__', None)
        if not module_file:
            continue
        try:
            module_path = _normalize_path(module_file)
        except Exception:
            continue
        if module_path.startswith(watch_dir):
            sys.modules.pop(name, None)


def _build_server_namespace():
    result = {}
    for name, value in globals().items():
        if name in SERVER_NAMESPACE_EXCLUDES:
            continue
        if name.startswith('__'):
            continue
        result[name] = value
    return result


def _load_server(force=False):
    if STATE.loading:
        return STATE.module
    if not force and not _needs_reload():
        return STATE.module
    if not os.path.isfile(SERVER_FILE):
        raise RuntimeError('server.py 不存在: %s' % SERVER_FILE)
    STATE.loading = True
    old_module = STATE.module
    old_signature = STATE.signature
    try:
        if WATCH_DIR not in sys.path:
            sys.path.insert(0, WATCH_DIR)
        _purge_watch_modules()
        spec = importlib.util.spec_from_file_location(SERVER_MODULE_NAME, SERVER_FILE)
        if spec is None or spec.loader is None:
            raise RuntimeError('无法为 server.py 创建加载器')
        module = importlib.util.module_from_spec(spec)
        module.__dict__.update(_build_server_namespace())
        sys.modules[SERVER_MODULE_NAME] = module
        spec.loader.exec_module(module)
        STATE.module = module
        STATE.signature = _build_signature()
        print('[qmt-loader] server reloaded: %s' % SERVER_FILE)
        return module
    except Exception:
        sys.modules.pop(SERVER_MODULE_NAME, None)
        STATE.module = old_module
        STATE.signature = old_signature
        print('[qmt-loader] server reload failed')
        traceback.print_exc()
        return STATE.module
    finally:
        STATE.loading = False


def _dispatch(callback_name, *args):
    module = _load_server()
    if module is None:
        return None
    handler = getattr(module, callback_name, None)
    if callable(handler):
        return handler(*args)
    generic_handler = getattr(module, 'on_event', None)
    if callable(generic_handler):
        return generic_handler(callback_name, *args)
    return None


def init(ContextInfo):
    return _dispatch('init', ContextInfo)


def after_init(ContextInfo):
    return _dispatch('after_init', ContextInfo)


def handlebar(ContextInfo):
    return _dispatch('handlebar', ContextInfo)


def server_tick(ContextInfo):
    return _dispatch('server_tick', ContextInfo)


def stop(ContextInfo):
    return _dispatch('stop', ContextInfo)


def account_callback(ContextInfo, account_info):
    return _dispatch('account_callback', ContextInfo, account_info)


def order_callback(ContextInfo, order_info):
    return _dispatch('order_callback', ContextInfo, order_info)


def deal_callback(ContextInfo, deal_info):
    return _dispatch('deal_callback', ContextInfo, deal_info)


def position_callback(ContextInfo, position_info):
    return _dispatch('position_callback', ContextInfo, position_info)


def orderError_callback(ContextInfo, order_info, error_info):
    return _dispatch('orderError_callback', ContextInfo, order_info, error_info)


def order_error_callback(ContextInfo, order_info, error_info):
    return _dispatch('order_error_callback', ContextInfo, order_info, error_info)


def credit_opvolume_callback(ContextInfo, accid, seq, ret, result):
    return _dispatch('credit_opvolume_callback', ContextInfo, accid, seq, ret, result)


def reload_server(ContextInfo=None):
    return _load_server(force=True)
