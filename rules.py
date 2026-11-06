"""
规则引擎：确定性触发判定

所有触发条件都是可复现的算术比较，不含模型推断。
AI 只在触发之后参与解读（见 analyst.py）。

冷却机制两层：
1. 时间冷却——同一规则在 cooldown 秒内不重复提醒
2. 阈值进阶——行情继续恶化/改善超过 step 才再次提醒
"""
import time
from datetime import datetime
from typing import Dict, List, Optional


# 默认冷却时间（秒）
DEFAULT_COOLDOWN = 1800        # 30 分钟
# 阈值进阶步长：涨跌幅类再变动多少个百分点才重提醒
DEFAULT_PCT_STEP = 2.0

SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}


class Alert:
    """一条提醒"""

    def __init__(self, secid: str, name: str, code: str, rule: str,
                 severity: str, title: str, message: str,
                 metrics: Optional[Dict] = None, value: Optional[float] = None):
        self.secid = secid
        self.name = name
        self.code = code
        self.rule = rule              # 规则标识，用于冷却去重
        self.severity = severity      # info / warn / critical
        self.title = title
        self.message = message
        self.metrics = metrics or {}
        self.value = value            # 触发时的关键数值，用于阈值进阶
        self.ts = time.time()

    def to_dict(self) -> Dict:
        return {
            "secid": self.secid, "name": self.name, "code": self.code,
            "rule": self.rule, "severity": self.severity,
            "title": self.title, "message": self.message,
            "metrics": self.metrics, "value": self.value, "ts": self.ts,
        }

    def __repr__(self) -> str:
        return f"<Alert {self.name} {self.rule} {self.severity}>"


