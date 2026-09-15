# AGENTS.md — 给 AI 助手 / 二次开发者的项目说明

本项目是一个 A 股量化研究 + 模拟交易 Web 平台。任何 AI 助手（WorkBuddy、Codex 等）
在修改本项目前，请先读完本文件，遵守其中的接口约定与安全红线。

## 1. 运行方式

```bash
python3 server.py          # 默认监听 http://127.0.0.1:8765
```

- 运行环境：Python 3.9+，核心功能仅用标准库；无框架前端在 `static/`；
- 回测/信号依赖 `backtest/data/*.csv`（本地日线，仓库不含数据，需自行准备）；
- 可选：配置 tushare token 自动同步全市场日线（见 `tushare_config.example.json`）。

## 2. 数据约定（重要）

`backtest/data/{code}_{name}.csv`，表头：

```text
date,open,high,low,close,volume,pct,amount,turnover,chg[,adj_factor]
```

- **价格口径：前复权**（与东方财富/同花顺等股票软件默认一致）；
  实时 K 线走 `market.get_kline()`（东财 `fqt=1`，腾讯 `qfq` 兜底）；
- 若 CSV 带 `adj_factor` 列，`engine.load_data()` 会按
  `前复权价 = 原始价 × 该日因子 ÷ 最新因子` 自动换算；
- 成交量为股数，成交额为元，pct/chg 为百分比与涨跌额。

## 3. 模块地图

| 模块 | 职责 | 改动注意 |
|---|---|---|
| `server.py` | HTTP 服务/路由、自动撮合线程、每日更新 | 新增接口请保持现有字段兼容 |
| `market.py` | 实时行情、K线、板块/资金/筹码 | 多源失败要有兜底，不要写死单一源 |
| `strategies.py` | 策略元数据 + 信号构造 | 新增策略见第 5 节 |
| `signals.py` | 单标的信号评估 | 统一"收盘信号 → 次日开盘成交"口径 |
| `engine.py` | 回测引擎（T+1、手续费、止损） | 改动会影响全部回测结果 |
| `paper.py` | 模拟盘撮合、换仓、权益曲线 | 不要破坏 state 文件字段兼容性 |
| `research.py` | 投研评级接入（可选） | 文件不存在时应静默降级 |
| `static/` | 前端页面 | 纯 JS，无构建步骤 |

## 4. 安全红线（必须遵守）

1. 禁止把以下内容提交到 git：
   `paper_state*.json`、`backup_paper_state*.json`、`tushare_config.json`、
   `.tqsdk_auth.json`、`*.log`、任何数据缓存目录；
2. 禁止在代码中写死 token / 账号 / 密码；一律走配置文件或环境变量：
   `TUSHARE_TOKEN`、`TQSDK_USER`、`TQSDK_PASS`；
3. 修改用户运行中的数据或账户状态前，必须先提醒使用者备份；
4. 不要删除或重命名现有 API（`/api/quotes`、`/api/kline`、`/api/backtest`、
   `/api/signals`、`/api/paper*` 等），如需变更请新增字段而不是改语义。

## 5. 常见任务

### 新增一个策略

1. 在 `strategies.py` 的 `strategy_meta()` 中注册：`{"label": 中文名, "params": {参数: {default: 值, ...}}}`；
2. 在 `strategies.build_signal(name, rows, params, ...)` 中实现信号函数：
   返回目标仓位 `1`（持有多头）/`0`（空仓）；信号在**当日收盘**判定，引擎次日开盘成交；
3. 自测：`python3 - <<'EOF'` 用 `engine.load_data("000001")` + `engine.run_backtest` 跑一笔，确认有交易、无异常；
4. 前端策略下拉框会自动读取 `/api/strategies`，无需改页面。

### 修改模拟盘参数

模拟盘由 `paper.py` 管理：`start()` 建账户、`tick()` 撮合。参数含义见
`paper._default_state()` 注释（换仓门槛 `rotation_gap`、冷却 `sell_cooldown_days`、
移动止损 `stop_loss`、最短持有 `min_hold_days` 等）。改动后请用
`POST /api/paper/tick` 触发一次撮合验证。

### 接入新的行情源

在 `market.py` 增加取数函数，并在现有函数中以"主源失败 → 备用源"方式接入，
保持返回字段与现有结构一致（code/name/price/pct/open/high/low/volume/amount 等）。

## 6. 提交前自检

```bash
python3 -m py_compile server.py market.py paper.py strategies.py signals.py engine.py
python3 - <<'EOF'
import engine
rows = engine.load_data("000001")      # 若本地无数据，可跳过此步
print(len(rows), rows[-1])
EOF
```

- 跑一遍 `GET /api/health`、`GET /api/strategies`、`POST /api/backtest` 冒烟测试；
- 确认没有把密钥、账户状态、数据缓存带进提交（`git status`）。

## 7. 已知限制

- 本地数据为前复权；主力/停牌/退市样本可能缺失，回测存在幸存者偏差；
- 免费行情源（新浪/腾讯/东财）有频率限制，失败需走备用源；
- 非交易日不要跑增量更新；周末产生的"假 K 线"需用交易日历过滤；
- 模拟盘为纸面撮合，不含滑点冲击与涨跌停排队细节，不代表实盘收益。
