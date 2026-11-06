#!/usr/bin/env python3
"""
AI 炒股盯盘提醒系统

触发判定全部由确定性规则完成，AI 仅在触发后做事实解读与收盘复盘。
不含任何下单功能。

用法:
    python main.py --add 510300 --cost 4.00 --shares 10000
    python main.py --list
    python main.py --check              # 查一次就退出
    python main.py --watch              # 持续盯盘
    python main.py --review             # 生成当日复盘
"""
import argparse
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel

import indicators as ind
from analyst import Analyst
from config_manager import ConfigManager, load_deepseek_key
from datasource import EastMoneySource
from notifier import Notifier
from rules import Alert, RuleEngine


console = Console()
_stop_requested = False

# 连续多少轮取数失败后推系统通知
FAILURE_ALERT_ROUNDS = 3


def _handle_sigint(signum, frame):
    """Ctrl-C 优雅退出：标记后在循环检查点退出，保证状态落盘"""
    global _stop_requested
    _stop_requested = True
    console.print("\n[yellow]收到退出信号，正在保存状态…[/yellow]")


class KlineCache:
    """日K线缓存

    日K线按交易日缓存，盘中不重复拉取；
    但计算指标时会用实时价替换/追加当天那根，保证均线与量比跟得上盘面。
    """

    def __init__(self, source: EastMoneySource, limit: int = 60):
        self.source = source
        self.limit = limit
        self._cache: Dict[str, Dict] = {}   # secid -> {'date': str, 'bars': list}

    def get_bars(self, secid: str, quote: Optional[Dict] = None) -> List[Dict]:
        """取K线，并用实时报价校准当天那根"""
        today = datetime.now().strftime("%Y-%m-%d")
        cached = self._cache.get(secid)

        if not cached or cached["date"] != today:
            result = self.source.get_daily_kline(secid, limit=self.limit)
            if not result["success"]:
                return cached["bars"] if cached else []
            self._cache[secid] = {"date": today, "bars": result["bars"]}
            cached = self._cache[secid]

        bars = [dict(b) for b in cached["bars"]]
        if not quote or not quote.get("success") or quote.get("suspended"):
            return bars

        price = quote.get("price", 0)
        if price <= 0:
            return bars

        # 用实时数据覆盖当天那根K线。
        # 实时字段缺失（取到 0）时回退到K线原值，再退到现价，
        # 避免把当日高低价退化成现价、把振幅和回撤算小。
        today_bar = bars[-1] if bars and bars[-1]["date"] == today else None

        def pick(field: str) -> float:
            live = quote.get(field) or 0
            if live:
                return live
            if today_bar and today_bar.get(field):
                return today_bar[field]
            return price

        live_bar = {
            "date": today,
            "open": pick("open"),
            "close": price,
            "high": max(pick("high"), price),
            "low": min(pick("low"), price),
            "volume": quote.get("volume") or (
                today_bar.get("volume", 0) if today_bar else 0),
            "amount": quote.get("amount") or (
                today_bar.get("amount", 0) if today_bar else 0),
        }

        if today_bar is not None:
            # K线接口已含今日（盘中为动态值），用报价覆盖保持一致
            bars[-1] = live_bar
        else:
            bars.append(live_bar)

        return bars


