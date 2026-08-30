from server_market_utils import (
    _dataframe_to_records,
    normalize_int,
    normalize_number,
)

BATCH_SIZE = 300

INSTRUMENT_BULK_FIELDS = (
    'UpStopPrice',
    'DownStopPrice',
    'InstrumentStatus',
    'IsTrading',
    'TotalVolumn',
    'FloatVolumn',
    'OpenDate',
    'ExpireDate',
    'InstrumentName',
    'PreClose',
)


def _get_context_callable(runtime, names):
    if isinstance(names, str):
        names = (names,)
    context = runtime.context_ref
    for name in names:
        function = getattr(context, name, None) if context is not None else None
        if callable(function):
            return function, name
    for name in names:
        function = globals().get(name)
        if callable(function):
            return function, name
    return None, None


def _normalize_symbol_list(symbols):
    result = []
    for symbol in symbols or []:
        text = str(symbol or '').strip().upper()
        if not text or '.' not in text:
            continue
        if text not in result:
            result.append(text)
    return result


def _normalize_date_key(value):
    if value is None:
        return None
    if hasattr(value, 'strftime'):
        return value.strftime('%Y%m%d')
    digits = ''.join(char for char in str(value) if char.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return str(value)


def _normalize_factor_entries(raw):
    if raw is None:
        return []
    if isinstance(raw, dict):
        entries = []
        for key, value in raw.items():
            entries.append({
                'date': _normalize_date_key(key),
                'factor': normalize_number(value),
            })
        entries.sort(key=lambda item: str(item.get('date') or ''))
        return entries
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _filter_entries_by_range(entries, start, end):
    if not start and not end:
        return entries
    filtered = []
    for entry in entries:
        date_key = str(entry.get('date') or '')
        if start and date_key < str(start):
            continue
        if end and date_key > str(end):
            continue
        filtered.append(entry)
    return filtered


def _extract_instrument_record(detail):
    if not isinstance(detail, dict):
        return {}
    record = {}
    for field in INSTRUMENT_BULK_FIELDS:
        value = detail.get(field)
        if field in ('InstrumentStatus', 'IsTrading'):
            record[field] = normalize_int(value) if value is not None else value
        elif field in ('OpenDate', 'ExpireDate', 'InstrumentName'):
            record[field] = value
        else:
            record[field] = normalize_number(value)
    return record


def _parse_turnover_rate_chunk(result, chunk):
    rates_by_symbol = {symbol: [] for symbol in chunk}
    if result is None:
        return rates_by_symbol

    columns = getattr(result, 'columns', None)
    index = getattr(result, 'index', None)
    if columns is not None and index is not None:
        try:
            column_names = [str(item) for item in columns]
            for time_key, row in result.iterrows():
                trade_date = _normalize_date_key(time_key)
                row_dict = row.to_dict() if hasattr(row, 'to_dict') else {}
                for symbol in chunk:
                    column = symbol if symbol in row_dict else None
                    if column is None:
                        for candidate in column_names:
                            if candidate.upper() == symbol:
                                column = candidate
                                break
                    if column is None:
                        continue
                    value = normalize_number(row_dict.get(column))
                    if trade_date is not None and value is not None:
                        rates_by_symbol[symbol].append({
                            'trade_date': trade_date,
                            'turnover_rate': value,
                        })
            if any(rates_by_symbol[symbol] for symbol in chunk):
                return rates_by_symbol
        except Exception:
            pass

    records = _dataframe_to_records(result)
    if records:
        for row in records:
            trade_date = _normalize_date_key(
                row.get('trade_date') or row.get('time') or row.get('date') or row.get('index')
            )
            for symbol in chunk:
                value = normalize_number(row.get(symbol))
                if value is None:
                    continue
                rates_by_symbol[symbol].append({
                    'trade_date': trade_date,
                    'turnover_rate': value,
                })
        return rates_by_symbol

    if isinstance(result, dict):
        for symbol in chunk:
            symbol_result = result.get(symbol)
            if isinstance(symbol_result, dict):
                for key, value in symbol_result.items():
                    rates_by_symbol[symbol].append({
                        'trade_date': _normalize_date_key(key),
                        'turnover_rate': normalize_number(value),
                    })
    return rates_by_symbol


def build_divid_factors_payload(runtime, symbols, start, end, record_error, batch_size=BATCH_SIZE):
    symbols = _normalize_symbol_list(symbols)
    factors_by_symbol = {}
    errors = []
    get_divid_factors, _ = _get_context_callable(runtime, 'get_divid_factors')
    if not callable(get_divid_factors):
        return {
            'start': start,
            'end': end,
            'factors_by_symbol': {},
            'errors': ['get_divid_factors_unavailable'],
            'requested': len(symbols),
            'returned': 0,
        }
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset:offset + batch_size]
        for symbol in chunk:
            try:
                raw = get_divid_factors(symbol)
                entries = _filter_entries_by_range(_normalize_factor_entries(raw), start, end)
                if entries:
                    factors_by_symbol[symbol] = entries
            except Exception:
                record_error('build_divid_factors_payload')
                errors.append('get_divid_factors_failed')
                break
        if errors:
            break
    return {
        'start': start,
        'end': end,
        'factors_by_symbol': factors_by_symbol,
        'errors': errors,
        'requested': len(symbols),
        'returned': len(factors_by_symbol),
    }


