# A 股量化交易研究平台（Web 版）

一个开箱即用的 A 股量化研究/模拟交易 Web 平台：实时行情、策略信号、全市场回测、
个股最优策略自动轮动、多模拟账户撮合，纯 Python 标准库实现，前端无框架。

> 仅用于量化学习与研究，不构成任何投资建议；模拟盘收益不代表实盘收益。

## 功能

- 实时行情：腾讯/新浪/东方财富多源行情（行情页、自选监控）；
- 策略引擎：30+ 策略（超跌反弹、RSI、均线、MACD、动量、突破、低位修复等），
  统一口径回测（收盘信号 → 次日开盘成交，T+1，含手续费/印花税）；
- 个股最优策略：对每只股票在多个周期上回测全部策略，挑选"它自己最近胜率最高"的
  策略用于信号监控与模拟盘（不做一套策略套所有股票）；
- 模拟盘：主账户 / 小账户 / 超跌精选等多账户独立撮合，移动止损、卖出冷却、
  T+1 保护、动态换仓，逐笔记录成交与胜率；
- 信号监控：买入/卖出信号分离、按近期胜率排序、历史信号回溯标注；
- 数据层：本地日线 CSV + 腾讯/新浪实时行情；可选 tushare 全市场日线自动同步。

## 快速开始

```bash
# 1. 准备数据目录（回测/信号使用本地日线 CSV：date,open,high,low,close,volume,pct,amount,turnover,chg）
mkdir -p backtest/data
#    可选：配置 tushare token 后自动拉取全市场日线（见 requirements.txt）

# 2. 启动
python3 server.py
# 浏览器打开 http://127.0.0.1:8765
```

平台会自动创建模拟账户状态文件（已 gitignore）。行情/回测不需要任何 API Key；
若要全市场日线自动同步，自行配置 tushare token（见 requirements.txt）。

## 主要模块

| 文件 | 职责 |
|---|---|
| `server.py` | Web 服务、API 路由、自动撮合/每日更新线程 |
| `market.py` | 多源实时行情、板块/资金/筹码、K线与日线刷新 |
| `strategies.py` | 策略定义与信号构造 |
| `signals.py` | 单标的信号评估（实时源优先、本地数据兜底） |
| `engine.py` | 统一回测引擎（T+1、手续费、移动止损） |
| `paper.py` | 模拟盘撮合：买入择优、卖出确认、止损、换仓、权益曲线 |
| `research.py` | 投研评级接入（可选，数据文件不入库） |
| `tushare_provider.py` | 可选：tushare 全市场日线同步 |
| `static/` | 无框架前端页面 |

## API 一览（节选）

```text
GET  /api/quotes?codes=000001,600519   实时行情
GET  /api/kline?code=000001&days=250   K线 + 指标
GET  /api/strategies                   策略列表
POST /api/backtest                     单标的回测
POST /api/backtest_all                 全市场组合回测
GET  /api/signals                      个股最优信号（按近1月胜率排序）
GET  /api/paper?account=main           模拟盘状态
POST /api/paper/tick                   触发一次撮合
POST /api/paper/start                  新建/重置模拟账户
```

## 目录约定

```text
quant_ui/
  server.py / market.py / ...     平台代码
  static/                         前端页面
  backtest/data/*.csv             本地日线数据（不入库，自行同步）
  paper_state_*.json              模拟盘状态（不入库）
  tushare_config.json             tushare token（不入库）
```
