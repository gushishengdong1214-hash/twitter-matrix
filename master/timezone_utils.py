"""时区显示工具:把数据库里的 VPS 本地时间字符串转换为北京时间显示。

数据库里所有时间都是 sqlite `datetime('now', 'localtime')` 写入,等同于
streamlit 进程的本地时区(中控 VPS 的系统时区)。本模块只在 UI 显示时把
这些字符串转换成 Asia/Shanghai (UTC+8) 字符串,不动数据库存储格式。
"""
import time
from datetime import datetime, timedelta


_BEIJING_OFFSET_SEC = 8 * 3600  # UTC+8


def _local_offset_seconds() -> int:
    """获取当前本地时区相对 UTC 的偏移(秒)。考虑夏令时。"""
    if time.daylight and time.localtime().tm_isdst:
        return -time.altzone
    return -time.timezone


def to_beijing(ts_str) -> str:
    """把 VPS 本地时间字符串转为北京时间字符串。

    输入示例:'2026-05-10 19:36:35' / None / 'None' / NaT / ''
    输出示例:'2026-05-11 07:36:35' / '-'
    """
    if ts_str is None:
        return '-'
    s = str(ts_str)
    if not s or s in ('None', 'NaT', '-'):
        return '-'
    try:
        dt_local = datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return s
    dt_utc = dt_local - timedelta(seconds=_local_offset_seconds())
    dt_beijing = dt_utc + timedelta(seconds=_BEIJING_OFFSET_SEC)
    return dt_beijing.strftime('%Y-%m-%d %I:%M:%S %p')