def build_instrument_bulk_payload(runtime, symbols, record_error, batch_size=BATCH_SIZE):
    symbols = _normalize_symbol_list(symbols)
    detail_by_symbol = {}
    errors = []
    get_instrumentdetail, _ = _get_context_callable(
        runtime,
        ('get_instrumentdetail', 'get_instrument_detail'),
    )
    if not callable(get_instrumentdetail):
        return {
            'detail_by_symbol': {},
            'errors': ['get_instrumentdetail_unavailable'],
            'requested': len(symbols),
            'returned': 0,
        }
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset:offset + batch_size]
        for symbol in chunk:
            try:
                detail = get_instrumentdetail(symbol)
                record = _extract_instrument_record(detail if isinstance(detail, dict) else {})
                if record:
                    record['symbol'] = symbol
                    detail_by_symbol[symbol] = record
            except Exception:
                record_error('build_instrument_bulk_payload')
                errors.append('get_instrumentdetail_failed')
                break
        if errors:
            break
    return {
        'detail_by_symbol': detail_by_symbol,
        'errors': errors,
        'requested': len(symbols),
        'returned': len(detail_by_symbol),
    }


def build_turnover_rate_payload(runtime, symbols, start, end, record_error, batch_size=BATCH_SIZE):
    symbols = _normalize_symbol_list(symbols)
    rates_by_symbol = {}
    errors = []
    get_turnover_rate, _ = _get_context_callable(runtime, 'get_turnover_rate')
    if not callable(get_turnover_rate):
        return {
            'start': start,
            'end': end,
            'rates_by_symbol': {},
            'errors': ['get_turnover_rate_unavailable'],
            'requested': len(symbols),
            'returned': 0,
        }
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset:offset + batch_size]
        try:
            result = get_turnover_rate(chunk, start or '', end or '')
            chunk_rates = _parse_turnover_rate_chunk(result, chunk)
            for symbol, rows in chunk_rates.items():
                if rows:
                    rates_by_symbol[symbol] = rows
        except Exception:
            record_error('build_turnover_rate_payload')
            errors.append('get_turnover_rate_failed')
            break
    return {
        'start': start,
        'end': end,
        'rates_by_symbol': rates_by_symbol,
        'errors': errors,
        'requested': len(symbols),
        'returned': len(rates_by_symbol),
    }


def build_total_share_payload(runtime, symbols, record_error, batch_size=BATCH_SIZE):
    symbols = _normalize_symbol_list(symbols)
    shares_by_symbol = {}
    errors = []
    get_total_share, _ = _get_context_callable(runtime, 'get_total_share')
    if not callable(get_total_share):
        return {
            'shares_by_symbol': {},
            'errors': ['get_total_share_unavailable'],
            'requested': len(symbols),
            'returned': 0,
        }
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset:offset + batch_size]
        for symbol in chunk:
            try:
                value = get_total_share(symbol)
                number = normalize_number(value)
                if number is not None:
                    shares_by_symbol[symbol] = number
            except Exception:
                record_error('build_total_share_payload')
                errors.append('get_total_share_failed')
                break
        if errors:
            break
    return {
        'shares_by_symbol': shares_by_symbol,
        'errors': errors,
        'requested': len(symbols),
        'returned': len(shares_by_symbol),
    }