def market_status(source: EastMoneySource) -> Dict:
    """判断当前市场状态

    用上证指数最新K线日期是否为今天来识别节假日，
    比硬编码节假日表可靠。
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    in_session = ind.is_trading_time(now)

    result = source.get_daily_kline("1.000001", limit=2)
    if not result["success"] or not result["bars"]:
        # 判不出来就按时间走，不阻塞盯盘
        return {"is_trading_day": now.weekday() < 5,
                "in_session": in_session, "note": "无法确认交易日"}

    latest = result["bars"][-1]["date"]
    is_trading_day = latest == today

    return {
        "is_trading_day": is_trading_day,
        "in_session": in_session and is_trading_day,
        "latest_bar": latest,
        "note": "" if is_trading_day else f"今日非交易日（最新交易日 {latest}）",
    }


def build_board(items: List[Dict], quotes: Dict[str, Dict],
                metrics_map: Dict[str, Dict]) -> List[Dict]:
    """组装行情看板数据"""
    rows = []
    for item in items:
        secid = item["secid"]
        quote = quotes.get(secid, {})
        metrics = metrics_map.get(secid, {})

        if not quote.get("success"):
            rows.append({
                "name": item.get("name", secid),
                "code": item.get("code", ""),
                "note": "获取失败",
            })
            continue

        if quote.get("suspended"):
            rows.append({
                "name": quote.get("name") or item.get("name", ""),
                "code": quote.get("code") or item.get("code", ""),
                "note": "停牌",
            })
            continue

        price = quote.get("price", 0)
        cost = item.get("cost")
        profit_pct = ((price - cost) / cost * 100) if cost and cost > 0 else None

        rows.append({
            "name": quote.get("name") or item.get("name", ""),
            "code": quote.get("code") or item.get("code", ""),
            "price": price,
            "change_pct": quote.get("change_pct"),
            "volume_ratio": metrics.get("volume_ratio"),
            "ma20_bias": metrics.get("ma20_bias"),
            "profit_pct": profit_pct,
        })
    return rows


def scan_once(cm: ConfigManager, source: EastMoneySource,
              cache: KlineCache, engine: RuleEngine,
              config: Dict) -> Dict:
    """扫描一轮：取行情 → 算指标 → 跑规则

    Returns:
        {'rows': 看板数据, 'alerts': 触发的提醒, 'metrics_map': 指标,
         'total': 标的数, 'failed': 取数失败数, 'errors': 失败原因样本}
    """
    items = config.get("watchlist", [])
    if not items:
        return {"rows": [], "alerts": [], "metrics_map": {},
                "total": 0, "failed": 0, "errors": []}

    secids = [item["secid"] for item in items]
    quotes = source.get_quotes(secids)

    metrics_map: Dict[str, Dict] = {}
    all_alerts: List[Alert] = []
    errors: List[str] = []

    for item in items:
        secid = item["secid"]
        quote = quotes.get(secid, {})
        if not quote.get("success"):
            # 取数失败必须计数并上报，不能静默跳过：
            # 盯盘工具最危险的失败模式是"看起来在跑，其实没在看"
            errors.append(f"{item.get('name', secid)}: "
                          f"{quote.get('error', '未知错误')}")
            continue

        bars = cache.get_bars(secid, quote)
        metrics = ind.compute_all(bars, quote)
        metrics_map[secid] = metrics

        rules = cm.effective_rules(item, config)
        all_alerts.extend(engine.evaluate(item, quote, metrics, rules))

    rows = build_board(items, quotes, metrics_map)
    return {"rows": rows, "alerts": engine.sort_alerts(all_alerts),
            "metrics_map": metrics_map, "total": len(items),
            "failed": len(errors), "errors": errors[:3]}


def record_day_alerts(state: Dict, alerts: List[Alert]) -> Dict:
    """把当日提醒追加到 state 的 _day_log，供复盘使用

    直接写入传入的 state（即 engine.state），由调用方统一落盘。
    不能单独存盘：否则随后的 save_state(engine.state) 会把 _day_log 覆盖掉。
    """
    if not alerts:
        return state
    today = datetime.now().strftime("%Y-%m-%d")
    log = state.setdefault("_day_log", {})
    day_list = log.setdefault(today, [])
    for alert in alerts:
        day_list.append({
            "name": alert.name, "code": alert.code, "title": alert.title,
            "severity": alert.severity,
            "time": datetime.fromtimestamp(alert.ts).strftime("%H:%M:%S"),
        })
    # 只保留最近 10 天
    for old in sorted(log.keys())[:-10]:
        log.pop(old, None)
    return state


# ---------- 子命令 ----------

def cmd_add(args, cm: ConfigManager, source: EastMoneySource) -> int:
    resolved = source.resolve(args.add)
    if not resolved["success"]:
        console.print(f"[red]✗ {resolved['error']}[/red]")
        return 1

    rules = {}
    if args.stop_loss is not None:
        rules["stop_loss_pct"] = args.stop_loss
    if args.take_profit is not None:
        rules["take_profit_pct"] = args.take_profit
    if args.price_above is not None:
        rules["price_above"] = args.price_above
    if args.price_below is not None:
        rules["price_below"] = args.price_below

    result = cm.add_item(
        secid=resolved["secid"], code=resolved["code"], name=resolved["name"],
        cost=args.cost, shares=args.shares, rules=rules or None,
    )
    item = result["item"]
    action = "已更新" if result["action"] == "updated" else "已加入"
    console.print(
        f"[green]✓ {action}自选：{item['name']}（{item['code']}）[/green]"
    )
    if item.get("cost"):
        console.print(f"  成本价 {item['cost']}", end="")
        if item.get("shares"):
            console.print(f"  持仓 {item['shares']} 股", end="")
        console.print()
    if rules:
        console.print(f"  自定义规则 {rules}")
    if not item.get("cost") and (args.stop_loss is not None
                                 or args.take_profit is not None):
        console.print("  [yellow]注意：止损/止盈需要设置 --cost 才会生效[/yellow]")
    return 0


def cmd_remove(args, cm: ConfigManager) -> int:
    result = cm.remove_item(args.remove)
    if result["success"]:
        item = result["item"]
        console.print(f"[green]✓ 已移除：{item['name']}（{item['code']}）[/green]")
        return 0
    console.print(f"[red]✗ {result['error']}[/red]")
    return 1


def cmd_list(cm: ConfigManager) -> int:
    config = cm.load()
    items = config.get("watchlist", [])
    if not items:
        console.print("[yellow]自选为空。用 --add 添加，例如：[/yellow]")
        console.print("  python main.py --add 510300 --cost 4.00 --shares 10000")
        return 0

    console.print(Panel.fit("[bold cyan]自选配置[/bold cyan]", border_style="cyan"))
    for item in items:
        console.print(f"\n[cyan]{item['name']}[/cyan] [dim]{item['code']}  "
                      f"{item['secid']}[/dim]")
        if item.get("cost"):
            line = f"  成本 {item['cost']}"
            if item.get("shares"):
                line += f"   持仓 {item['shares']} 股"
            console.print(line)
        effective = cm.effective_rules(item, config)
        console.print(f"  [dim]生效规则 {effective}[/dim]")

    console.print(f"\n[dim]全局默认规则 {config.get('default_rules')}[/dim]")
    console.print(f"[dim]轮询间隔 {config.get('poll_interval')}s   "
                  f"AI 解读 {'开' if config.get('ai', {}).get('enabled') else '关'}[/dim]")
    return 0


def cmd_check(cm: ConfigManager, source: EastMoneySource,
              config: Dict, use_ai: bool) -> int:
    """查一次就退出"""
    status = market_status(source)
    if status.get("note"):
        console.print(f"[dim]{status['note']}[/dim]")
    elif not status["in_session"]:
        console.print("[dim]当前非交易时段，显示最近收盘数据[/dim]")

    engine = RuleEngine(state=cm.load_state(),
                        cooldown=config.get("poll_interval", 60) * 30)
    cache = KlineCache(source)
    notifier = Notifier(config.get("notify"))

    result = scan_once(cm, source, cache, engine, config)
    if not result["rows"]:
        console.print("[yellow]自选为空，先用 --add 添加标的[/yellow]")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notifier.print_board(result["rows"], title=f"自选行情  {stamp}")

    # 取数失败必须显式上报：否则"无规则触发"会被误读成"一切正常"
    if result["failed"]:
        if result["failed"] == result["total"]:
            console.print(f"[red]✗ 行情获取全部失败（{result['failed']}/"
                          f"{result['total']}），本次检查结果不可信[/red]")
        else:
            console.print(f"[yellow]⚠️ {result['failed']}/{result['total']} "
                          f"个标的取数失败，这些标的未参与规则判断[/yellow]")
        for err in result["errors"]:
            console.print(f"[dim]    {err[:150]}[/dim]")
        if result["failed"] == result["total"]:
            return 1

    alerts = result["alerts"]
    if not alerts:
        console.print("[dim]无规则触发[/dim]")
        cm.save_state(engine.state)
        return 0

    ai_comment = None
    if use_ai:
        analyst = Analyst(load_deepseek_key(),
                          model=config.get("ai", {}).get("model", "deepseek-chat"))
        with console.status("[cyan]AI 解读中…[/cyan]"):
            explained = analyst.explain_alerts(alerts)
        if explained["success"]:
            ai_comment = explained["content"]
        else:
            console.print(f"[dim]AI 解读失败（不影响提醒）：{explained['error']}[/dim]")

    notifier.push_alerts(alerts, ai_comment)
    record_day_alerts(engine.state, alerts)
    cm.save_state(engine.state)
    return 0


def cmd_watch(cm: ConfigManager, source: EastMoneySource,
              config: Dict, use_ai: bool) -> int:
    """持续盯盘"""
    interval = max(int(config.get("poll_interval", 60)), 10)
    engine = RuleEngine(state=cm.load_state(), cooldown=interval * 30)
    cache = KlineCache(source)
    notifier = Notifier(config.get("notify"))
    analyst = Analyst(
        load_deepseek_key(),
        model=config.get("ai", {}).get("model", "deepseek-chat"),
    ) if use_ai else None

    items = config.get("watchlist", [])
    if not items:
        console.print("[yellow]自选为空，先用 --add 添加标的[/yellow]")
        return 0

    signal.signal(signal.SIGINT, _handle_sigint)

    console.print(Panel.fit(
        f"[bold cyan]盯盘启动[/bold cyan]  {len(items)} 个标的  "
        f"间隔 {interval}s  AI解读 {'开' if use_ai else '关'}\n"
        f"[dim]Ctrl-C 退出[/dim]",
        border_style="cyan",
    ))

    day_checked: Optional[str] = None
    rounds = 0
    consecutive_failures = 0

    while not _stop_requested:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # 每天首轮确认交易日
        if day_checked != today:
            status = market_status(source)
            day_checked = today
            if not status["is_trading_day"]:
                console.print(f"[dim]{now:%H:%M:%S}  "
                              f"{status.get('note', '非交易日')}，休眠中[/dim]")

        if not ind.is_trading_time(now):
            # 非交易时段降频等待
            for _ in range(min(interval, 60)):
                if _stop_requested:
                    break
                time.sleep(1)
            continue

        rounds += 1
        try:
            result = scan_once(cm, source, cache, engine, config)
        except Exception as e:
            consecutive_failures += 1
            console.print(f"[red]{now:%H:%M:%S}  扫描出错：{e}[/red]")
            time.sleep(interval)
            continue

        # 取数健康度：全部失败说明已经"假装在盯盘"，必须让用户知道
        if result["total"] and result["failed"] == result["total"]:
            consecutive_failures += 1
            console.print(f"[red]{now:%H:%M:%S}  第{rounds}轮  "
                          f"行情获取全部失败（{result['failed']}/{result['total']}）"
                          f"[/red]")
            for err in result["errors"]:
                console.print(f"[dim]    {err[:150]}[/dim]")

            # 连续失败到阈值就推系统通知：此时用户大概不在看终端
            if consecutive_failures == FAILURE_ALERT_ROUNDS:
                sent = notifier.send_macos(
                    title="盯盘异常",
                    message=f"已连续 {consecutive_failures} 轮无法获取行情，"
                            f"提醒功能当前失效",
                    subtitle="请检查网络连接",
                )
                if sent.get("success"):
                    console.print("[yellow]    已发送异常通知[/yellow]")
                else:
                    console.print(f"[yellow]    未能发送系统通知"
                                  f"（{sent.get('error', '未知原因')}），请留意终端[/yellow]")
            for _ in range(interval):
                if _stop_requested:
                    break
                time.sleep(1)
            continue

        if consecutive_failures >= FAILURE_ALERT_ROUNDS:
            console.print(f"[green]{now:%H:%M:%S}  行情获取已恢复[/green]")
            notifier.send_macos(title="盯盘恢复",
                                message="行情获取已恢复，提醒功能正常")
        consecutive_failures = 0

        alerts = result["alerts"]
        if alerts:
            ai_comment = None
            if analyst:
                explained = analyst.explain_alerts(alerts)
                if explained["success"]:
                    ai_comment = explained["content"]
                else:
                    console.print(f"[dim]AI 解读失败（不影响提醒）："
                                  f"{explained['error']}[/dim]")
            notifier.push_alerts(alerts, ai_comment)
            record_day_alerts(engine.state, alerts)
            cm.save_state(engine.state)
        else:
            # 每 10 轮打一次心跳，确认程序还活着
            if rounds % 10 == 1:
                priced = [r for r in result["rows"] if r.get("price")]
                total = len(priced)
                ups = sum(1 for r in priced if (r.get("change_pct") or 0) > 0)
                line = (f"[dim]{now:%H:%M:%S}  第{rounds}轮  "
                        f"{total}个标的  {ups}涨{total - ups}跌  无触发[/dim]")
                if result["failed"]:
                    line += f"  [yellow]{result['failed']}个取数失败[/yellow]"
                console.print(line)

        for _ in range(interval):
            if _stop_requested:
                break
            time.sleep(1)

    cm.save_state(engine.state)
    console.print("[green]✓ 状态已保存，盯盘结束[/green]")
    return 0


def cmd_review(cm: ConfigManager, source: EastMoneySource,
               config: Dict, use_ai: bool) -> int:
    """当日复盘"""
    engine = RuleEngine(state=cm.load_state())
    cache = KlineCache(source)

    result = scan_once(cm, source, cache, engine, config)
    if not result["rows"]:
        console.print("[yellow]自选为空[/yellow]")
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    Notifier(config.get("notify")).print_board(
        result["rows"], title=f"收盘快照  {stamp}")

    day_log = (cm.load_state().get("_day_log") or {}).get(today, [])
    if day_log:
        console.print(f"\n[bold]今日触发 {len(day_log)} 条提醒[/bold]")
        for entry in day_log:
            console.print(f"  [dim]{entry['time']}[/dim]  "
                          f"{entry['title']}  [dim]({entry['severity']})[/dim]")
    else:
        console.print("\n[dim]今日无提醒触发[/dim]")

    if not use_ai:
        return 0

    analyst = Analyst(load_deepseek_key(),
                      model=config.get("ai", {}).get("model", "deepseek-chat"))
    with console.status("[cyan]生成复盘…[/cyan]"):
        review = analyst.daily_review(result["rows"], day_log)

    if review["success"]:
        console.print()
        console.print(Panel(review["content"],
                            title="[bold cyan]当日复盘[/bold cyan]",
                            border_style="cyan"))
    else:
        console.print(f"[dim]复盘生成失败：{review['error']}[/dim]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI 炒股盯盘提醒系统（只提醒，不下单）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py --add 510300 --cost 4.00 --shares 10000
  python main.py --add 茅台 --cost 1520 --stop-loss -5
  python main.py --add 上证指数
  python main.py --list
  python main.py --check
  python main.py --watch
  python main.py --review
""",
    )
    parser.add_argument("--add", metavar="标的", help="加入自选（代码/名称）")
    parser.add_argument("--remove", metavar="标的", help="移出自选")
    parser.add_argument("--list", action="store_true", help="查看自选与规则")
    parser.add_argument("--check", action="store_true", help="查一次行情就退出")
    parser.add_argument("--watch", action="store_true", help="持续盯盘")
    parser.add_argument("--review", action="store_true", help="生成当日复盘")

    parser.add_argument("--cost", type=float, help="成本价（止损/止盈需要）")
    parser.add_argument("--shares", type=int, help="持仓股数")
    parser.add_argument("--stop-loss", type=float, metavar="PCT",
                        help="止损线%%，如 -5")
    parser.add_argument("--take-profit", type=float, metavar="PCT",
                        help="止盈线%%，如 10")
    parser.add_argument("--price-above", type=float, metavar="价格",
                        help="上破该价格提醒")
    parser.add_argument("--price-below", type=float, metavar="价格",
                        help="下破该价格提醒")

    parser.add_argument("--no-ai", action="store_true", help="本次不调用 AI")
    parser.add_argument("--interval", type=int, metavar="秒",
                        help="覆盖轮询间隔")
    parser.add_argument("--config-dir", help="配置目录")

    args = parser.parse_args()

    if not any([args.add, args.remove, args.list, args.check,
                args.watch, args.review]):
        parser.print_help()
        return 0

    cm = ConfigManager(args.config_dir)
    # 默认直连东财；若用户所在网络必须走代理，可在配置里设 network.use_proxy
    source = EastMoneySource(
        use_proxy=bool(cm.load().get("network", {}).get("use_proxy", False))
    )

    try:
        if args.add:
            return cmd_add(args, cm, source)
        if args.remove:
            return cmd_remove(args, cm)
        if args.list:
            return cmd_list(cm)

        config = cm.load()
        if args.interval:
            config["poll_interval"] = args.interval

        use_ai = (not args.no_ai) and config.get("ai", {}).get("enabled", True)
        if use_ai and not load_deepseek_key():
            console.print("[yellow]未找到 DeepSeek API key，本次不做 AI 解读[/yellow]")
            console.print("[dim]可设置环境变量 DEEPSEEK_API_KEY，"
                          "或配置 ~/.dsh/.credentials.yaml[/dim]")
            use_ai = False

        if args.check:
            return cmd_check(cm, source, config, use_ai)
        if args.watch:
            return cmd_watch(cm, source, config, use_ai)
        if args.review:
            return cmd_review(cm, source, config, use_ai)

    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        return 130
    except Exception as e:
        console.print(f"[red]未预期错误：{e}[/red]")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
