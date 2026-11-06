"""
行情数据源：东方财富公开接口

提供自选标的的代码解析、实时报价、历史日K线。
所有方法返回 dict，失败时带 error 字段，不抛异常。
"""
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, List, Optional


# 东方财富字段编号 -> 含义
# 单只报价接口 (qt/stock/get)
_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,f169,f170"
# 批量报价接口 (qt/ulist.np/get)
# f15/f16/f17 = 最高/最低/今开，振幅和当日K线校准需要
_LIST_FIELDS = "f2,f3,f4,f5,f6,f12,f13,f14,f15,f16,f17,f18"
# 日K线接口
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 常见指数代码 -> secid（指数代码和股票代码会撞号，需要显式指定）
_INDEX_MAP = {
    "000001": "1.000001",  # 上证指数（和平安银行 0.000001 撞号）
    "000300": "1.000300",  # 沪深300
    "000905": "1.000905",  # 中证500
    "000016": "1.000016",  # 上证50
    "399001": "0.399001",  # 深证成指
    "399006": "0.399006",  # 创业板指
    "399905": "0.399905",  # 中证500(深)
    "899050": "2.899050",  # 北证50
}

# 别名 -> 代码，方便直接输中文
_ALIAS = {
    "上证指数": "000001", "上证": "000001", "沪指": "000001",
    "深证成指": "399001", "深成指": "399001",
    "创业板指": "399006", "创业板": "399006",
    "沪深300": "000300", "中证500": "000905", "上证50": "000016",
    "北证50": "899050",
}


