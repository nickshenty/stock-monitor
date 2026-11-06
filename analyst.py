"""
AI 解读：DeepSeek 对已触发提醒做事实解释与收盘复盘

边界（硬约束，写在系统提示里）：
- 只解释"已经发生了什么"，基于传入的确定性指标
- 不预测价格、不给买卖指令、不推荐标的
- 指标数据不足时明确说不知道，不编造

AI 调用失败不影响提醒发出——提醒本身由规则引擎保证。
"""
import json
import requests
from typing import Dict, List, Optional

from rules import Alert


API_URL = "https://api.deepseek.com/chat/completions"

SYSTEM_PROMPT = """你是盯盘助手，只做事实解释，面向有经验的个人投资者。

你收到的是「确定性规则已经触发的提醒」和触发时的指标快照。你的任务：

1. 用 2-4 句话说明这些提醒在盘面上意味着什么，必须引用给你的具体数字
2. 多条提醒同时出现时，指出它们之间的关联（比如同涨同跌、是否同一板块）
3. 指标数据缺失（值为 null）时，直接说该指标无数据，不要推测

严格禁止：
- 预测后续价格走势、给出目标价
- 给出买入/卖出/加仓/减仓等操作建议
- 评价标的"好/坏"、"值得买/不值得买"
- 使用"建议关注""可考虑""或将""有望"这类暗示方向的措辞

语言：简体中文，口语化但不啰嗦，不用 markdown 标题和列表符号，直接写成段落。
不要重复提醒标题里已有的信息，补充规则之外的观察。"""

REVIEW_PROMPT = """你是盯盘助手，为用户生成当日自选标的收盘复盘。

基于给你的行情数据和当日触发的提醒，写一段 150 字以内的复盘：
1. 自选整体表现（涨跌家数、最强最弱）
2. 今天触发过的提醒回顾，以及对应标的收盘情况
3. 指标上值得记录的客观事实（如均线位置变化、放量情况）

严格禁止：预测明日走势、给买卖建议、推荐标的、使用暗示方向的措辞。
语言：简体中文，写成连贯段落，不用 markdown 标题和列表符号。"""


class Analyst:
    """DeepSeek 解读封装"""

    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 timeout: int = 30):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _call(self, system: str, user: str,
              max_tokens: int = 400) -> Dict:
        """调用 DeepSeek chat completions"""
        if not self.api_key:
            return {"success": False, "error": "未配置 DeepSeek API key"}

        try:
            resp = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,     # 解读要稳定，不要发散
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                return {"success": False,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return {"success": False, "error": "接口未返回内容"}

            content = (choices[0].get("message") or {}).get("content", "").strip()
            if not content:
                return {"success": False, "error": "返回内容为空"}

            usage = data.get("usage") or {}
            return {
                "success": True,
                "content": content,
                "tokens": usage.get("total_tokens", 0),
            }

        except requests.Timeout:
            return {"success": False, "error": f"请求超时（{self.timeout}s）"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---------- 提醒解读 ----------

    @staticmethod
    def _format_metrics(metrics: Dict) -> Dict:
        """挑出 AI 需要的指标，数值取整，None 保留为 null"""
        def r(value, digits=3):
            return round(value, digits) if isinstance(value, (int, float)) else None

        dd = metrics.get("drawdown") or {}
        return {
            "现价": r(metrics.get("price")),
            "涨跌幅%": r(metrics.get("change_pct"), 2),
            "MA5": r(metrics.get("ma5")),
            "MA10": r(metrics.get("ma10")),
            "MA20": r(metrics.get("ma20")),
            "MA60": r(metrics.get("ma60")),
            "距MA20偏离%": r(metrics.get("ma20_bias"), 2),
            "距MA60偏离%": r(metrics.get("ma60_bias"), 2),
            "量比": r(metrics.get("volume_ratio"), 2),
            "振幅%": r(metrics.get("amplitude"), 2),
            "20日高点": r(dd.get("high")),
            "较20日高点回撤%": r(dd.get("drawdown_pct"), 2),
            "已开盘分钟": metrics.get("trading_minutes"),
        }

    def explain_alerts(self, alerts: List[Alert]) -> Dict:
        """解读一批提醒"""
        if not alerts:
            return {"success": False, "error": "无提醒可解读"}

        payload = []
        for alert in alerts:
            payload.append({
                "标的": f"{alert.name}({alert.code})",
                "提醒": alert.title,
                "规则说明": alert.message,
                "严重度": alert.severity,
                "指标快照": self._format_metrics(alert.metrics),
            })

        user = (
            "以下是刚刚触发的提醒及触发时的指标快照：\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n\n请按系统提示解释这些提醒。"
        )
        return self._call(SYSTEM_PROMPT, user, max_tokens=400)

    # ---------- 收盘复盘 ----------

    def daily_review(self, board_rows: List[Dict],
                     day_alerts: List[Dict]) -> Dict:
        """生成当日复盘

        Args:
            board_rows: 收盘行情快照
            day_alerts: 当日所有触发过的提醒（dict 形式）
        """
        if not board_rows:
            return {"success": False, "error": "无行情数据"}

        quotes = []
        for row in board_rows:
            quotes.append({
                "标的": f"{row.get('name')}({row.get('code')})",
                "收盘": row.get("price"),
                "涨跌幅%": row.get("change_pct"),
                "量比": row.get("volume_ratio"),
                "距MA20%": row.get("ma20_bias"),
                "浮动盈亏%": row.get("profit_pct"),
            })

        alerts_brief = [
            {"标的": a.get("name"), "提醒": a.get("title"),
             "严重度": a.get("severity")}
            for a in day_alerts
        ]

        user = (
            "收盘行情：\n"
            + json.dumps(quotes, ensure_ascii=False, indent=2)
            + "\n\n今日触发的提醒：\n"
            + (json.dumps(alerts_brief, ensure_ascii=False, indent=2)
               if alerts_brief else "今日无提醒触发")
            + "\n\n请按系统提示生成复盘。"
        )
        return self._call(REVIEW_PROMPT, user, max_tokens=500)
