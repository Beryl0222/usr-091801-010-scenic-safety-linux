"""时间解析与格式化：内部统一使用 UTC 纪元秒。"""

from datetime import datetime, timezone


def parse_time(value):
    """接受纪元秒或 ISO 8601 字符串，返回纪元秒；空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def fmt_time(timestamp):
    """把纪元秒格式化为 ISO 8601 UTC 字符串。"""
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
