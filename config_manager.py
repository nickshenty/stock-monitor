"""
配置管理：自选标的与提醒规则

配置存在项目内 config/watchlist.json，明文 JSON（不含任何凭据，可直接手改）。
DeepSeek API key 从 ~/.dsh/.credentials.yaml 读取，不在本项目存储。
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_CONFIG = {
    "poll_interval": 60,          # 轮询间隔（秒）
    "notify": {
        "macos": True,            # macOS 通知中心
        "terminal": True,         # 终端打印
        "sound": "Ping",
    },
    "ai": {
        "enabled": True,
        "model": "deepseek-chat",
        "explain_alerts": True,   # 对触发的提醒做解读
        "daily_review": True,     # 收盘后复盘
    },
    "network": {
        # 行情请求走代理还是直连。默认 null=自动（先按系统代理设置，
        # 失败自动换另一条路并记住）；true=只走系统代理；false=只直连。
        # 不同网络下哪个能通不一样，一般不用改。
        "use_proxy": None,
    },
    # 全局默认规则，可被单个标的的 rules 覆盖
    "default_rules": {
        "change_pct_up": 3.0,     # 涨幅超过 3% 提醒
        "change_pct_down": -3.0,  # 跌幅超过 3% 提醒
        "volume_ratio": 2.0,      # 量比超过 2 提醒
        "ma_cross": ["5_20"],     # 监控 5/20 均线交叉
        "drawdown_pct": -8.0,     # 从20日高点回撤超 8% 提醒
    },
    "watchlist": [],
}


class ConfigManager:
    """配置读写"""

    def __init__(self, config_dir: Optional[str] = None):
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            self.config_dir = Path(__file__).parent / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "watchlist.json"
        self.state_file = self.config_dir / "alert_state.json"

    # ---------- 主配置 ----------

    def load(self) -> Dict:
        """读配置，缺字段用默认值补齐"""
        if not self.config_file.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))

        try:
            with open(self.config_file, encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"配置文件损坏：{self.config_file}\n{e}\n"
                f"修好它，或删除后重新运行 --add 配置"
            ) from e

        # 补齐缺失的顶层字段
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for key, value in config.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def save(self, config: Dict) -> None:
        """写配置（原子写，避免中断损坏文件）"""
        tmp = self.config_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_file)

    # ---------- 自选标的 ----------

    def add_item(self, secid: str, code: str, name: str,
                 cost: Optional[float] = None,
                 shares: Optional[int] = None,
                 rules: Optional[Dict] = None) -> Dict:
        """加入自选。已存在则更新"""
        config = self.load()
        watchlist: List[Dict] = config["watchlist"]

        entry = {
            "secid": secid,
            "code": code,
            "name": name,
            "cost": cost,
            "shares": shares,
            "rules": rules or {},
        }

        for i, item in enumerate(watchlist):
            if item["secid"] == secid:
                # 保留原有未显式覆盖的字段
                if cost is None:
                    entry["cost"] = item.get("cost")
                if shares is None:
                    entry["shares"] = item.get("shares")
                if not rules:
                    entry["rules"] = item.get("rules", {})
                watchlist[i] = entry
                self.save(config)
                return {"success": True, "action": "updated", "item": entry}

        watchlist.append(entry)
        self.save(config)
        return {"success": True, "action": "added", "item": entry}

    def remove_item(self, keyword: str) -> Dict:
        """按代码或名称移除自选"""
        config = self.load()
        watchlist = config["watchlist"]
        keyword = keyword.strip()

        for i, item in enumerate(watchlist):
            if keyword in (item["secid"], item["code"]) or keyword == item["name"]:
                removed = watchlist.pop(i)
                self.save(config)
                return {"success": True, "item": removed}

        return {"success": False, "error": f"自选中没有：{keyword}"}

    def effective_rules(self, item: Dict, config: Dict) -> Dict:
        """合并全局默认规则与标的自定义规则"""
        rules = dict(config.get("default_rules", {}))
        rules.update(item.get("rules") or {})
        return rules

    # ---------- 提醒状态（冷却去重） ----------

    def load_state(self) -> Dict:
        """读提醒状态。文件损坏时返回空状态，不阻塞盯盘"""
        if not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save_state(self, state: Dict) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)


def load_deepseek_key() -> Optional[str]:
    """从 DSH 配置读取 DeepSeek API key

    优先环境变量 DEEPSEEK_API_KEY，其次 ~/.dsh/.credentials.yaml
    """
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key.strip()

    creds = Path.home() / ".dsh" / ".credentials.yaml"
    if not creds.exists():
        return None

    try:
        with open(creds, encoding="utf-8") as f:
            for line in f:
                if "DEEPSEEK_API_KEY:" in line:
                    key = line.split(":", 1)[1].strip().strip('"').strip("'")
                    return key or None
    except OSError:
        return None
    return None
