"""
提醒输出：macOS 通知中心 + 终端

标的名称来自外部接口，拼入 AppleScript 前必须转义，
且通过 subprocess 列表参数调用（不经 shell），避免注入。
"""
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from rules import Alert


console = Console()

SEVERITY_STYLE = {
    "info": ("cyan", "ℹ️"),
    "warn": ("yellow", "⚠️"),
    "critical": ("red", "🚨"),
}


def _escape_applescript(text: str) -> str:
    """转义 AppleScript 字符串字面量中的特殊字符"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Notifier:
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self.use_macos = config.get("macos", True)
        self.use_terminal = config.get("terminal", True)
        self.sound = config.get("sound", "Ping")

    # ---------- macOS 通知 ----------

    def send_macos(self, title: str, message: str,
                   subtitle: str = "") -> Dict:
        """发送 macOS 通知中心消息"""
        if not self.use_macos:
            return {"success": False, "error": "macOS 通知已关闭"}

        script_parts = [
            f'display notification "{_escape_applescript(message)}"',
            f'with title "{_escape_applescript(title)}"',
        ]
        if subtitle:
            script_parts.append(f'subtitle "{_escape_applescript(subtitle)}"')
        if self.sound:
            script_parts.append(f'sound name "{_escape_applescript(self.sound)}"')

        script = " ".join(script_parts)

        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                return {"success": False,
                        "error": proc.stderr.strip() or f"退出码 {proc.returncode}"}
            return {"success": True}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "osascript 超时"}
        except FileNotFoundError:
            return {"success": False, "error": "未找到 osascript（非 macOS？）"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---------- 提醒推送 ----------

    def push_alerts(self, alerts: List[Alert],
                    ai_comment: Optional[str] = None) -> None:
        """推送一批提醒"""
        if not alerts:
            return

        if self.use_terminal:
            self._print_alerts(alerts, ai_comment)

        if self.use_macos:
            self._notify_alerts(alerts, ai_comment)

    def _print_alerts(self, alerts: List[Alert],
                      ai_comment: Optional[str]) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        console.print()
        console.print(Panel.fit(
            f"[bold]触发 {len(alerts)} 条提醒[/bold]  [dim]{stamp}[/dim]",
            border_style="yellow",
        ))

        for alert in alerts:
            style, icon = SEVERITY_STYLE.get(alert.severity, ("white", "•"))
            console.print(
                f"  {icon} [{style}]{alert.title}[/{style}]  "
                f"[dim]{alert.code}[/dim]"
            )
            console.print(f"     [dim]{alert.message}[/dim]")

        if ai_comment:
            console.print()
            console.print(Panel(
                ai_comment,
                title="[bold cyan]AI 解读[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
            ))

    def _notify_alerts(self, alerts: List[Alert],
                       ai_comment: Optional[str]) -> None:
        """合并成一条系统通知，避免刷屏"""
        if len(alerts) == 1:
            alert = alerts[0]
            body = ai_comment.strip() if ai_comment else alert.message
            self.send_macos(
                title=alert.title,
                message=body[:240],
                subtitle=f"{alert.name} {alert.code}",
            )
            return

        # 多条合并
        lines = [f"{a.title}" for a in alerts[:4]]
        if len(alerts) > 4:
            lines.append(f"…另有 {len(alerts) - 4} 条")
        body = ai_comment.strip() if ai_comment else "；".join(lines)

        self.send_macos(
            title=f"盯盘提醒 {len(alerts)} 条",
            message=body[:240],
            subtitle="；".join(lines)[:120],
        )

    # ---------- 行情看板 ----------

    def print_board(self, rows: List[Dict], title: str = "自选行情") -> None:
        """打印自选行情表

        rows: [{'name','code','price','change_pct','volume_ratio',
                'ma20_bias','profit_pct','note'}]
        """
        table = Table(title=title, box=box.ROUNDED, show_header=True,
                      header_style="bold magenta")
        table.add_column("名称", style="cyan", width=18, overflow="ellipsis")
        table.add_column("代码", style="dim", width=8)
        table.add_column("现价", justify="right", width=10)
        table.add_column("涨跌", justify="right", width=9)
        table.add_column("量比", justify="right", width=7)
        table.add_column("距MA20", justify="right", width=9)
        table.add_column("浮动盈亏", justify="right", width=11)

        for row in rows:
            pct = row.get("change_pct")
            if pct is None:
                pct_cell = "[dim]-[/dim]"
            elif pct > 0:
                pct_cell = f"[red]{pct:+.2f}%[/red]"
            elif pct < 0:
                pct_cell = f"[green]{pct:+.2f}%[/green]"
            else:
                pct_cell = f"{pct:+.2f}%"

            price = row.get("price")
            price_cell = f"{price:.3f}" if price else "[dim]-[/dim]"

            vr = row.get("volume_ratio")
            vr_cell = f"{vr:.2f}" if vr is not None else "[dim]-[/dim]"
            if vr is not None and vr >= 2.0:
                vr_cell = f"[bold yellow]{vr:.2f}[/bold yellow]"

            bias = row.get("ma20_bias")
            bias_cell = f"{bias:+.2f}%" if bias is not None else "[dim]-[/dim]"

            note = row.get("note")
            profit = row.get("profit_pct")
            if note:
                # 取数失败/停牌时盈亏无从计算，显示 - 而不是"未设成本"，
                # 否则会让设过成本的标的看起来像没设
                profit_cell = "[dim]-[/dim]"
            elif profit is None:
                profit_cell = "[dim]未设成本[/dim]"
            elif profit > 0:
                profit_cell = f"[red]{profit:+.2f}%[/red]"
            else:
                profit_cell = f"[green]{profit:+.2f}%[/green]"

            name_cell = row.get("name", "")
            if note:
                name_cell = f"[dim]{name_cell}[/dim]"
                price_cell = f"[dim]{note}[/dim]"

            table.add_row(name_cell, row.get("code", ""), price_cell,
                          pct_cell, vr_cell, bias_cell, profit_cell)

        console.print(table)