class EastMoneySource:
    """东方财富行情源"""

    SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
    QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
    LIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self, timeout: int = 10, retries: int = 2,
                 use_proxy: bool = False):
        self.timeout = timeout
        self.session = requests.Session()
        # 东财是国内站点，直连即可。很多人机器上开着 Clash 之类的系统代理，
        # requests 会自动读取并把行情请求也绕进代理——既无必要，又多一个
        # 故障点（代理一抖整轮盯盘就取不到数）。默认绕开系统代理设置。
        self.session.trust_env = use_proxy
        self.session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://quote.eastmoney.com/",
        })
        # 网络抖动、东财偶发 5xx 都会让单轮盯盘失效，
        # 这里做短重试：盯盘是长时间运行，不能一次抖动就瞎掉。
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            backoff_factor=0.5,          # 0.5s, 1.0s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4,
                              pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._secid_cache: Dict[str, str] = {}

    # ---------- 代码解析 ----------

    def resolve(self, keyword: str) -> Dict:
        """把用户输入（代码 / 名称 / 别名）解析成 secid

        Returns:
            {'success': True, 'secid': '1.510300', 'code': '510300',
             'name': '沪深300ETF华泰柏瑞', 'type': '基金'}
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return {"success": False, "error": "标的不能为空"}

        if keyword in self._secid_cache:
            cached = self._secid_cache[keyword]
            return {"success": True, **cached}

        # 中文别名
        if keyword in _ALIAS:
            keyword = _ALIAS[keyword]

        # 已经是 secid 格式（如 1.510300）
        if re.fullmatch(r"[0-2]\.\d{6}", keyword):
            info = {"secid": keyword, "code": keyword.split(".")[1],
                    "name": keyword.split(".")[1], "type": "未知"}
            quote = self.get_quote(keyword)
            if quote["success"]:
                info["name"] = quote["name"]
            self._secid_cache[keyword] = info
            return {"success": True, **info}

        # 已知指数代码
        if keyword in _INDEX_MAP:
            secid = _INDEX_MAP[keyword]
            quote = self.get_quote(secid)
            info = {
                "secid": secid,
                "code": keyword,
                "name": quote.get("name", keyword) if quote["success"] else keyword,
                "type": "指数",
            }
            self._secid_cache[keyword] = info
            return {"success": True, **info}

        # 走搜索接口
        try:
            resp = self.session.get(
                self.SEARCH_URL,
                params={
                    "input": keyword,
                    "type": "14",
                    "token": "D43BF722C8E33BDC906FB84D85E326E8",
                    "count": "5",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            items = (data.get("QuotationCodeTable") or {}).get("Data") or []
            if not items:
                return {"success": False, "error": f"未找到标的：{keyword}"}

            # 优先沪深市场（MktNum 0/1），排除港股美股
            hs = [i for i in items if i.get("MktNum") in ("0", "1", "2")]
            item = (hs or items)[0]
            quote_id = item.get("QuoteID", "")
            if not quote_id:
                return {"success": False, "error": f"标的无行情ID：{keyword}"}

            info = {
                "secid": quote_id,
                "code": item.get("UnifiedCode") or item.get("Code", ""),
                "name": item.get("Name", keyword),
                "type": item.get("SecurityTypeName", "未知"),
            }
            self._secid_cache[keyword] = info
            return {"success": True, **info}

        except Exception as e:
            return {"success": False, "error": f"解析失败：{e}"}

    # ---------- 实时报价 ----------

    @staticmethod
    def _num(value, default=0.0) -> float:
        """东财停牌/无数据时返回 '-'，统一转成数字"""
        if value in (None, "-", ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_quote(self, secid: str) -> Dict:
        """单只标的实时报价"""
        try:
            resp = self.session.get(
                self.QUOTE_URL,
                params={"secid": secid, "fields": _QUOTE_FIELDS,
                        "invt": "2", "fltt": "2"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data")
            if not data:
                return {"success": False, "secid": secid,
                        "error": "接口未返回数据（代码可能无效）"}

            price = self._num(data.get("f43"))
            prev_close = self._num(data.get("f60"))
            return {
                "success": True,
                "secid": secid,
                "code": data.get("f57", ""),
                "name": data.get("f58", ""),
                "price": price,
                "high": self._num(data.get("f44")),
                "low": self._num(data.get("f45")),
                "open": self._num(data.get("f46")),
                "volume": self._num(data.get("f47")),       # 手
                "amount": self._num(data.get("f48")),       # 元
                "prev_close": prev_close,
                "change": self._num(data.get("f169")),
                "change_pct": self._num(data.get("f170")),
                "suspended": price == 0,
                "ts": time.time(),
            }
        except Exception as e:
            return {"success": False, "secid": secid, "error": str(e)}

    def get_quotes(self, secids: List[str]) -> Dict[str, Dict]:
        """批量实时报价，一次请求拿全部自选

        Returns: {secid: quote_dict}
        """
        if not secids:
            return {}

        result: Dict[str, Dict] = {}
        # 东财批量接口对数量有限制，分批处理
        for i in range(0, len(secids), 50):
            batch = secids[i:i + 50]
            try:
                resp = self.session.get(
                    self.LIST_URL,
                    params={"secids": ",".join(batch), "fields": _LIST_FIELDS,
                            "invt": "2", "fltt": "2"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                diff = (payload.get("data") or {}).get("diff") or []

                for item in diff:
                    code = item.get("f12", "")
                    market = item.get("f13", "")
                    secid = f"{market}.{code}"
                    price = self._num(item.get("f2"))
                    result[secid] = {
                        "success": True,
                        "secid": secid,
                        "code": code,
                        "name": item.get("f14", ""),
                        "price": price,
                        "change_pct": self._num(item.get("f3")),
                        "change": self._num(item.get("f4")),
                        "volume": self._num(item.get("f5")),
                        "amount": self._num(item.get("f6")),
                        "high": self._num(item.get("f15")),
                        "low": self._num(item.get("f16")),
                        "open": self._num(item.get("f17")),
                        "prev_close": self._num(item.get("f18")),
                        "suspended": price == 0,
                        "ts": time.time(),
                    }
            except Exception as e:
                for secid in batch:
                    result.setdefault(secid, {
                        "success": False, "secid": secid, "error": str(e)
                    })

        # 补齐接口没返回的标的
        for secid in secids:
            result.setdefault(secid, {
                "success": False, "secid": secid, "error": "接口未返回该标的"
            })
        return result

    # ---------- 历史K线 ----------

    def get_daily_kline(self, secid: str, limit: int = 60) -> Dict:
        """日K线（前复权）

        Returns:
            {'success': True, 'bars': [{'date','open','close','high','low',
                                        'volume','amount'}, ...]}
            bars 按时间正序，最后一根是今天（开盘时为当前动态值）
        """
        try:
            resp = self.session.get(
                self.KLINE_URL,
                params={
                    "secid": secid,
                    "fields1": "f1,f2,f3",
                    "fields2": _KLINE_FIELDS2,
                    "klt": "101",          # 日K
                    "fqt": "1",            # 前复权
                    "end": "20500101",
                    "lmt": str(limit),
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data")
            if not data or not data.get("klines"):
                return {"success": False, "secid": secid, "error": "无K线数据"}

            bars = []
            for line in data["klines"]:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                # f51~f57: 日期,开,收,高,低,成交量(手),成交额(元)
                bars.append({
                    "date": parts[0],
                    "open": self._num(parts[1]),
                    "close": self._num(parts[2]),
                    "high": self._num(parts[3]),
                    "low": self._num(parts[4]),
                    "volume": self._num(parts[5]),
                    "amount": self._num(parts[6]),
                })

            return {
                "success": True,
                "secid": secid,
                "code": data.get("code", ""),
                "name": data.get("name", ""),
                "bars": bars,
            }
        except Exception as e:
            return {"success": False, "secid": secid, "error": str(e)}
