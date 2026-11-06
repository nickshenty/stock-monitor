# 自选股盯盘提醒

终端里跑的自选股监控：轮询东方财富行情 → 跑规则 → 命中就推 macOS 通知，可选接 DeepSeek 做提醒解读和收盘复盘。

不交易、不下单、不预测，只做「价格到了告诉我一声」这件事。

## 快速开始

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 加自选（代码或名称都能识别）
./venv/bin/python main.py --add 600519 --cost 1400 --stop-loss -5
./venv/bin/python main.py --add 510300
./venv/bin/python main.py --list

# 先看一眼当前行情
./venv/bin/python main.py --check

# 持续盯盘
./venv/bin/python main.py --watch
```

AI 功能需要 DeepSeek Key，不设也能用（纯规则提醒）：

```bash
export DEEPSEEK_API_KEY=sk-xxx
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `--add 标的` | 加入自选，支持代码/名称，可带 `--cost` `--shares` `--stop-loss` `--take-profit` `--price-above` `--price-below` |
| `--remove 标的` | 移出自选 |
| `--list` | 查看自选和各自生效的规则 |
| `--check` | 查一次行情就退出（适合配合 cron） |
| `--watch` | 持续盯盘，`Ctrl-C` 优雅退出并保存状态 |
| `--review` | 生成当日复盘（有 AI 时含日评） |

通用参数：`--no-ai` 本次不调 AI、`--interval 秒` 覆盖轮询间隔、`--config-dir 目录` 指定配置目录。

## 监控的规则

每条规则可按标的单独覆盖，未配置的继承 `default_rules`。

| 规则键 | 默认值 | 含义 |
| --- | --- | --- |
| `change_pct_up` | `3.0` | 涨幅达到该值提醒 |
| `change_pct_down` | `-3.0` | 跌幅达到该值提醒 |
| `volume_ratio` | `2.0` | 量比达到该值提醒（放量） |
| `ma_cross` | `["5_20"]` | 均线金叉/死叉，每对均线每天只报一次 |
| `drawdown_pct` | `-8.0` | 距 20 日高点回撤超过该值提醒 |
| `stop_loss_pct` | 需配 `cost` | 相对成本的止损线，`critical` 级 |
| `take_profit_pct` | 需配 `cost` | 相对成本的止盈线 |
| `price_above` / `price_below` | 需显式设置 | 绝对价格上破/下破 |

**冷却**：同一标的同一规则，价格变化不足 `alert_step`（默认 2%）不重复提醒；均线交叉类每天只报一次。避免横盘震荡时被同一条消息刷屏。

## 配置

配置在 `--config-dir` 指定的目录，默认是项目下的 `config/`：

- `watchlist.json` — 自选与规则
- `alert_state.json` — 冷却状态与当日提醒记录（程序自动维护，别手改）

`watchlist.json` 结构：

```json
{
  "poll_interval": 60,
  "notify": { "macos": true, "terminal": true, "sound": "Ping" },
  "ai": { "enabled": true, "model": "deepseek-chat",
          "explain_alerts": true, "daily_review": true },
  "network": { "use_proxy": false },
  "default_rules": {
    "change_pct_up": 3.0, "change_pct_down": -3.0, "volume_ratio": 2.0,
    "ma_cross": ["5_20"], "drawdown_pct": -8.0
  },
  "watchlist": [
    { "secid": "1.600519", "code": "600519", "name": "贵州茅台",
      "cost": 1400, "shares": 100,
      "rules": { "stop_loss_pct": -5, "take_profit_pct": 15 } }
  ]
}
```

### network.use_proxy

行情请求走代理还是直连，**默认自动**（`null`），一般不用改。

为什么会需要这个开关：同一台机器上「走系统代理」和「直连」哪个能通并不固定。有的网络直连东财完全正常；有的网络（运营商限制、或开了 Clash 之类的代理）直连会被重置，必须经代理才通。我实测过两种都真实存在，硬编码任何一种都会在另一类环境下直接失效。

所以自动模式会两条路都试：先按系统代理设置走，不通就换直连；哪条通了就把哪条记为主路径，后续请求直接用，不会反复试错。

需要固定行为时：

- `true` — 只走系统代理
- `false` — 只直连，完全不读系统代理设置（`requests` 的 `trust_env`）
- `null` — 自动（默认）

## 取数失败会明确告诉你

盯盘工具最危险的失败模式不是报错，而是「看起来在跑，其实没在看」。所以取数失败不会被静默吞掉：

- `--check` 全部标的取数失败时，除了报错还会以退出码 `1` 结束，不会只显示一句「无规则触发」让你以为一切正常。部分失败会标出多少个标的没参与规则判断。
- `--watch` 每轮都会显式打印失败标的和原因；连续 3 轮全部失败会推一条系统通知（此时你大概不在看终端），恢复后再推一条「已恢复」。
- 网络抖动和东财偶发的 5xx 有短重试（0.5s、1s 退避），不会一次抖动就废掉整轮。

## 说明

- 行情来自东方财富公开接口，非官方 API，字段和可用性可能变化；数据仅供自用参考，不构成投资建议。
- 非交易时段 `--watch` 自动降频等待，开盘后恢复正常轮询。
- 停牌标的不会产生提醒（价格无效，盈亏无从计算）。

## 文件结构

```
main.py            CLI 入口、子命令、盯盘循环
datasource.py      东财行情：代码解析、实时报价、日K线
indicators.py      量比、均线、回撤等指标计算
rules.py           规则判定与冷却
notifier.py        终端看板与 macOS 通知
analyst.py         DeepSeek 提醒解读 / 收盘复盘
config_manager.py  配置与状态读写
```