def build_trading_dates_payload(runtime, symbol, start, end, count, period, record_error):
    symbol = _normalize_symbol_list([symbol])[0] if symbol else None
    if symbol is None:
        return {
            'symbol': symbol,
            'start': start,
            'end': end,
            'count': count,
            'period': period,
            'dates': [],
            'error': 'symbol_required',
        }
    get_trading_dates, _ = _get_context_callable(runtime, 'get_trading_dates')
    if not callable(get_trading_dates):
        return {
            'symbol': symbol,
            'start': start,
            'end': end,
            'count': count,
            'period': period,
            'dates': [],
            'error': 'get_trading_dates_unavailable',
        }
    try:
        effective_count = count if count is not None else 1
        try:
            dates = get_trading_dates(
                symbol,
                start or '',
                end or '',
                effective_count,
                period or '1d',
            )
        except TypeError:
            dates = get_trading_dates(
                symbol,
                start or '',
                end or '',
                effective_count,
            )
        normalized_dates = []
        for item in dates or []:
            normalized = _normalize_date_key(item)
            if normalized is not None:
                normalized_dates.append(normalized)
        return {
            'symbol': symbol,
            'start': start,
            'end': end,
            'count': count,
            'period': period or '1d',
            'dates': normalized_dates,
        }
    except Exception:
        record_error('build_trading_dates_payload')
        return {
            'symbol': symbol,
            'start': start,
            'end': end,
            'count': count,
            'period': period or '1d',
            'dates': [],
            'error': 'get_trading_dates_failed',
        }


def build_sector_payload(runtime, name, with_weight, index_code, realtime, record_error):
    sector_name = str(name or '').strip()
    if not sector_name:
        return {'name': name, 'symbols': [], 'weights_by_symbol': {}, 'error': 'name_required'}
    get_stock_list_in_sector, _ = _get_context_callable(runtime, 'get_stock_list_in_sector')
    if not callable(get_stock_list_in_sector):
        return {
            'name': sector_name,
            'symbols': [],
            'weights_by_symbol': {},
            'error': 'get_stock_list_in_sector_unavailable',
        }
    try:
        if realtime in (None, ''):
            try:
                symbols = get_stock_list_in_sector(sector_name)
            except TypeError:
                symbols = get_stock_list_in_sector(sector_name, 0)
        else:
            symbols = get_stock_list_in_sector(sector_name, realtime)
    except Exception:
        record_error('build_sector_payload')
        return {
            'name': sector_name,
            'symbols': [],
            'weights_by_symbol': {},
            'error': 'get_stock_list_in_sector_failed',
        }
    normalized_symbols = _normalize_symbol_list(symbols)
    payload = {
        'name': sector_name,
        'symbols': normalized_symbols,
        'count': len(normalized_symbols),
        'with_weight': bool(with_weight),
        'weights_by_symbol': {},
    }
    if not with_weight:
        return payload
    weight_index = str(index_code or sector_name).strip()
    get_weight_in_index, _ = _get_context_callable(runtime, 'get_weight_in_index')
    if not callable(get_weight_in_index):
        payload['error'] = 'get_weight_in_index_unavailable'
        return payload
    weights_by_symbol = {}
    for symbol in normalized_symbols:
        try:
            weight = normalize_number(get_weight_in_index(weight_index, symbol))
            if weight is not None:
                weights_by_symbol[symbol] = weight
        except Exception:
            record_error('build_sector_payload')
            payload['error'] = 'get_weight_in_index_failed'
            payload['weights_by_symbol'] = weights_by_symbol
            return payload
    payload['index_code'] = weight_index
    payload['weights_by_symbol'] = weights_by_symbol
    return payload