class RuleEngine:
    """规则判定与冷却控制"""

    def __init__(self, state: Optional[Dict] = None,
                 cooldown: int = DEFAULT_COOLDOWN):
        # state 结构: {f"{secid}:{rule}": {"ts": float, "value": float, "date": str}}
        self.state: Dict = state or {}
        self.cooldown = cooldown

    # ---------- 冷却判定 ----------

    def _state_key(self, secid: str, rule: str) -> str:
        return f"{secid}:{rule}"

    def _should_fire(self, secid: str, rule: str, value: Optional[float],
                     step: float = DEFAULT_PCT_STEP,
                     once_per_day: bool = False,
                     now: Optional[float] = None) -> bool:
        """是否应该发出提醒"""
        now = now if now is not None else time.time()
        key = self._state_key(secid, rule)
        last = self.state.get(key)

        if last is None:
            return True

        # 每日一次的规则（如均线交叉）
        if once_per_day:
            today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            return last.get("date") != today

        # 时间冷却内：只有恶化/改善超过 step 才重提醒
        if now - last.get("ts", 0) < self.cooldown:
            last_value = last.get("value")
            if value is None or last_value is None:
                return False
            return abs(value - last_value) >= step

        return True

    def _record(self, alert: Alert, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self.state[self._state_key(alert.secid, alert.rule)] = {
            "ts": now,
            "value": alert.value,
            "date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        }

    # ---------- 规则判定 ----------

    def evaluate(self, item: Dict, quote: Dict, metrics: Dict,
                 rules: Dict, now: Optional[float] = None) -> List[Alert]:
        """对单个标的跑全部规则

        Args:
            item: 自选配置 {secid, code, name, cost, shares, rules}
            quote: 实时报价
            metrics: indicators.compute_all 的结果
            rules: 已合并的生效规则

        Returns: 通过冷却判定、需要发出的 Alert 列表
        """
        if not quote.get("success"):
            return []

        secid = item["secid"]
        name = quote.get("name") or item.get("name", "")
        code = quote.get("code") or item.get("code", "")
        price = quote.get("price", 0)

        # 停牌不产生提醒
        if quote.get("suspended") or price <= 0:
            return []

        candidates: List[Alert] = []
        change_pct = quote.get("change_pct")

        def mk(rule, severity, title, message, value=None):
            return Alert(secid, name, code, rule, severity, title, message,
                         metrics=metrics, value=value)

        # --- 1. 涨跌幅 ---
        up = rules.get("change_pct_up")
        if up is not None and change_pct is not None and change_pct >= up:
            candidates.append(mk(
                "change_pct_up", "info",
                f"{name} 涨 {change_pct:.2f}%",
                f"现价 {price:.3f}，涨幅 {change_pct:+.2f}%，已达提醒线 {up:+.1f}%",
                value=change_pct,
            ))

        down = rules.get("change_pct_down")
        if down is not None and change_pct is not None and change_pct <= down:
            candidates.append(mk(
                "change_pct_down", "warn",
                f"{name} 跌 {change_pct:.2f}%",
                f"现价 {price:.3f}，跌幅 {change_pct:+.2f}%，已达提醒线 {down:+.1f}%",
                value=change_pct,
            ))

        # --- 2. 量比异动 ---
        vr_limit = rules.get("volume_ratio")
        vr = metrics.get("volume_ratio")
        if vr_limit is not None and vr is not None and vr >= vr_limit:
            candidates.append(mk(
                "volume_ratio", "info",
                f"{name} 放量 量比{vr:.2f}",
                f"量比 {vr:.2f}（阈值 {vr_limit:.1f}），"
                f"现价 {price:.3f} {change_pct:+.2f}%",
                value=vr,
            ))

        # --- 3. 均线交叉 ---
        for pair in rules.get("ma_cross") or []:
            cross = metrics.get(f"cross_{pair}")
            if not cross:
                continue
            fast, slow = pair.split("_")
            if cross == "golden":
                candidates.append(mk(
                    f"ma_cross_{pair}_golden", "info",
                    f"{name} MA{fast}上穿MA{slow}",
                    f"金叉：MA{fast}={metrics.get(f'ma{fast}'):.3f} "
                    f"上穿 MA{slow}={metrics.get(f'ma{slow}'):.3f}，现价 {price:.3f}",
                ))
            else:
                candidates.append(mk(
                    f"ma_cross_{pair}_death", "warn",
                    f"{name} MA{fast}下穿MA{slow}",
                    f"死叉：MA{fast}={metrics.get(f'ma{fast}'):.3f} "
                    f"下穿 MA{slow}={metrics.get(f'ma{slow}'):.3f}，现价 {price:.3f}",
                ))

        # --- 4. 从高点回撤 ---
        dd_limit = rules.get("drawdown_pct")
        dd = metrics.get("drawdown")
        if dd_limit is not None and dd and dd["drawdown_pct"] <= dd_limit:
            candidates.append(mk(
                "drawdown", "warn",
                f"{name} 回撤 {dd['drawdown_pct']:.2f}%",
                f"从 {dd['high_date']} 高点 {dd['high']:.3f} 回撤 "
                f"{dd['drawdown_pct']:.2f}%（阈值 {dd_limit:.1f}%），现价 {price:.3f}",
                value=dd["drawdown_pct"],
            ))

        # --- 5. 成本价止损/止盈（需配置 cost） ---
        cost = item.get("cost")
        if cost and cost > 0:
            profit_pct = (price - cost) / cost * 100

            stop_loss = rules.get("stop_loss_pct")
            if stop_loss is not None and profit_pct <= stop_loss:
                candidates.append(mk(
                    "stop_loss", "critical",
                    f"{name} 触及止损 {profit_pct:.2f}%",
                    f"成本 {cost:.3f} → 现价 {price:.3f}，浮动 {profit_pct:+.2f}%，"
                    f"已触及止损线 {stop_loss:+.1f}%",
                    value=profit_pct,
                ))

            take_profit = rules.get("take_profit_pct")
            if take_profit is not None and profit_pct >= take_profit:
                candidates.append(mk(
                    "take_profit", "info",
                    f"{name} 触及止盈 {profit_pct:.2f}%",
                    f"成本 {cost:.3f} → 现价 {price:.3f}，浮动 {profit_pct:+.2f}%，"
                    f"已触及止盈线 {take_profit:+.1f}%",
                    value=profit_pct,
                ))

        # --- 6. 绝对价格突破 ---
        price_above = rules.get("price_above")
        if price_above is not None and price >= price_above:
            candidates.append(mk(
                "price_above", "info",
                f"{name} 上破 {price_above}",
                f"现价 {price:.3f} 上破设定价 {price_above}",
                value=price,
            ))

        price_below = rules.get("price_below")
        if price_below is not None and price <= price_below:
            candidates.append(mk(
                "price_below", "warn",
                f"{name} 下破 {price_below}",
                f"现价 {price:.3f} 下破设定价 {price_below}",
                value=price,
            ))

        # --- 冷却过滤 ---
        fired: List[Alert] = []
        for alert in candidates:
            once_per_day = alert.rule.startswith("ma_cross_")
            step = rules.get("alert_step", DEFAULT_PCT_STEP)
            if self._should_fire(secid, alert.rule, alert.value,
                                 step=step, once_per_day=once_per_day, now=now):
                self._record(alert, now=now)
                fired.append(alert)

        return fired

    def sort_alerts(self, alerts: List[Alert]) -> List[Alert]:
        """按严重程度降序排列"""
        return sorted(alerts,
                      key=lambda a: SEVERITY_ORDER.get(a.severity, 0),
                      reverse=True)
