import json
import os
import socket
import sys
import traceback
from urllib.parse import parse_qs, urlparse

from server_http_utils import (
    build_json_response as http_build_json_response,
    build_unauthorized_response as http_build_unauthorized_response,
    build_websocket_handshake_response as http_build_websocket_handshake_response,
    build_ws_close_frame as http_build_ws_close_frame,
    build_ws_frame as http_build_ws_frame,
    build_ws_json_frame as http_build_ws_json_frame,
    build_ws_pong_frame as http_build_ws_pong_frame,
    extract_request_token as http_extract_request_token,
    is_request_authorized as http_is_request_authorized,
    parse_http_request as http_parse_http_request,
    queue_client_response as http_queue_client_response,
)
from server_market_utils import (
    build_candles_payload as market_build_candles_payload,
    build_longhubang_payload as market_build_longhubang_payload,
    build_signals_payload as market_build_signals_payload,
    extract_market_rows as market_extract_market_rows,
    normalize_int as market_normalize_int,
    normalize_market_time as market_normalize_market_time,
    normalize_number as market_normalize_number,
    build_full_tick_payload as market_build_full_tick_payload,
)
from server_runtime_utils import (
    create_runtime as runtime_create_runtime,
    get_attr as runtime_get_attr,
    log as runtime_log,
    make_jsonable as runtime_make_jsonable,
    now as runtime_now,
    upgrade_runtime as runtime_upgrade_runtime,
)
from server_socket_utils import (
    accept_new_clients as socket_accept_new_clients,
    consume_ws_frames as socket_consume_ws_frames,
    poll_clients as socket_poll_clients,
    push_ws_updates as socket_push_ws_updates,
    recv_from_client as socket_recv_from_client,
    send_to_client as socket_send_to_client,
)


RUNTIME_MODULE_NAME = 'qmt_live_runtime'
HTTP_SERVER_VERSION = 7
HTTP_HOST = '127.0.0.1'
HTTP_PORT = 18080
TIMER_CALLBACK_NAME = 'server_tick'
HTTP_TIMER_PERIOD = '10nMilliSecond'
HTTP_TIMER_START_TIME = '2000-01-01 00:00:00'
HTTP_TIMER_MARKET = 'SH'
WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
REFRESH_INTERVAL_SECONDS = 5.0
CLIENT_TIMEOUT_SECONDS = 5.0
MAX_ACCEPTS_PER_TICK = 32
MAX_READS_PER_CLIENT = 4
MAX_REQUEST_SIZE = 65536
CONFIG_RELOAD_INTERVAL_SECONDS = 1.0
DEFAULT_ACCOUNT_TYPE = 'STOCK'
DEFAULT_QUOTE_PERIOD = 'tick'
DEFAULT_QUOTE_DIVIDEND_TYPE = 'none'
DEFAULT_CANDLE_DIVIDEND_TYPE = 'front_ratio'
CORS_ALLOW_HEADERS = 'Authorization, X-QMT-Token, Content-Type, Sec-WebSocket-Protocol'
CORS_ALLOW_METHODS = 'GET, OPTIONS'
SERVER_CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'server_config.json')
CONTEXT_ACCOUNT_METHOD_CANDIDATES = [
    'get_account',
    'get_accounts',
    'get_account_id',
    'get_account_ids',
    'get_account_list',
    'get_trade_account',
    'get_trade_accounts',
    'get_accid',
    'account',
    'accounts',
]
POSITION_CODE_FIELD_GROUPS = [
    ('stock_code',),
    ('code',),
    ('symbol',),
    ('ticker',),
    ('m_strStockCode',),
    ('m_strInstrumentID', 'm_strExchangeID'),
]
ACCOUNT_ID_FIELDS = ['account_id', 'accountId', 'accid', 'm_strAccountID', 'm_strAccountId']
ACCOUNT_TYPE_FIELDS = ['account_type', 'accountType', 'm_strAccountType', 'm_eAccountType']
OPTION_TYPE_VALUES = {'', 'CALL', 'PUT'}


def _create_runtime():
    return runtime_create_runtime(RUNTIME_MODULE_NAME, {
        'init_count': 0,
        'handlebar_count': 0,
        'http_host': HTTP_HOST,
        'http_port': HTTP_PORT,
        'http_started_at': None,
        'http_mode': 'runtime_poll',
        'listener_ready': False,
        'timer_registered': False,
        'timer_callback': TIMER_CALLBACK_NAME,
        'timer_period': HTTP_TIMER_PERIOD,
        'timer_start_time': HTTP_TIMER_START_TIME,
        'timer_market': HTTP_TIMER_MARKET,
        'last_init_at': None,
        'last_after_init_at': None,
        'last_handlebar_at': None,
        'last_http_poll_at': None,
        'last_account_at': None,
        'last_position_at': None,
        'last_error_at': None,
        'last_error': None,
        'last_refresh_at': None,
        'last_refresh_error': None,
        'last_refresh_source': None,
        'last_account_refresh_at': None,
        'last_account_refresh_error': None,
        'last_account_refresh_source': None,
        'account_snapshot_count': 0,
        'last_order_at': None,
        'order_snapshot_count': 0,
        'last_deal_at': None,
        'deal_snapshot_count': 0,
        'last_quote_at': None,
        'quote_snapshot_count': 0,
        'quote_subscription_count': 0,
        'account_id': None,
        'account_type': None,
        'account_source': None,
        'account_info': None,
        'order_info': None,
        'deal_info': None,
        'position_source': 'empty',
        'context': None,
        'context_account_methods': [],
        'account_discovery_result': None,
        'config_path': SERVER_CONFIG_FILE,
        'config_mtime': None,
        'last_config_load_at': None,
        'configured_account_id': None,
        'configured_account_type': None,
        'auth_enabled': False,
        'configured_auth_token': None,
        'configured_quote_symbols': [],
        'manual_quote_symbols': [],
        'quote_period': DEFAULT_QUOTE_PERIOD,
        'quote_dividend_type': DEFAULT_QUOTE_DIVIDEND_TYPE,
        'subscription_account_id': None,
        'active_client_count': 0,
        'server_version': HTTP_SERVER_VERSION,
    })


def _upgrade_runtime(runtime):
    return runtime_upgrade_runtime(runtime, _create_runtime())


RUNTIME = sys.modules.get(RUNTIME_MODULE_NAME)
if RUNTIME is None:
    RUNTIME = _create_runtime()
    sys.modules[RUNTIME_MODULE_NAME] = RUNTIME
else:
    RUNTIME = _upgrade_runtime(RUNTIME)


def _now():
    return runtime_now()


def _log(message):
    return runtime_log(message)


