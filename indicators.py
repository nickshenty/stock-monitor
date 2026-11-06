"""
技术指标计算

纯标准库算术实现，不依赖 pandas/numpy。
所有函数对数据不足的情况返回 None，而不是抛异常或给个假值。
"""
from datetime import datetime, time as dtime
from typing import Dict, List, Optional


# A股交易时段
MORNING_OPEN = dtime(9, 30)
MORNING_CLOSE = dtime(11, 30)
AFTERNOON_OPEN = dtime(13, 0)
AFTERNOON_CLOSE = dtime(15, 0)
TOTAL_TRADING_MINUTES = 240


def elapsed_trading_minutes(now: Optional[datetime] = None) -> int:
    """当日已开盘的交易分钟数（0~240）

    用于量比折算。盘前返回 0，盘后返回 240。
    """
    now = now or datetime.now()
    t = now.time()

    if t < MORNING_OPEN:
        return 0
    if t <= MORNING_CLOSE:
        return int((now - now.replace(hour=9, minute=30, second=0,
                                      microsecond=0)).total_seconds() // 60)
    if t < AFTERNOON_OPEN:
        return 120
    if t <= AFTERNOON_CLOSE:
        past = int((now - now.replace(hour=13, minute=0, second=0,
                                      microsecond=0)).total_seconds() // 60)
        return min(120 + past, TOTAL_TRADING_MINUTES)
    return TOTAL_TRADING_MINUTES


def is_trading_time(now: Optional[datetime] = None) -> bool:
    """是否在交易时段内（只判断时间，不判断节假日）"""
    now = now or datetime.now()
    if now.weekday() >= 5:          # 周六日
        return False
    t = now.time()
    return (MORNING_OPEN <= t <= MORNING_CLOSE) or \
           (AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE)


def ma(bars: List[Dict], n: int, offset: int = 0) -> Optional[float]:
    """n 日简单移动均线（收盘价）

    Args:
        bars: 正序日K线，最后一根是最新
        n: 周期
        offset: 向前偏移几根。0 = 最新均线，1 = 上一根均线（判交叉用）
    """
    end = len(bars) - offset
    if end < n or n <= 0:
        return None
    window = bars[end - n:end]
    return sum(b["close"] for b in window) / n


def detect_cross(bars: List[Dict], fast: int, slow: int) -> Optional[str]:
    """均线交叉检测

    Returns: 'golden'（金叉）/ 'death'（死叉）/ None（无交叉）
    """
    fast_now = ma(bars, fast, 0)
    slow_now = ma(bars, slow, 0)
    fast_prev = ma(bars, fast, 1)
    slow_prev = ma(bars, slow, 1)

    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return None

    if fast_prev <= slow_prev and fast_now > slow_now:
        return "golden"
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "death"
    return None


def volume_ratio(bars: List[Dict], today_volume: float,
                 now: Optional[datetime] = None) -> Optional[float]:
    """量比 = 当日每分钟均量 / 过去5日每分钟均量

    bars 应包含今天（最后一根），计算时会排除今天。
    开盘不足 1 分钟返回 None，避免除零放大。
    """
    minutes = elapsed_trading_minutes(now)
    if minutes < 1:
        return None

    # 排除今天，取过去 5 个交易日
    history = bars[:-1] if bars else []
    if len(history) < 5:
        return None
    past5 = history[-5:]

    past_avg_per_min = sum(b["volume"] for b in past5) / 5 / TOTAL_TRADING_MINUTES
    if past_avg_per_min <= 0:
        return None

    today_per_min = today_volume / minutes
    return today_per_min / past_avg_per_min


def drawdown_from_high(bars: List[Dict], price: float,
                       lookback: int = 20) -> Optional[Dict]:
    """从近期最高点的回撤

    Returns: {'high': 最高价, 'high_date': 日期, 'drawdown_pct': 回撤%}
    """
    if not bars or price <= 0:
        return None
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if not window:
        return None

    peak = max(window, key=lambda b: b["high"])
    if peak["high"] <= 0:
        return None

    return {
        "high": peak["high"],
        "high_date": peak["date"],
        "drawdown_pct": (price - peak["high"]) / peak["high"] * 100,
    }


def amplitude(quote: Dict) -> Optional[float]:
    """当日振幅% = (最高 - 最低) / 昨收"""
    high = quote.get("high", 0)
    low = quote.get("low", 0)
    prev_close = quote.get("prev_close", 0)
    if not all((high, low, prev_close)) or prev_close <= 0:
        return None
    return (high - low) / prev_close * 100


def compute_all(bars: List[Dict], quote: Dict,
                now: Optional[datetime] = None) -> Dict:
    """一次算出全部指标，供规则引擎和 AI 解读共用

    数据不足的指标值为 None，调用方需判空。
    """
    price = quote.get("price", 0)
    result: Dict = {
        "price": price,
        "change_pct": quote.get("change_pct"),
        "ma5": ma(bars, 5),
        "ma10": ma(bars, 10),
        "ma20": ma(bars, 20),
        "ma60": ma(bars, 60),
        "cross_5_20": detect_cross(bars, 5, 20),
        "cross_5_10": detect_cross(bars, 5, 10),
        "volume_ratio": volume_ratio(bars, quote.get("volume", 0), now),
        "amplitude": amplitude(quote),
        "drawdown": drawdown_from_high(bars, price),
        "trading_minutes": elapsed_trading_minutes(now),
    }

    # 价格相对各均线的偏离%
    for key in ("ma5", "ma10", "ma20", "ma60"):
        ma_val = result[key]
        if ma_val and ma_val > 0 and price > 0:
            result[f"{key}_bias"] = (price - ma_val) / ma_val * 100
        else:
            result[f"{key}_bias"] = None

    return result
