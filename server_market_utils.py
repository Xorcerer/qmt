def normalize_number(value):
    if value in (None, ''):
        return None
    try:
        if hasattr(value, 'item') and callable(getattr(value, 'item')):
            value = value.item()
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


def normalize_int(value):
    number = normalize_number(value)
    if number is None:
        return None
    return int(number)


def normalize_market_time(value, period):
    if value is None:
        return None
    if hasattr(value, 'strftime'):
        if period in ('tick', '1m', '3m', '5m', '15m', '30m', '1h'):
            return value.strftime('%Y%m%d%H%M%S')
        return value.strftime('%Y%m%d')
    text = str(value)
    digits = ''.join([char for char in text if char.isdigit()])
    if period == '1mon' and len(digits) >= 6:
        return digits[:6] + '01'
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) >= 8:
        return digits[:8]
    return text


def extract_market_rows(result, fields):
    rows = []
    iterrows = getattr(result, 'iterrows', None)
    if callable(iterrows):
        try:
            for index, row in iterrows():
                row_dict = row.to_dict() if hasattr(row, 'to_dict') else {}
                rows.append((index, row_dict))
            if rows:
                return rows
        except Exception:
            pass
    if isinstance(result, dict):
        dict_values = []
        for field in fields:
            value = result.get(field)
            if isinstance(value, dict):
                dict_values.append(value)
        if dict_values:
            times = []
            for field_values in dict_values:
                for key in field_values.keys():
                    if key not in times:
                        times.append(key)
            for time_key in times:
                row_dict = {}
                for field in fields:
                    field_values = result.get(field)
                    if isinstance(field_values, dict):
                        row_dict[field] = field_values.get(time_key)
                rows.append((time_key, row_dict))
    return rows


def build_candles_payload(
    runtime,
    symbol,
    period,
    count,
    start,
    end,
    dividend_type,
    record_error,
):
    context = runtime.context_ref
    if context is None:
        return {
            'symbol': symbol,
            'period': period,
            'count': count,
            'start': start,
            'end': end,
            'dividend_type': dividend_type,
            'bars': [],
            'error': 'context_unavailable',
        }
    get_market_data = getattr(context, 'get_market_data', None)
    if not callable(get_market_data):
        return {
            'symbol': symbol,
            'period': period,
            'count': count,
            'start': start,
            'end': end,
            'dividend_type': dividend_type,
            'bars': [],
            'error': 'get_market_data_unavailable',
        }
    fields = ['open', 'high', 'low', 'close', 'volume', 'amount']
    kwargs = {
        'stock_code': [symbol],
        'period': period,
        'dividend_type': dividend_type,
        'count': count,
    }
    if start:
        kwargs['start_time'] = start
    if end:
        kwargs['end_time'] = end
    try:
        result = get_market_data(fields, **kwargs)
        bars = []
        for index, row_dict in extract_market_rows(result, fields):
            bars.append({
                'time': normalize_market_time(index, period),
                'open': normalize_number(row_dict.get('open')),
                'high': normalize_number(row_dict.get('high')),
                'low': normalize_number(row_dict.get('low')),
                'close': normalize_number(row_dict.get('close')),
                'volume': normalize_number(row_dict.get('volume')),
                'amount': normalize_number(row_dict.get('amount')),
            })
        bars = [bar for bar in bars if bar['time'] is not None]
        return {
            'symbol': symbol,
            'period': period,
            'count': count,
            'start': start,
            'end': end,
            'dividend_type': dividend_type,
            'bars': bars,
        }
    except Exception:
        record_error('_build_candles_payload')
        return {
            'symbol': symbol,
            'period': period,
            'count': count,
            'start': start,
            'end': end,
            'dividend_type': dividend_type,
            'bars': [],
            'error': 'get_market_data_failed',
        }