def _truncate_text(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + '...'


def _make_jsonable(value, depth=0):
    return runtime_make_jsonable(value, depth)


def _get_attr(obj, names):
    return runtime_get_attr(obj, names)


def _extract_account_identity(source):
    if source is None:
        return None, None
    return _get_attr(source, ACCOUNT_ID_FIELDS), _get_attr(source, ACCOUNT_TYPE_FIELDS)


def _normalize_quote_symbol(value):
    if value in (None, ''):
        return None
    text = str(value).strip().upper()
    if not text or '.' not in text:
        return None
    return text


def _normalize_quote_symbols(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(',')
    result = []
    for value in values:
        if value is None:
            continue
        for part in str(value).split(','): # 支持多个逗号分隔的输入symbol
            normalized = _normalize_quote_symbol(part)
            if normalized is None:
                continue
            if normalized not in result:
                result.append(normalized)
    return result


def _normalize_trade_side(value):
    text = str(value or '').strip().upper()
    if text in ('BUY', 'B', '23'):
        return 'BUY'
    if text in ('SELL', 'S', '24'):
        return 'SELL'
    return None


def _normalize_price(value):
    price = _normalize_number(value)
    if price is None or price <= 0:
        return None
    return round(float(price), 3)


def _normalize_price_type(value):
    text = str(value or '').strip().upper()
    if not text:
        return 11
    mapping = {
        'LIMIT': 11,
        'FIX': 11,
        'MARKET': 18,
        'MARKET_BEST': 18,
        'BEST': 18,
    }
    if text in mapping:
        return mapping[text]
    number = _normalize_int(value)
    if number is not None and number > 0:
        return int(number)
    return 11


def _normalize_volume(value):
    volume = _normalize_int(value)
    if volume is None or volume <= 0:
        return None
    return int(volume)


def _normalize_auth_token(value):
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _normalize_bool_flag(value):
    if value in (None, ''):
        return None
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if text in ('0', 'false', 'no', 'n', 'off'):
        return False
    return None


def _normalize_account_type(value):
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.upper()
    known_types = ['STOCK', 'FUTURE', 'CREDIT', 'HUGANGTONG', 'SHENGANGTONG', 'STOCK_OPTION', 'SWAP']
    if normalized in known_types:
        return normalized
    return text


def _normalize_option_type(value):
    if value in (None, ''):
        return ''
    text = str(value).strip().upper()
    if text == 'ALL':
        return ''
    if text in OPTION_TYPE_VALUES:
        return text
    return None


def _normalize_option_date(value):
    if value in (None, ''):
        return None
    digits = ''.join([char for char in str(value) if char.isdigit()])
    if len(digits) in (6, 8):
        return digits
    return None


def _get_context_callable(name):
    context = RUNTIME.context_ref
    function = getattr(context, name, None) if context is not None else None
    if callable(function):
        return function
    function = globals().get(name)
    if callable(function):
        return function
    return None


def _extract_option_symbol(item):
    if isinstance(item, str):
        return _normalize_quote_symbol(item)
    if isinstance(item, dict):
        for key in ('symbol', 'code', 'InstrumentID', 'instrument_id', 'stock_code'):
            value = item.get(key)
            normalized = _normalize_quote_symbol(value)
            if normalized is not None:
                return normalized
    return None


def _safe_get_option_detail(symbol):
    function = _get_context_callable('get_option_detail_data')
    if not callable(function):
        return None, 'option_detail_unavailable'
    try:
        payload = _make_jsonable(function(symbol))
    except Exception as exc:
        return None, str(exc)
    if isinstance(payload, dict):
        return payload, None
    return {'value': payload}, None


def _safe_get_option_iv(symbol):
    function = _get_context_callable('get_option_iv')
    if not callable(function):
        return None, 'option_iv_unavailable'
    try:
        value = function(symbol)
    except Exception as exc:
        return None, str(exc)
    return _normalize_number(value), None


def _extract_option_sort_value(item, sort_key):
    detail = item.get('detail') if isinstance(item, dict) else None
    if not isinstance(detail, dict):
        detail = {}
    if sort_key == 'strike_desc':
        value = _normalize_number(detail.get('OptExercisePrice'))
        return -(value if value is not None else -1)
    if sort_key == 'expire_asc':
        return str(detail.get('ExpireDate') or '')
    value = _normalize_number(detail.get('OptExercisePrice'))
    return value if value is not None else float('inf')


def _build_option_record(symbol, option_type, with_detail, with_iv, with_quote):
    payload = {
        'symbol': symbol,
        'market': symbol.split('.')[-1] if '.' in symbol else '',
        'option_type': option_type or '',
        'detail': None,
        'iv': None,
        'quote': None,
        'has_quote': False,
        'is_subscribed': False,
    }
    if with_detail:
        detail, detail_error = _safe_get_option_detail(symbol)
        payload['detail'] = detail
        if detail_error:
            payload['detail_error'] = detail_error
        if isinstance(detail, dict):
            payload['exercise_price'] = _normalize_number(detail.get('OptExercisePrice'))
            payload['expire_date'] = str(detail.get('ExpireDate') or '')
            payload['pre_close'] = _normalize_number(detail.get('PreClose'))
            payload['settlement_price'] = _normalize_number(detail.get('SettlementPrice'))
            payload['price_tick'] = _normalize_number(detail.get('PriceTick'))
            payload['volume_multiple'] = _normalize_int(detail.get('VolumeMultiple'))
            payload['underlying_code'] = detail.get('OptUndlCode')
            payload['underlying_market'] = detail.get('OptUndlMarket')
    if with_iv:
        iv_value, iv_error = _safe_get_option_iv(symbol)
        payload['iv'] = iv_value
        if iv_error:
            payload['iv_error'] = iv_error
    if with_quote:
        quote_payload = _build_quote_payload(symbol)
        payload['quote'] = quote_payload.get('quote')
        payload['has_quote'] = bool(quote_payload.get('has_data'))
        payload['is_subscribed'] = bool(quote_payload.get('is_subscribed'))
    return payload


def _build_options_payload(underlying, dedate, option_type, available, with_detail, with_iv, with_quote, auto_subscribe, limit, sort_key):
    context = RUNTIME.context_ref
    normalized_underlying = _normalize_quote_symbol(underlying)
    normalized_date = _normalize_option_date(dedate)
    normalized_type = _normalize_option_type(option_type)
    if normalized_underlying is None:
        return {'error': 'underlying_required'}
    if normalized_date is None:
        return {'error': 'invalid_date'}
    if normalized_type is None:
        return {'error': 'invalid_option_type'}
    function = _get_context_callable('get_option_list')
    if not callable(function):
        return {
            'underlying': normalized_underlying,
            'date': normalized_date,
            'type': normalized_type,
            'items': [],
            'count': 0,
            'error': 'get_option_list_unavailable',
        }
    if len(normalized_date) == 6:
        available = None
    try:
        if available is None:
            option_list = function(normalized_underlying, normalized_date, normalized_type)
        else:
            option_list = function(normalized_underlying, normalized_date, normalized_type, available)
    except TypeError:
        try:
            option_list = function(normalized_underlying, normalized_date, normalized_type)
        except Exception as exc:
            return {
                'underlying': normalized_underlying,
                'date': normalized_date,
                'type': normalized_type,
                'items': [],
                'count': 0,
                'error': 'get_option_list_failed',
                'detail': str(exc),
            }
    except Exception as exc:
        return {
            'underlying': normalized_underlying,
            'date': normalized_date,
            'type': normalized_type,
            'items': [],
            'count': 0,
            'error': 'get_option_list_failed',
            'detail': str(exc),
        }
    raw_items = option_list if isinstance(option_list, (list, tuple)) else []
    symbols = []
    for item in raw_items:
        symbol = _extract_option_symbol(item)
        if symbol is not None and symbol not in symbols:
            symbols.append(symbol)
    if limit is not None and limit >= 0:
        symbols = symbols[:limit]
    if auto_subscribe:
        for symbol in symbols:
            _set_manual_quote_symbol(symbol, True)
        if context is not None:
            _ensure_quote_subscriptions(context)
    items = []
    for symbol in symbols:
        items.append(_build_option_record(symbol, normalized_type, with_detail, with_iv, with_quote))
    if sort_key:
        items.sort(key=lambda item: _extract_option_sort_value(item, sort_key))
    return {
        'underlying': normalized_underlying,
        'date': normalized_date,
        'type': normalized_type,
        'available': available,
        'with_detail': with_detail,
        'with_iv': with_iv,
        'with_quote': with_quote,
        'auto_subscribe': auto_subscribe,
        'count': len(items),
        'items': items,
    }


def _build_option_trade_options_payload():
    return {
        'account_type': 'STOCK_OPTION',
        'actions': [
            {'code': 'BUY_OPEN', 'label': '买入开仓', 'xtconstant': 'STOCK_OPTION_BUY_OPEN'},
            {'code': 'SELL_CLOSE', 'label': '卖出平仓', 'xtconstant': 'STOCK_OPTION_SELL_CLOSE'},
            {'code': 'SELL_OPEN', 'label': '卖出开仓', 'xtconstant': 'STOCK_OPTION_SELL_OPEN'},
            {'code': 'BUY_CLOSE', 'label': '买入平仓', 'xtconstant': 'STOCK_OPTION_BUY_CLOSE'},
            {'code': 'COVERED_OPEN', 'label': '备兑开仓', 'xtconstant': 'STOCK_OPTION_COVERED_OPEN'},
            {'code': 'COVERED_CLOSE', 'label': '备兑平仓', 'xtconstant': 'STOCK_OPTION_COVERED_CLOSE'},
            {'code': 'CALL_EXERCISE', 'label': '认购行权', 'xtconstant': 'STOCK_OPTION_CALL_EXERCISE'},
            {'code': 'PUT_EXERCISE', 'label': '认沽行权', 'xtconstant': 'STOCK_OPTION_PUT_EXERCISE'},
            {'code': 'SECU_LOCK', 'label': '证券锁定', 'xtconstant': 'STOCK_OPTION_SECU_LOCK'},
            {'code': 'SECU_UNLOCK', 'label': '证券解锁', 'xtconstant': 'STOCK_OPTION_SECU_UNLOCK'},
        ],
    }


def _extract_account_identity_from_value(value):
    if value is None:
        return None, None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            account_id, account_type = _extract_account_identity_from_value(item)
            if account_id not in (None, ''):
                return account_id, account_type
        return None, None
    if isinstance(value, dict):
        account_id = _get_attr(value, ACCOUNT_ID_FIELDS)
        account_type = _get_attr(value, ACCOUNT_TYPE_FIELDS)
        if account_id not in (None, ''):
            return account_id, account_type
        for item in value.values():
            account_id, account_type = _extract_account_identity_from_value(item)
            if account_id not in (None, ''):
                return account_id, account_type
        return None, None
    if isinstance(value, (str, int)):
        text = str(value).strip()
        if text:
            return text, None
        return None, None
    return _extract_account_identity(value)


def _collect_context_account_methods(context):
    result = []
    try:
        names = dir(context)
    except Exception:
        return result
    for name in names:
        lowered = name.lower()
        if 'account' not in lowered and 'accid' not in lowered:
            continue
        result.append(name)
        if len(result) >= 40:
            break
    return result


def _try_call_context_method(context, method_name):
    method = getattr(context, method_name, None)
    if not callable(method):
        return None, None
    try:
        value = method()
    except TypeError:
        return None, None
    except Exception:
        return None, None
    return _extract_account_identity_from_value(value)


def _try_discover_account_from_context(context):
    if context is None:
        return
    direct_account_id, direct_account_type = _extract_account_identity(context)
    if direct_account_id not in (None, ''):
        RUNTIME.state['account_id'] = str(direct_account_id)
        normalized_type = _normalize_account_type(direct_account_type)
        if normalized_type is not None:
            RUNTIME.state['account_type'] = normalized_type
        elif not RUNTIME.state['account_type']:
            RUNTIME.state['account_type'] = DEFAULT_ACCOUNT_TYPE
        RUNTIME.state['account_source'] = 'context_attr'
        RUNTIME.state['account_discovery_result'] = 'context_attr'
        return
    method_names = _collect_context_account_methods(context)
    RUNTIME.state['context_account_methods'] = method_names
    for method_name in CONTEXT_ACCOUNT_METHOD_CANDIDATES:
        account_id, account_type = _try_call_context_method(context, method_name)
        if account_id in (None, ''):
            continue
        RUNTIME.state['account_id'] = str(account_id)
        normalized_type = _normalize_account_type(account_type)
        if normalized_type is not None:
            RUNTIME.state['account_type'] = normalized_type
        elif not RUNTIME.state['account_type']:
            RUNTIME.state['account_type'] = DEFAULT_ACCOUNT_TYPE
        RUNTIME.state['account_source'] = method_name
        RUNTIME.state['account_discovery_result'] = method_name
        return


def _apply_context_account_subscription(context):
    account_id = RUNTIME.state['account_id']
    account_type = RUNTIME.state['account_type']
    if context is None or not account_id:
        return
    if RUNTIME.state['subscription_account_id'] == account_id:
        return
    set_account = getattr(context, 'set_account', None)
    if not callable(set_account):
        return
    try:
        if account_type:
            set_account(account_id, account_type)
        else:
            set_account(account_id)
        RUNTIME.state['subscription_account_id'] = account_id
        _log('set_account applied account=%s type=%s' % (account_id, account_type))
    except TypeError:
        try:
            set_account(account_id)
            RUNTIME.state['subscription_account_id'] = account_id
            _log('set_account applied account=%s' % account_id)
        except Exception:
            _record_error('_apply_context_account_subscription')
    except Exception:
        _record_error('_apply_context_account_subscription')


def _load_runtime_config(context):
    now = _now()
    last_config_load_at = RUNTIME.state['last_config_load_at']
    if last_config_load_at is not None and now - last_config_load_at < CONFIG_RELOAD_INTERVAL_SECONDS:
        return
    RUNTIME.state['last_config_load_at'] = now
    try:
        stat_result = os.stat(SERVER_CONFIG_FILE)
    except OSError:
        RUNTIME.state['config_mtime'] = None
        RUNTIME.state['configured_account_id'] = None
        RUNTIME.state['configured_account_type'] = None
        return
    mtime = int(stat_result.st_mtime * 1000000)
    if RUNTIME.state['config_mtime'] == mtime:
        return
    RUNTIME.state['config_mtime'] = mtime
    try:
        with open(SERVER_CONFIG_FILE, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except Exception:
        _record_error('_load_runtime_config')
        return
    configured_account_id = payload.get('account_id')
    configured_account_type = _normalize_account_type(payload.get('account_type'))
    configured_auth_token = _normalize_auth_token(payload.get('auth_token'))
    RUNTIME.state['configured_quote_symbols'] = _normalize_quote_symbols(payload.get('quote_symbols'))
    RUNTIME.state['quote_period'] = str(payload.get('quote_period') or DEFAULT_QUOTE_PERIOD)
    RUNTIME.state['quote_dividend_type'] = str(payload.get('quote_dividend_type') or DEFAULT_QUOTE_DIVIDEND_TYPE)
    RUNTIME.state['configured_auth_token'] = configured_auth_token
    RUNTIME.state['auth_enabled'] = configured_auth_token is not None
    if configured_account_id in (None, ''):
        RUNTIME.state['configured_account_id'] = None
        RUNTIME.state['configured_account_type'] = None
        return
    configured_account_id = str(configured_account_id)
    if configured_account_type is None:
        configured_account_type = DEFAULT_ACCOUNT_TYPE
    RUNTIME.state['configured_account_id'] = configured_account_id
    RUNTIME.state['configured_account_type'] = configured_account_type
    RUNTIME.state['account_id'] = configured_account_id
    RUNTIME.state['account_type'] = configured_account_type
    RUNTIME.state['account_source'] = 'config'
    _apply_context_account_subscription(context)
    _log('config account loaded type=%s' % configured_account_type)


def _extract_code(position):
    for group in POSITION_CODE_FIELD_GROUPS:
        values = []
        for name in group:
            if isinstance(position, dict):
                value = position.get(name)
            else:
                try:
                    value = getattr(position, name)
                except Exception:
                    value = None
            if value in (None, ''):
                values = []
                break
            values.append(str(value))
        if values:
            return '.'.join(values)
    return None


def _build_position_record(position):
    payload = _make_jsonable(position)
    if isinstance(payload, dict):
        code = _extract_code(position)
        if code is not None:
            payload['code'] = code
    return payload


def _normalize_number(value):
    return market_normalize_number(value)


def _normalize_int(value):
    return market_normalize_int(value)


def _normalize_market_time(value, period):
    return market_normalize_market_time(value, period)


def _extract_market_rows(result, fields, period):
    return market_extract_market_rows(result, fields)


def _build_candles_payload(symbol, period, count, start, end, dividend_type):
    return market_build_candles_payload(RUNTIME, symbol, period, count, start, end, dividend_type, _record_error)


def _store_positions(payload, source, replace):
    items = payload if isinstance(payload, (list, tuple, set)) else [payload]
    records = []
    index = {}
    for item in items:
        record = _build_position_record(item)
        code = None
        if isinstance(record, dict):
            code = record.get('code')
        if not code:
            code = 'row_%s' % len(records)
        index[code] = record
        records.append(record)
    if replace:
        RUNTIME.position_index = index
    else:
        RUNTIME.position_index.update(index)
    RUNTIME.state['last_position_at'] = _now()
    RUNTIME.state['position_source'] = source
    return records


def _store_account_snapshot(payload, source):
    items = payload if isinstance(payload, (list, tuple, set)) else [payload]
    records = [_make_jsonable(item) for item in items]
    RUNTIME.state['account_info'] = records
    RUNTIME.state['last_account_at'] = _now()
    RUNTIME.state['account_snapshot_count'] = len(records)
    RUNTIME.state['last_account_refresh_source'] = source
    return records


def _extract_deal_symbol(record):
    instrument = _get_attr(record, ['code', 'symbol', 'm_strInstrumentID'])
    exchange = _get_attr(record, ['exchange', 'm_strExchangeID'])
    if instrument in (None, ''):
        return None
    instrument = str(instrument).strip().upper()
    if '.' in instrument:
        return instrument
    if exchange in (None, ''):
        return instrument
    return '%s.%s' % (instrument, str(exchange).strip().upper())


def _extract_deal_side(record):
    label = _get_attr(record, ['side', 'm_strOptName'])
    if label not in (None, ''):
        text = str(label)
        if '买' in text or 'BUY' in text.upper():
            return 'BUY'
        if '卖' in text or 'SELL' in text.upper():
            return 'SELL'
    direction = _get_attr(record, ['direction', 'm_nDirection'])
    text = str(direction)
    if text in ('0', '48'):
        return 'BUY'
    if text in ('1', '49'):
        return 'SELL'
    return 'UNKNOWN'


def _extract_deal_time(record):
    trade_date = _get_attr(record, ['trade_date', 'm_strTradeDate'])
    trade_time = _get_attr(record, ['trade_time', 'm_strTradeTime'])
    digits_date = ''.join([char for char in str(trade_date or '') if char.isdigit()])
    digits_time = ''.join([char for char in str(trade_time or '') if char.isdigit()])
    if digits_date:
        return digits_date[:8] + digits_time[:6].ljust(6, '0')
    return None


def _build_deal_record(deal):
    payload = _make_jsonable(deal)
    if isinstance(payload, dict):
        payload['symbol'] = _extract_deal_symbol(deal if not isinstance(deal, dict) else payload)
        payload['side'] = _extract_deal_side(deal if not isinstance(deal, dict) else payload)
        payload['time'] = _extract_deal_time(deal if not isinstance(deal, dict) else payload)
        payload['price'] = _normalize_number(_get_attr(deal if not isinstance(deal, dict) else payload, ['price', 'm_dPrice']))
        payload['volume'] = _normalize_int(_get_attr(deal if not isinstance(deal, dict) else payload, ['volume', 'm_nVolume']))
        payload['commission'] = _normalize_number(_get_attr(deal if not isinstance(deal, dict) else payload, ['commission', 'm_dComssion', 'm_dCommission']))
        payload['trade_id'] = _get_attr(deal if not isinstance(deal, dict) else payload, ['trade_id', 'm_strTradeID'])
        payload['order_sys_id'] = _get_attr(deal if not isinstance(deal, dict) else payload, ['order_sys_id', 'm_strOrderSysID'])
        payload['remark'] = _get_attr(deal if not isinstance(deal, dict) else payload, ['remark', 'm_strRemark'])
    return payload


def _extract_order_symbol(record):
    return _normalize_quote_symbol(_get_attr(record, ['symbol', 'code', 'stock_code', 'm_strInstrumentID', 'm_strStockCode']))


def _extract_order_side(record):
    label = _get_attr(record, ['side', 'm_strOptName'])
    if label not in (None, ''):
        text = str(label)
        if '买' in text or 'BUY' in text.upper():
            return 'BUY'
        if '卖' in text or 'SELL' in text.upper():
            return 'SELL'
    direction = _get_attr(record, ['direction', 'm_nDirection'])
    text = str(direction)
    if text in ('0', '48'):
        return 'BUY'
    if text in ('1', '49'):
        return 'SELL'
    return 'UNKNOWN'


def _extract_order_time(record):
    trade_date = _get_attr(record, ['insert_date', 'm_strInsertDate'])
    trade_time = _get_attr(record, ['insert_time', 'm_strInsertTime'])
    digits_date = ''.join([char for char in str(trade_date or '') if char.isdigit()])
    digits_time = ''.join([char for char in str(trade_time or '') if char.isdigit()])
    if digits_date:
        return digits_date[:8] + digits_time[:6].ljust(6, '0')
    return None


def _build_order_record(order):
    payload = _make_jsonable(order)
    if isinstance(payload, dict):
        payload['symbol'] = _extract_order_symbol(order if not isinstance(order, dict) else payload)
        payload['side'] = _extract_order_side(order if not isinstance(order, dict) else payload)
        payload['time'] = _extract_order_time(order if not isinstance(order, dict) else payload)
        payload['price'] = _normalize_number(_get_attr(order if not isinstance(order, dict) else payload, ['price', 'm_dPrice']))
        payload['traded_price'] = _normalize_number(_get_attr(order if not isinstance(order, dict) else payload, ['traded_price', 'm_dTradedPrice']))
        payload['volume'] = _normalize_int(_get_attr(order if not isinstance(order, dict) else payload, ['volume', 'm_nVolumeTotalOriginal']))
        payload['traded_volume'] = _normalize_int(_get_attr(order if not isinstance(order, dict) else payload, ['traded_volume', 'm_nVolumeTraded']))
        payload['order_id'] = _get_attr(order if not isinstance(order, dict) else payload, ['order_id', 'm_nOrderID'])
        payload['order_sys_id'] = _get_attr(order if not isinstance(order, dict) else payload, ['order_sys_id', 'm_strOrderSysID'])
        payload['remark'] = _get_attr(order if not isinstance(order, dict) else payload, ['remark', 'm_strRemark'])
        payload['commission'] = _normalize_number(_get_attr(order if not isinstance(order, dict) else payload, ['commission', 'm_dComssion', 'm_dCommission']))
        payload['status'] = _get_attr(order if not isinstance(order, dict) else payload, ['status', 'm_nOrderStatus'])
    return payload


def _store_order_snapshot(payload, source, replace):
    items = payload if isinstance(payload, (list, tuple, set)) else [payload]
    records = []
    index = {} if replace else dict(RUNTIME.order_index)
    for item in items:
        record = _build_order_record(item)
        if not isinstance(record, dict):
            continue
        order_key = record.get('order_sys_id') or record.get('order_id') or ('row_%s' % len(records))
        index[str(order_key)] = record
        records.append(record)
    RUNTIME.order_index = index
    RUNTIME.state['order_info'] = records[-1] if records else RUNTIME.state['order_info']
    RUNTIME.state['last_order_at'] = _now()
    RUNTIME.state['order_snapshot_count'] = len(RUNTIME.order_index)
    return records


def _store_deal_snapshot(payload, source, replace):
    items = payload if isinstance(payload, (list, tuple, set)) else [payload]
    records = []
    index = {} if replace else dict(RUNTIME.deal_index)
    for item in items:
        record = _build_deal_record(item)
        if not isinstance(record, dict):
            continue
        trade_id = record.get('trade_id') or record.get('order_sys_id') or ('row_%s' % len(records))
        index[str(trade_id)] = record
        records.append(record)
    RUNTIME.deal_index = index
    RUNTIME.state['deal_info'] = records[-1] if records else RUNTIME.state['deal_info']
    RUNTIME.state['last_deal_at'] = _now()
    RUNTIME.state['deal_snapshot_count'] = len(RUNTIME.deal_index)
    return records


def _store_quote_snapshot(stock_code, payload):
    normalized = _normalize_quote_symbol(stock_code)
    if normalized is None:
        return
    RUNTIME.latest_quotes[normalized] = {
        'stock_code': normalized,
        'data': _make_jsonable(payload),
        'ts': _now(),
    }
    RUNTIME.quote_sequence += 1
    RUNTIME.state['last_quote_at'] = RUNTIME.latest_quotes[normalized]['ts']
    RUNTIME.state['quote_snapshot_count'] = len(RUNTIME.latest_quotes)


def _create_quote_callback(stock_code):
    normalized = _normalize_quote_symbol(stock_code)

    def callback(data):
        _store_quote_snapshot(normalized, data)
        return None

    return callback


def _get_target_quote_symbols():
    result = []
    for value in RUNTIME.state['configured_quote_symbols']:
        normalized = _normalize_quote_symbol(value)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    for value in RUNTIME.state.get('manual_quote_symbols', []):
        normalized = _normalize_quote_symbol(value)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    for value in sorted(RUNTIME.position_index.keys()):
        normalized = _normalize_quote_symbol(value)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


def _set_manual_quote_symbol(symbol, enabled):
    normalized = _normalize_quote_symbol(symbol)
    if normalized is None:
        return None
    current = _normalize_quote_symbols(RUNTIME.state.get('manual_quote_symbols', []))
    if enabled:
        if normalized not in current:
            current.append(normalized)
    else:
        current = [item for item in current if item != normalized]
    RUNTIME.state['manual_quote_symbols'] = current
    return normalized


def _ensure_quote_subscriptions(context):
    subscribe_quote = getattr(context, 'subscribe_quote', None)
    if not callable(subscribe_quote):
        return
    unsubscribe_quote = getattr(context, 'unsubscribe_quote', None)
    target_symbols = _get_target_quote_symbols()
    for stock_code in list(RUNTIME.quote_subscriptions.keys()):
        if stock_code in target_symbols:
            continue
        sub_id = RUNTIME.quote_subscriptions.pop(stock_code, None)
        if callable(unsubscribe_quote) and sub_id is not None:
            try:
                unsubscribe_quote(sub_id)
            except Exception:
                _record_error('_ensure_quote_subscriptions')
        RUNTIME.latest_quotes.pop(stock_code, None)
    for stock_code in target_symbols:
        if stock_code in RUNTIME.quote_subscriptions:
            continue
        try:
            sub_id = subscribe_quote(
                stock_code,
                RUNTIME.state['quote_period'],
                RUNTIME.state['quote_dividend_type'],
                _create_quote_callback(stock_code),
            )
            RUNTIME.quote_subscriptions[stock_code] = sub_id
            _log('quote subscribed stock=%s period=%s' % (stock_code, RUNTIME.state['quote_period']))
        except Exception:
            _record_error('_ensure_quote_subscriptions')
    RUNTIME.state['quote_subscription_count'] = len(RUNTIME.quote_subscriptions)
    RUNTIME.state['quote_snapshot_count'] = len(RUNTIME.latest_quotes)


def _clear_quote_subscriptions(context):
    unsubscribe_quote = getattr(context, 'unsubscribe_quote', None)
    if callable(unsubscribe_quote):
        for stock_code, sub_id in list(RUNTIME.quote_subscriptions.items()):
            try:
                unsubscribe_quote(sub_id)
            except Exception:
                _record_error('_clear_quote_subscriptions')
    RUNTIME.quote_subscriptions = {}
    RUNTIME.latest_quotes = {}
    RUNTIME.quote_sequence = 0
    RUNTIME.state['quote_subscription_count'] = 0
    RUNTIME.state['quote_snapshot_count'] = 0


def _record_error(prefix):
    detail = traceback.format_exc()
    _log('%s failed\n%s' % (prefix, detail))
    RUNTIME.state['last_error_at'] = _now()
    RUNTIME.state['last_error'] = detail


def _snapshot_context(context):
    fields = ['barpos', 'start', 'end', 'period', 'dividend_type', 'accid']
    result = {}
    for name in fields:
        if not hasattr(context, name):
            continue
        try:
            result[name] = _make_jsonable(getattr(context, name))
        except Exception:
            continue
    for name in ACCOUNT_ID_FIELDS:
        if hasattr(context, name):
            try:
                result[name] = _make_jsonable(getattr(context, name))
            except Exception:
                pass
            break
    for name in ACCOUNT_TYPE_FIELDS:
        if hasattr(context, name):
            try:
                result[name] = _make_jsonable(getattr(context, name))
            except Exception:
                pass
            break
    return result


def _update_context(context):
    if context is None:
        return
    RUNTIME.context_ref = context
    account_id, account_type = _extract_account_identity(context)
    RUNTIME.state['context'] = _snapshot_context(context)
    RUNTIME.state['context_account_methods'] = _collect_context_account_methods(context)
    if account_id not in (None, ''):
        RUNTIME.state['account_id'] = str(account_id)
        RUNTIME.state['account_source'] = 'context_attr'
    if account_type not in (None, ''):
        RUNTIME.state['account_type'] = _normalize_account_type(account_type)
    _try_discover_account_from_context(context)
    _apply_context_account_subscription(context)


def _get_trade_detail_data_function():
    function = globals().get('get_trade_detail_data')
    if callable(function):
        return function
    return None


def _try_refresh_positions(context, force=False):
    now = _now()
    last_refresh_at = RUNTIME.state['last_refresh_at']
    if not force and last_refresh_at is not None and now - last_refresh_at < REFRESH_INTERVAL_SECONDS:
        return
    _update_context(context)
    account_id = RUNTIME.state['account_id']
    account_type = RUNTIME.state['account_type']
    function = _get_trade_detail_data_function()
    if function is None or not account_id or not account_type:
        return
    try:
        accounts = function(account_id, account_type, 'ACCOUNT')
        account_records = _store_account_snapshot(accounts or [], 'get_trade_detail_data')
        RUNTIME.state['last_account_refresh_at'] = now
        RUNTIME.state['last_account_refresh_error'] = None
        orders = function(account_id, account_type, 'ORDER')
        order_records = _store_order_snapshot(orders or [], 'get_trade_detail_data', True)
        deals = function(account_id, account_type, 'DEAL')
        deal_records = _store_deal_snapshot(deals or [], 'get_trade_detail_data', True)
        positions = function(account_id, account_type, 'position')
        records = _store_positions(positions or [], 'api_snapshot', True)
        RUNTIME.state['last_refresh_at'] = now
        RUNTIME.state['last_refresh_error'] = None
        RUNTIME.state['last_refresh_source'] = 'get_trade_detail_data'
        _ensure_quote_subscriptions(context)
        _log('accounts refreshed count=%s account=%s type=%s' % (len(account_records), account_id, account_type))
        _log('orders refreshed count=%s account=%s type=%s' % (len(order_records), account_id, account_type))
        _log('deals refreshed count=%s account=%s type=%s' % (len(deal_records), account_id, account_type))
        _log('positions refreshed count=%s account=%s type=%s' % (len(records), account_id, account_type))
    except Exception:
        detail = traceback.format_exc()
        _log('refresh positions failed\n%s' % detail)
        RUNTIME.state['last_refresh_at'] = now
        RUNTIME.state['last_refresh_error'] = detail
        RUNTIME.state['last_account_refresh_at'] = now
        RUNTIME.state['last_account_refresh_error'] = detail


def _get_code_mtime():
    try:
        return os.path.getmtime(__file__)
    except Exception:
        return None


def _build_health_payload():
    return {
        'status': 'ok',
        'server_version': HTTP_SERVER_VERSION,
        'code_mtime': _get_code_mtime(),
        'http_mode': RUNTIME.state['http_mode'],
        'http_host': RUNTIME.state['http_host'],
        'http_port': RUNTIME.state['http_port'],
        'http_started_at': RUNTIME.state['http_started_at'],
        'listener_ready': RUNTIME.state['listener_ready'],
        'timer_registered': RUNTIME.state['timer_registered'],
        'timer_callback': RUNTIME.state['timer_callback'],
        'timer_period': RUNTIME.state['timer_period'],
        'timer_start_time': RUNTIME.state['timer_start_time'],
        'timer_market': RUNTIME.state['timer_market'],
        'auth_enabled': RUNTIME.state['auth_enabled'],
        'init_count': RUNTIME.state['init_count'],
        'handlebar_count': RUNTIME.state['handlebar_count'],
        'last_init_at': RUNTIME.state['last_init_at'],
        'last_after_init_at': RUNTIME.state['last_after_init_at'],
        'last_handlebar_at': RUNTIME.state['last_handlebar_at'],
        'last_http_poll_at': RUNTIME.state['last_http_poll_at'],
        'last_position_at': RUNTIME.state['last_position_at'],
        'last_account_at': RUNTIME.state['last_account_at'],
        'last_refresh_at': RUNTIME.state['last_refresh_at'],
        'last_refresh_source': RUNTIME.state['last_refresh_source'],
        'last_refresh_error': RUNTIME.state['last_refresh_error'],
        'last_account_refresh_at': RUNTIME.state['last_account_refresh_at'],
        'last_account_refresh_source': RUNTIME.state['last_account_refresh_source'],
        'last_account_refresh_error': RUNTIME.state['last_account_refresh_error'],
        'last_order_at': RUNTIME.state['last_order_at'],
        'last_deal_at': RUNTIME.state['last_deal_at'],
        'last_quote_at': RUNTIME.state['last_quote_at'],
        'account_id': RUNTIME.state['account_id'],
        'account_type': RUNTIME.state['account_type'],
        'account_source': RUNTIME.state['account_source'],
        'configured_account_id': RUNTIME.state['configured_account_id'],
        'configured_account_type': RUNTIME.state['configured_account_type'],
        'config_path': RUNTIME.state['config_path'],
        'account_snapshot_count': RUNTIME.state['account_snapshot_count'],
        'order_snapshot_count': RUNTIME.state['order_snapshot_count'],
        'deal_snapshot_count': RUNTIME.state['deal_snapshot_count'],
        'quote_subscription_count': RUNTIME.state['quote_subscription_count'],
        'quote_snapshot_count': RUNTIME.state['quote_snapshot_count'],
        'configured_quote_symbols': RUNTIME.state['configured_quote_symbols'],
        'quote_period': RUNTIME.state['quote_period'],
        'quote_dividend_type': RUNTIME.state['quote_dividend_type'],
        'position_count': len(RUNTIME.position_index),
        'active_client_count': RUNTIME.state['active_client_count'],
        'context': RUNTIME.state['context'],
        'context_account_methods': RUNTIME.state['context_account_methods'],
        'account_discovery_result': RUNTIME.state['account_discovery_result'],
        'last_error_at': RUNTIME.state['last_error_at'],
        'last_error': RUNTIME.state['last_error'],
    }


def _build_positions_payload():
    positions = [RUNTIME.position_index[key] for key in sorted(RUNTIME.position_index.keys())]
    return {
        'position_count': len(positions),
        'position_source': RUNTIME.state['position_source'],
        'last_position_at': RUNTIME.state['last_position_at'],
        'last_refresh_at': RUNTIME.state['last_refresh_at'],
        'account_id': RUNTIME.state['account_id'],
        'account_type': RUNTIME.state['account_type'],
        'positions': positions,
    }


def _build_quotes_payload():
    quotes = [RUNTIME.latest_quotes[key] for key in sorted(RUNTIME.latest_quotes.keys())]
    return {
        'quote_count': len(quotes),
        'quote_sequence': RUNTIME.quote_sequence,
        'quote_subscription_count': len(RUNTIME.quote_subscriptions),
        'quote_symbols': sorted(RUNTIME.quote_subscriptions.keys()),
        'last_quote_at': RUNTIME.state['last_quote_at'],
        'quotes': quotes,
    }


def _build_orders_payload(symbol=None, strategy_name=None, remark=None, limit=None):
    orders = [RUNTIME.order_index[key] for key in sorted(RUNTIME.order_index.keys())]
    filtered = []
    symbol_value = _normalize_quote_symbol(symbol) if symbol else None
    strategy_value = str(strategy_name or '').strip()
    remark_value = str(remark or '').strip()
    for item in orders:
        item_symbol = _normalize_quote_symbol(item.get('symbol') or item.get('code'))
        item_remark = str(item.get('remark') or item.get('m_strRemark') or '')
        if symbol_value and item_symbol != symbol_value:
            continue
        if strategy_value and strategy_value not in item_remark:
            continue
        if remark_value and remark_value not in item_remark:
            continue
        filtered.append(item)
    if limit is not None and limit >= 0:
        filtered = filtered[-limit:]
    return {
        'order_count': len(filtered),
        'order_snapshot_count': len(RUNTIME.order_index),
        'last_order_at': RUNTIME.state['last_order_at'],
        'orders': filtered,
    }


def _build_deals_payload(symbol=None, strategy_name=None, remark=None, limit=None):
    deals = [RUNTIME.deal_index[key] for key in sorted(RUNTIME.deal_index.keys())]
    filtered = []
    symbol_value = _normalize_quote_symbol(symbol) if symbol else None
    strategy_value = str(strategy_name or '').strip()
    remark_value = str(remark or '').strip()
    for item in deals:
        item_symbol = _normalize_quote_symbol(item.get('symbol') or item.get('code'))
        item_remark = str(item.get('remark') or item.get('m_strRemark') or '')
        if symbol_value and item_symbol != symbol_value:
            continue
        if strategy_value and strategy_value not in item_remark:
            continue
        if remark_value and remark_value not in item_remark:
            continue
        filtered.append(item)
    if limit is not None and limit >= 0:
        filtered = filtered[-limit:]
    return {
        'deal_count': len(filtered),
        'deal_snapshot_count': len(RUNTIME.deal_index),
        'last_deal_at': RUNTIME.state['last_deal_at'],
        'deals': filtered,
    }


def _build_quote_payload(symbol):
    normalized = _normalize_quote_symbol(symbol)
    if normalized is None:
        return {'symbol': symbol, 'quote': None, 'error': 'symbol_required'}
    quote = RUNTIME.latest_quotes.get(normalized)
    return {
        'symbol': normalized,
        'quote': quote,
        'is_subscribed': normalized in _get_target_quote_symbols(),
        'has_data': quote is not None,
    }

def _build_download_history_payload(symbol, period, start, end):
    normalized_symbol = _normalize_quote_symbol(symbol)
    if normalized_symbol is None:
        return {'error': 'symbol_required'}

    function = globals().get('download_history_data')
    if not callable(function):
        return {'error': 'download_history_data_unavailable'}

    try:
        function(symbol, period or '1d', start or '', end or '')

    except Exception as exc:
        return {'error': 'download_history_failed', 'detail': str(exc),
                'symbol': symbol, 'period': period or '1d', 'start': start, 'end': end}
    return {'status': 'downloaded', 'symbol': symbol, 'period': period or '1d',
            'start': start or '', 'end': end or ''}

def _build_signals_payload(symbol):
    return market_build_signals_payload(RUNTIME, symbol, _normalize_quote_symbol)


def _build_longhubang_payload(symbol, start_time, end_time):
    return market_build_longhubang_payload(RUNTIME, symbol, start_time, end_time, _record_error)

def _build_full_tick_payload(symbols):
    return market_build_full_tick_payload(RUNTIME, symbols)


def _build_accounts_payload():
    return {
        'account_id': RUNTIME.state['account_id'],
        'account_type': RUNTIME.state['account_type'],
        'last_account_at': RUNTIME.state['last_account_at'],
        'account_info': RUNTIME.state['account_info'],
        'account_snapshot_count': RUNTIME.state['account_snapshot_count'],
        'last_account_refresh_at': RUNTIME.state['last_account_refresh_at'],
        'last_account_refresh_source': RUNTIME.state['last_account_refresh_source'],
        'last_account_refresh_error': RUNTIME.state['last_account_refresh_error'],
        'context': RUNTIME.state['context'],
    }


def _call_passorder_with_fallbacks(passorder_func, args_variants):
    last_exc = None
    for args in args_variants:
        try:
            return passorder_func(*args), None, len(args)
        except TypeError as exc:
            last_exc = exc
            continue
    return None, last_exc, None


def _get_passorder_callable(context):
    func = globals().get('passorder')
    if callable(func):
        return func
    func = getattr(context, 'passorder', None)
    if callable(func):
        return func
    return None


def _submit_stock_order(symbol, side, price, volume, remark, batch_id, source, price_type=None):
    normalized_symbol = _normalize_quote_symbol(symbol)
    normalized_side = _normalize_trade_side(side)
    normalized_price = _normalize_price(price)
    normalized_volume = _normalize_volume(volume)
    normalized_price_type = _normalize_price_type(price_type)
    context = RUNTIME.context_ref
    if context is None:
        return {'error': 'context_unavailable'}
    if normalized_symbol is None:
        return {'error': 'invalid_symbol'}
    if normalized_side is None:
        return {'error': 'invalid_side'}
    if normalized_price is None:
        return {'error': 'invalid_price'}
    if normalized_volume is None:
        return {'error': 'invalid_volume'}
    account_id = RUNTIME.state.get('account_id')
    if account_id in (None, ''):
        return {'error': 'account_unavailable'}
    passorder_func = _get_passorder_callable(context)
    if passorder_func is None:
        return {'error': 'passorder_unavailable'}

    strategy_name = 'algo-monitor'
    user_order_id = str(batch_id or ('manual-%s' % int(_now() * 1000)))
    op_type = 23 if normalized_side == 'BUY' else 24
    account_before = _build_accounts_payload()
    quote_before = _build_quote_payload(normalized_symbol)
    debug_before = {
        'order_info': _make_jsonable(RUNTIME.state.get('order_info')),
        'deal_info': _make_jsonable(RUNTIME.state.get('deal_info')),
        'last_error': _make_jsonable(RUNTIME.state.get('last_error')),
    }
    args_variants = [
        (op_type, 1101, str(account_id), normalized_symbol, normalized_price_type, normalized_price, normalized_volume, strategy_name, 1, user_order_id, context),
        (op_type, 1101, str(account_id), normalized_symbol, normalized_price_type, normalized_price, normalized_volume, strategy_name, 1, context),
        (op_type, 1101, str(account_id), normalized_symbol, normalized_price_type, normalized_price, normalized_volume, context),
    ]
    result, signature_error, arg_count = _call_passorder_with_fallbacks(passorder_func, args_variants)
    if signature_error is not None:
        return {
            'error': 'passorder_signature_error',
            'detail': str(signature_error),
            'symbol': normalized_symbol,
        }
    _try_refresh_positions(context, force=True)
    return {
        'status': 'submitted',
        'symbol': normalized_symbol,
        'side': normalized_side,
        'price': normalized_price,
        'price_type': normalized_price_type,
        'volume': normalized_volume,
        'remark': str(remark or ''),
        'batch_id': str(batch_id or ''),
        'source': str(source or ''),
        'account_id': str(account_id),
        'submitted_at': _now(),
        'passorder_result': _make_jsonable(result),
        'passorder_args_count': arg_count,
        'quote_before': quote_before.get('quote'),
        'account_before': account_before,
        'debug_before': debug_before,
        'order_info': _make_jsonable(RUNTIME.state.get('order_info')),
        'deal_info': _make_jsonable(RUNTIME.state.get('deal_info')),
        'last_error': _make_jsonable(RUNTIME.state.get('last_error')),
    }


def _build_instrument_payload(symbol):
    normalized_symbol = _normalize_quote_symbol(symbol)
    if normalized_symbol is None:
        return {'error': 'symbol_required'}
    function = globals().get('get_instrument_detail')
    if not callable(function):
        return {'error': 'instrument_detail_unavailable'}
    try:
        detail = function(normalized_symbol)
    except Exception as exc:
        return {
            'error': 'instrument_detail_failed',
            'detail': str(exc),
            'symbol': normalized_symbol,
        }
    payload = _make_jsonable(detail)
    if isinstance(payload, dict):
        payload['symbol'] = normalized_symbol
        return payload
    return {'symbol': normalized_symbol, 'detail': payload}


def _build_json_response(status_code, payload, extra_headers=None):
    return http_build_json_response(status_code, payload, CORS_ALLOW_HEADERS, CORS_ALLOW_METHODS, extra_headers)


def _extract_request_token(headers):
    return http_extract_request_token(headers, _normalize_auth_token)


def _is_request_authorized(request):
    return http_is_request_authorized(request, RUNTIME.state['configured_auth_token'], _normalize_auth_token)


def _build_unauthorized_response():
    return http_build_unauthorized_response(CORS_ALLOW_HEADERS, CORS_ALLOW_METHODS)


def _parse_http_request(request_bytes):
    return http_parse_http_request(request_bytes)


def _build_websocket_handshake_response(request):
    return http_build_websocket_handshake_response(
        request,
        WEBSOCKET_GUID,
        RUNTIME.state['configured_auth_token'],
        _normalize_auth_token,
        CORS_ALLOW_HEADERS,
        CORS_ALLOW_METHODS,
    )


def _build_ws_frame(payload_bytes, opcode=1):
    return http_build_ws_frame(payload_bytes, opcode)


def _build_ws_json_frame(payload):
    return http_build_ws_json_frame(payload)


def _build_ws_close_frame():
    return http_build_ws_close_frame()


def _build_ws_pong_frame(payload_bytes):
    return http_build_ws_pong_frame(payload_bytes)


def _queue_client_response(client, payload_bytes):
    return http_queue_client_response(client, payload_bytes)


def _build_http_response(request_bytes):
    try:
        request = _parse_http_request(request_bytes)
        if request is None:
            return _build_json_response(404, {'error': 'invalid_request'})
        if request['method'] == 'OPTIONS':
            return _build_json_response(200, {'status': 'ok'})
        authorized, _ = _is_request_authorized(request)
        if not authorized:
            return _build_unauthorized_response()
        method = request['method']
        raw_target = request['target']
        if method != 'GET':
            return _build_json_response(405, {'error': 'method_not_allowed', 'method': method})
        parsed = urlparse(raw_target)
        path = parsed.path or '/'
        query = parse_qs(parsed.query)
        if path == '/':
            return _build_json_response(200, {
                'name': 'qmt-position-server',
                'mode': 'runtime_poll',
                'endpoints': [
                    '/health', '/positions', '/accounts', '/quotes', '/quote',
                    '/orders', '/deals', '/subscribe', '/unsubscribe', '/candles', '/signals', '/instrument',
                    '/options', '/option-trade-options', '/longhubang', '/order', '/ws', 
                    '/full_tick', '/download_history',
                    '/debug/trade',
                ],
            })
        if path == '/health':
            return _build_json_response(200, _build_health_payload())
        if path == '/positions':
            return _build_json_response(200, _build_positions_payload())
        if path == '/quotes':
            return _build_json_response(200, _build_quotes_payload())
        if path == '/orders':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            strategy_name = (query.get('strategy_name') or [''])[0]
            remark = (query.get('remark') or [''])[0]
            limit = _normalize_int((query.get('limit') or [200])[0]) or 200
            return _build_json_response(200, _build_orders_payload(symbol, strategy_name, remark, limit))
        if path == '/deals':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            strategy_name = (query.get('strategy_name') or [''])[0]
            remark = (query.get('remark') or [''])[0]
            limit = _normalize_int((query.get('limit') or [200])[0]) or 200
            return _build_json_response(200, _build_deals_payload(symbol, strategy_name, remark, limit))
        if path == '/quote':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            return _build_json_response(200, _build_quote_payload(symbol))
        if path == '/instrument':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            payload = _build_instrument_payload(symbol)
            status_code = 200 if not payload.get('error') else 400
            return _build_json_response(status_code, payload)
        if path == '/options':
            underlying = (query.get('underlying') or [None])[0]
            dedate = (query.get('date') or [None])[0]
            option_type = (query.get('type') or [''])[0]
            available = _normalize_bool_flag((query.get('available') or [None])[0])
            with_detail = _normalize_bool_flag((query.get('with_detail') or ['1'])[0])
            with_iv = _normalize_bool_flag((query.get('with_iv') or ['0'])[0])
            with_quote = _normalize_bool_flag((query.get('with_quote') or ['0'])[0])
            auto_subscribe = _normalize_bool_flag((query.get('auto_subscribe') or ['0'])[0])
            limit = _normalize_int((query.get('limit') or [200])[0])
            sort_key = str((query.get('sort') or ['strike_asc'])[0] or 'strike_asc')
            payload = _build_options_payload(
                underlying,
                dedate,
                option_type,
                available,
                with_detail is not False,
                with_iv is True,
                with_quote is True,
                auto_subscribe is True,
                limit,
                sort_key,
            )
            status_code = 200 if not payload.get('error') else 400
            return _build_json_response(status_code, payload)
        if path == '/option-trade-options':
            return _build_json_response(200, _build_option_trade_options_payload())
        if path == '/subscribe':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            if symbol is None:
                return _build_json_response(400, {'error': 'symbol_required'})
            _set_manual_quote_symbol(symbol, True)
            if RUNTIME.context_ref is not None:
                _ensure_quote_subscriptions(RUNTIME.context_ref)
            return _build_json_response(200, {
                'status': 'ok',
                'symbol': symbol,
                'manual_quote_symbols': RUNTIME.state.get('manual_quote_symbols', []),
                'quote': _build_quote_payload(symbol).get('quote'),
            })
        if path == '/unsubscribe':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            if symbol is None:
                return _build_json_response(400, {'error': 'symbol_required'})
            _set_manual_quote_symbol(symbol, False)
            if RUNTIME.context_ref is not None:
                _ensure_quote_subscriptions(RUNTIME.context_ref)
            return _build_json_response(200, {
                'status': 'ok',
                'symbol': symbol,
                'manual_quote_symbols': RUNTIME.state.get('manual_quote_symbols', []),
            })
        if path == '/candles':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            period = str((query.get('period') or ['1d'])[0] or '1d')
            count = _normalize_int((query.get('count') or [240])[0]) or 240
            start = (query.get('start') or [None])[0]
            end = (query.get('end') or [None])[0]
            dividend_type = str((query.get('dividend_type') or [RUNTIME.state['context'].get('dividend_type') if isinstance(RUNTIME.state.get('context'), dict) else DEFAULT_CANDLE_DIVIDEND_TYPE])[0] or DEFAULT_CANDLE_DIVIDEND_TYPE)
            if symbol is None:
                return _build_json_response(404, {'error': 'symbol_required'})
            return _build_json_response(200, _build_candles_payload(symbol, period, count, start, end, dividend_type))
        if path == '/signals':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            return _build_json_response(200, _build_signals_payload(symbol))
        if path == '/longhubang':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            if symbol is None:
                return _build_json_response(400, {'error': 'symbol_required'})
            import time as _time_mod
            today = _time_mod.strftime('%Y%m%d')
            start_time = str((query.get('start') or [today])[0] or today)
            end_time = str((query.get('end') or [today])[0] or today)
            return _build_json_response(200, _build_longhubang_payload(symbol, start_time, end_time))
        if path == '/order':
            symbol = (query.get('symbol') or [None])[0]
            side = (query.get('side') or [None])[0]
            price = (query.get('price') or [None])[0]
            volume = (query.get('volume') or [None])[0]
            price_type = (query.get('price_type') or [''])[0]
            remark = (query.get('remark') or [''])[0]
            batch_id = (query.get('batch_id') or [''])[0]
            source = (query.get('source') or [''])[0]
            payload = _submit_stock_order(symbol, side, price, volume, remark, batch_id, source, price_type)
            if payload.get('error'):
                return _build_json_response(400, payload)
            return _build_json_response(200, payload)
        if path == '/accounts':
            return _build_json_response(200, _build_accounts_payload())

        if path == '/full_tick':
            raw_symbols = query.get('symbols') or query.get('symbol') or []
            symbols = _normalize_quote_symbols(raw_symbols)
            return _build_json_response(200, _build_full_tick_payload(symbols))
        
        if path == '/download_history':
            symbol = _normalize_quote_symbol((query.get('symbol') or [None])[0])
            period = str((query.get('period') or ['1d'])[0] or '1d')
            start = str((query.get('start') or [''])[0] or '')
            end = str((query.get('end') or [''])[0] or '')
            payload = _build_download_history_payload(symbol, period, start, end)
            status_code = 200 if not payload.get('error') else 400
            return _build_json_response(status_code, payload)
        
        if path == '/debug/trade':
            return _build_json_response(200, {
                'health': _build_health_payload(),
                'accounts': _build_accounts_payload(),
                'positions': _build_positions_payload(),
                'orders': _build_orders_payload(limit=200),
                'deals': _build_deals_payload(limit=200),
                'quotes': _build_quotes_payload(),
                'signals': _build_signals_payload(None),
            })
        return _build_json_response(404, {'error': 'not_found', 'path': path})
    except Exception:
        _record_error('_build_http_response')
        return _build_json_response(500, {'error': 'internal_error'})


def _close_socket(sock):
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        pass


def _close_client(client_key):
    client = RUNTIME.clients.pop(client_key, None)
    if client is None:
        return
    _close_socket(client.get('socket'))
    RUNTIME.state['active_client_count'] = len(RUNTIME.clients)


def _close_all_clients():
    for client_key in list(RUNTIME.clients.keys()):
        _close_client(client_key)


def _reset_listener():
    _close_all_clients()
    _close_socket(RUNTIME.listener)
    RUNTIME.listener = None
    RUNTIME.state['listener_ready'] = False


def _ensure_http_listener():
    if RUNTIME.listener is not None and RUNTIME.state.get('server_version') == HTTP_SERVER_VERSION:
        return
    _reset_listener()
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HTTP_HOST, HTTP_PORT))
        listener.listen(128)
        listener.setblocking(False)
        RUNTIME.listener = listener
        RUNTIME.state['server_version'] = HTTP_SERVER_VERSION
        RUNTIME.state['http_started_at'] = _now()
        RUNTIME.state['http_host'] = HTTP_HOST
        RUNTIME.state['http_port'] = HTTP_PORT
        RUNTIME.state['listener_ready'] = True
        _log('http listener ready at http://%s:%s' % (HTTP_HOST, HTTP_PORT))
    except Exception:
        RUNTIME.state['last_error_at'] = _now()
        RUNTIME.state['last_error'] = traceback.format_exc()
        RUNTIME.state['listener_ready'] = False
        _log('http listener start failed\n%s' % RUNTIME.state['last_error'])


def _ensure_timer(ContextInfo):
    if RUNTIME.state['timer_registered']:
        return
    run_time = getattr(ContextInfo, 'run_time', None)
    if not callable(run_time):
        return
    try:
        run_time(TIMER_CALLBACK_NAME, HTTP_TIMER_PERIOD, HTTP_TIMER_START_TIME, HTTP_TIMER_MARKET)
        RUNTIME.state['timer_registered'] = True
        RUNTIME.state['timer_callback'] = TIMER_CALLBACK_NAME
        RUNTIME.state['timer_period'] = HTTP_TIMER_PERIOD
        RUNTIME.state['timer_start_time'] = HTTP_TIMER_START_TIME
        RUNTIME.state['timer_market'] = HTTP_TIMER_MARKET
        _log('run_time registered func=%s period=%s' % (TIMER_CALLBACK_NAME, HTTP_TIMER_PERIOD))
    except Exception:
        _record_error('_ensure_timer')


def _accept_new_clients():
    return socket_accept_new_clients(RUNTIME, MAX_ACCEPTS_PER_TICK, _now, _log)


def _consume_ws_frames(client):
    return socket_consume_ws_frames(client, _queue_client_response, _build_ws_close_frame, _build_ws_pong_frame)


def _recv_from_client(client):
    return socket_recv_from_client(
        client,
        MAX_READS_PER_CLIENT,
        MAX_REQUEST_SIZE,
        _now,
        _build_json_response,
        _parse_http_request,
        _build_websocket_handshake_response,
        _build_http_response,
        _consume_ws_frames,
    )


def _send_to_client(client):
    return socket_send_to_client(client, _now)


def _push_ws_updates():
    return socket_push_ws_updates(RUNTIME, _build_quotes_payload, _queue_client_response, _build_ws_json_frame)


def _poll_clients():
    return socket_poll_clients(RUNTIME, CLIENT_TIMEOUT_SECONDS, _now, _recv_from_client, _send_to_client, _close_client)


def _run_server_tick(ContextInfo):
    _update_context(ContextInfo)
    _load_runtime_config(ContextInfo)
    _ensure_timer(ContextInfo)
    _ensure_http_listener()
    if RUNTIME.listener is None:
        return
    RUNTIME.state['last_http_poll_at'] = _now()
    _accept_new_clients()
    _poll_clients()
    _push_ws_updates()
    _poll_clients()


def on_event(event_name, *args):
    if event_name == TIMER_CALLBACK_NAME and args:
        return server_tick(args[0])
    if event_name != 'handlebar':
        _log('event=%s args=%s' % (event_name, len(args)))
    return None


def init(ContextInfo):
    _run_server_tick(ContextInfo)
    RUNTIME.state['init_count'] += 1
    RUNTIME.state['last_init_at'] = _now()
    _log('init count=%s' % RUNTIME.state['init_count'])
    _try_refresh_positions(ContextInfo, force=True)
    return None


def after_init(ContextInfo):
    _run_server_tick(ContextInfo)
    RUNTIME.state['last_after_init_at'] = _now()
    _try_refresh_positions(ContextInfo, force=True)
    _log('after_init')
    return None


def server_tick(ContextInfo):
    _run_server_tick(ContextInfo)
    _try_refresh_positions(ContextInfo, force=False)
    return None


def handlebar(ContextInfo):
    _update_context(ContextInfo)
    RUNTIME.state['handlebar_count'] += 1
    RUNTIME.state['last_handlebar_at'] = _now()
    _try_refresh_positions(ContextInfo, force=False)
    return None


def stop(ContextInfo):
    _update_context(ContextInfo)
    _clear_quote_subscriptions(ContextInfo)
    _reset_listener()
    RUNTIME.context_ref = None
    RUNTIME.state['timer_registered'] = False
    RUNTIME.state['timer_callback'] = TIMER_CALLBACK_NAME
    RUNTIME.state['subscription_account_id'] = None
    _log('stop')
    return None


def account_callback(ContextInfo, account_info):
    _update_context(ContextInfo)
    account_id, account_type = _extract_account_identity(account_info)
    RUNTIME.state['last_account_at'] = _now()
    RUNTIME.state['account_info'] = _make_jsonable(account_info)
    if account_id not in (None, ''):
        RUNTIME.state['account_id'] = str(account_id)
    if account_type not in (None, ''):
        RUNTIME.state['account_type'] = _normalize_account_type(account_type)
    _try_refresh_positions(ContextInfo, force=True)
    return None


def order_callback(ContextInfo, order_info):
    _update_context(ContextInfo)
    _store_order_snapshot(order_info, 'order_callback', False)
    return None


def deal_callback(ContextInfo, deal_info):
    _update_context(ContextInfo)
    _store_deal_snapshot(deal_info, 'deal_callback', False)
    _try_refresh_positions(ContextInfo, force=True)
    return None


def position_callback(ContextInfo, position_info):
    _update_context(ContextInfo)
    replace = isinstance(position_info, (list, tuple, set))
    _store_positions(position_info, 'position_callback', replace)
    _ensure_quote_subscriptions(ContextInfo)
    return None


def orderError_callback(ContextInfo, order_info, error_info):
    _update_context(ContextInfo)
    try:
        _store_order_snapshot(order_info, 'order_error_callback', False)
        RUNTIME.state['last_error_at'] = _now()
        RUNTIME.state['last_error'] = _make_jsonable(error_info)
    except Exception:
        _record_error('orderError_callback')
    return None


def order_error_callback(ContextInfo, order_info, error_info):
    return orderError_callback(ContextInfo, order_info, error_info)


def credit_opvolume_callback(ContextInfo, accid, seq, ret, result):
    _update_context(ContextInfo)
    if accid not in (None, ''):
        RUNTIME.state['account_id'] = str(accid)
    if ret not in (None, 0):
        RUNTIME.state['last_error_at'] = _now()
        RUNTIME.state['last_error'] = {'ret': ret, 'result': _make_jsonable(result)}
    return None
