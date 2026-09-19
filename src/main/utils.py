from datetime import datetime, timezone
import math

UTC = timezone.utc

def dt(value, provider=False):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        if not provider:
            raise ValueError('Timestamp must contain Z or a UTC offset')
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def iso(value):
    return dt(value).isoformat().replace('+00:00', 'Z')


def now():
    return datetime.now(UTC)


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Non-finite number')
    return result

