import time
import types


def create_runtime(runtime_module_name, initial_state):
    runtime = types.ModuleType(runtime_module_name)
    runtime.listener = None
    runtime.context_ref = None
    runtime.clients = {}
    runtime.position_index = {}
    runtime.order_index = {}
    runtime.deal_index = {}
    runtime.quote_subscriptions = {}
    runtime.latest_quotes = {}
    runtime.quote_sequence = 0
    runtime.state = dict(initial_state)
    return runtime


def upgrade_runtime(runtime, defaults):
    old_httpd = getattr(runtime, 'httpd', None)
    old_http_thread = getattr(runtime, 'http_thread', None)
    if old_httpd is not None:
        try:
            old_httpd.shutdown()
        except Exception:
            pass
        try:
            old_httpd.server_close()
        except Exception:
            pass
    if old_http_thread is not None and hasattr(old_http_thread, 'is_alive') and old_http_thread.is_alive():
        try:
            old_http_thread.join(2.0)
        except Exception:
            pass
    for name in ['listener', 'context_ref', 'clients', 'position_index', 'order_index', 'deal_index', 'quote_subscriptions', 'latest_quotes', 'quote_sequence']:
        if not hasattr(runtime, name):
            setattr(runtime, name, getattr(defaults, name))
    state = getattr(runtime, 'state', None)
    if not isinstance(state, dict):
        runtime.state = dict(defaults.state)
        return runtime
    for key, value in defaults.state.items():
        if key not in state:
            state[key] = value
    return runtime


def now():
    return time.time()


def log(message):
    print('[qmt-server] %s' % message)


def truncate_text(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + '...'


def make_jsonable(value, depth=0):
    if depth > 3:
        return truncate_text(repr(value), 400)
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', 'replace')
        except Exception:
            return repr(value)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return [make_jsonable(item, depth + 1) for item in items[:100]]
    if isinstance(value, dict):
        result = {}
        count = 0
        for key in sorted(value.keys(), key=lambda item: str(item)):
            result[str(key)] = make_jsonable(value[key], depth + 1)
            count += 1
            if count >= 100:
                break
        return result
    for method_name in ('tolist', 'item'):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return make_jsonable(method(), depth + 1)
            except Exception:
                pass
    attributes = {}
    try:
        names = dir(value)
    except Exception:
        return truncate_text(repr(value), 400)
    for name in names:
        if name.startswith('__'):
            continue
        try:
            attr_value = getattr(value, name)
        except Exception:
            continue
        if callable(attr_value):
            continue
        attributes[name] = make_jsonable(attr_value, depth + 1)
        if len(attributes) >= 100:
            break
    if attributes:
        return attributes
    return truncate_text(repr(value), 400)


def get_attr(obj, names):
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            if not hasattr(obj, name):
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
        if value in (None, ''):
            continue
        return value
    return None