def build_candles_bulk_payload(
    runtime,
    symbols,
    period,
    start,
    end,
    dividend_type,
    record_error,
    batch_size=300,
):
    symbols = [symbol for symbol in (symbols or []) if symbol]
    bars_by_symbol = {}
    errors = []
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset:offset + batch_size]
        context = runtime.context_ref
        if context is None:
            errors.append('context_unavailable')
            break
        get_market_data = getattr(context, 'get_market_data', None)
        if not callable(get_market_data):
            errors.append('get_market_data_unavailable')
            break
        fields = ['open', 'high', 'low', 'close', 'volume', 'amount']
        kwargs = {
            'stock_code': chunk,
            'period': period,
            'dividend_type': dividend_type,
            'count': -1,
        }
        if start:
            kwargs['start_time'] = start
        if end:
            kwargs['end_time'] = end
        try:
            result = get_market_data(fields, **kwargs)
            for symbol in chunk:
                symbol_result = result
                if isinstance(result, dict) and symbol in result:
                    symbol_result = result.get(symbol)
                rows = extract_market_rows(symbol_result, fields) if symbol_result is not None else []
                bars = []
                for index, row_dict in rows:
                    bars.append({
                        'time': normalize_market_time(index, period),
                        'open': normalize_number(row_dict.get('open')),
                        'high': normalize_number(row_dict.get('high')),
                        'low': normalize_number(row_dict.get('low')),
                        'close': normalize_number(row_dict.get('close')),
                        'volume': normalize_number(row_dict.get('volume')),
                        'amount': normalize_number(row_dict.get('amount')),
                    })
                bars_by_symbol[symbol] = [bar for bar in bars if bar.get('time') is not None]
        except Exception:
            record_error('_build_candles_bulk_payload')
            errors.append('get_market_data_failed')
            break
    return {
        'period': period,
        'start': start,
        'end': end,
        'dividend_type': dividend_type,
        'bars_by_symbol': bars_by_symbol,
        'errors': errors,
        'requested': len(symbols),
        'returned': len(bars_by_symbol),
    }


def _dataframe_to_records(df):
    """Convert a pandas DataFrame (or similar) to a list of dicts."""
    if df is None:
        return []
    iterrows = getattr(df, 'iterrows', None)
    if callable(iterrows):
        try:
            result = []
            for _, row in iterrows():
                record = row.to_dict() if hasattr(row, 'to_dict') else {}
                cleaned = {}
                for k, v in record.items():
                    cleaned[k] = normalize_number(v) if isinstance(v, (int, float)) or (hasattr(v, 'item') and callable(getattr(v, 'item'))) else v
                result.append(cleaned)
            return result
        except Exception:
            pass
    to_dict = getattr(df, 'to_dict', None)
    if callable(to_dict):
        try:
            return to_dict('records')
        except Exception:
            pass
    if isinstance(df, (list, tuple)):
        return list(df)
    return []


def build_longhubang_payload(runtime, symbol, start_time, end_time, record_error):
    context = runtime.context_ref
    if context is None:
        return {'symbol': symbol, 'records': [], 'error': 'context_unavailable'}
    get_longhubang = getattr(context, 'get_longhubang', None)
    if not callable(get_longhubang):
        return {'symbol': symbol, 'records': [], 'error': 'get_longhubang_unavailable'}
    try:
        result = get_longhubang([symbol], start_time, end_time)
        records = []
        rows = _dataframe_to_records(result)
        for row in rows:
            buy_booth = row.get('buyTraderBooth')
            sell_booth = row.get('sellTraderBooth')
            records.append({
                'reason': row.get('reason'),
                'close': normalize_number(row.get('close')),
                'spreadRate': normalize_number(row.get('spreadRate')),
                'turnoverVolume': normalize_number(row.get('TurnoverVolune')),
                'turnoverAmount': normalize_number(row.get('Turnover_Amount')),
                'buyTraderBooth': _dataframe_to_records(buy_booth),
                'sellTraderBooth': _dataframe_to_records(sell_booth),
            })
        return {'symbol': symbol, 'start': start_time, 'end': end_time, 'count': len(records), 'records': records}
    except Exception:
        record_error('build_longhubang_payload')
        return {'symbol': symbol, 'records': [], 'error': 'get_longhubang_failed'}


def build_signals_payload(runtime, symbol, normalize_quote_symbol):
    normalized_symbol = normalize_quote_symbol(symbol)
    points = []
    lowest_buy_price = None
    highest_buy_price = None
    for record in sorted(runtime.deal_index.values(), key=lambda item: str(item.get('time') or '')):
        if normalized_symbol is not None and record.get('symbol') != normalized_symbol:
            continue
        price = normalize_number(record.get('price'))
        side = record.get('side') or 'UNKNOWN'
        point = {
            'symbol': record.get('symbol'),
            'time': record.get('time'),
            'price': price,
            'volume': normalize_int(record.get('volume')),
            'side': side,
            'label': '买入' if side == 'BUY' else ('卖出' if side == 'SELL' else '成交'),
            'trade_id': record.get('trade_id'),
            'order_sys_id': record.get('order_sys_id'),
        }
        points.append(point)
        if side == 'BUY' and price is not None:
            if lowest_buy_price is None or price < lowest_buy_price:
                lowest_buy_price = price
            if highest_buy_price is None or price > highest_buy_price:
                highest_buy_price = price
    return {
        'symbol': normalized_symbol,
        'point_count': len(points),
        'lowest_buy_price': lowest_buy_price,
        'highest_buy_price': highest_buy_price,
        'points': points,
    }
