# 币安 U本位合约量化交易系统 — 使用文档

## 快速开始

### 1. 环境准备

```bash
cd /home/rinnen/binance_quant
source venv/bin/activate
```

### 2. 三种运行模式

| 模式 | 命令 | 说明 |
|------|------|------|
| **模拟盘** | `python main.py --mode paper` | 测试网行情+模拟下单,不消耗真实资金 |
| **回测** | `python main.py --mode backtest` | 历史数据回测,输出收益率/夏普等指标 |
| **实盘** | `python main.py --mode live` | ⚠️ 真实资金交易,需输入 LIVE 确认 |

### 3. 常用参数

```bash
# 指定交易对和周期
python main.py --mode paper --symbols BTC/USDT,ETH/USDT --timeframe 15m

# 指定检查间隔(秒)
python main.py --mode paper --interval 30

# 回测 180 天历史数据
python main.py --mode backtest --lookback-days 180
```

### 4. 验证测试网连通性

```bash
python -c "
from config.settings import get_config
from execution.client import BinanceFuturesClient
c = get_config()
client = BinanceFuturesClient(c.api_key, c.api_secret, c.testnet)
print('余额:', client.get_balance())
print('行情:', client.get_ticker('BTC/USDT')['last'])
print('持仓:', len(client.get_positions()))
"
```

## 系统架构

```
data/collector.py      → K线数据采集 (ccxt binanceusdm)
data/storage.py        → SQLite 数据存储
execution/client.py    → 币安合约 REST API (ccxt + REST回退)
execution/engine.py    → 订单执行引擎 (重试+精度处理)
risk/manager.py        → 风控管理 (仓位/杠杆/日亏损限制)
strategy/base.py       → 策略基类 (Signal/SignalType/BaseStrategy)
strategy/manager.py    → 策略管理器 (多策略并行)
strategy/examples/     → 示例策略
  ma_cross.py          → 双均线+ATR动态止损
  rsi_mean_reversion.py → RSI均值回归
backtest/engine.py     → 回测引擎 (夏普/最大回撤/胜率)
main.py                → 主程序入口
```

## 已注册策略

### 1. 双均线交叉 (MA_Cross)
- **规则**: 快线上穿慢线→做多, 下穿→做空, 反向交叉→平仓
- **参数**: fast_period=7, slow_period=25, atr_period=14
- **止损**: ATR × 2 动态止损
- **止盈**: ATR × 3 动态止盈

### 2. RSI 均值回归 (RSI_MR)
- **规则**: RSI<30→做多, RSI>70→做空, RSI回50→平仓
- **参数**: rsi_period=14, oversold=30, overbought=70
- **止损**: 固定 3%
- **止盈**: 固定 6%

## 风控参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| max_positions | 3 | 最大同时持仓数 |
| max_leverage | 10 | 最大杠杆倍数 |
| max_per_symbol_pct | 15% | 单币种最大仓位 |
| max_total_position_pct | 40% | 总仓位上限 |
| daily_loss_limit_pct | 5% | 日亏损熔断 |

## 输出文件

| 文件 | 说明 |
|------|------|
| `trading.log` | 运行日志 |
| `data/market.db` | SQLite 行情数据库 |
| `backtest/results/` | 回测结果图表 |

## 错误排查

| 症状 | 解决 |
|------|------|
| "Network is unreachable" | 检查网络/VPN,确保能访问 testnet.binancefuture.com |
| "API-key format invalid" | 检查 .env 中的密钥是否完整 |
| ccxt 报错 | 系统已内置 REST 回退,刷新重试 |
| 策略无信号 | 正常现象(RSI在40-60区间),切换更短周期试试 |

## 添加自定义策略

1. 在 `strategy/examples/` 创建新文件
2. 继承 `BaseStrategy`,实现 `generate_signal()`
3. 在 `main.py` 中注册

```python
# 示例: 自定义策略骨架
from strategy.base import BaseStrategy, Signal, SignalType

class MyStrategy(BaseStrategy):
    def generate_signal(self, symbol: str) -> Signal:
        df = self.get_data(symbol)
        # ... 你的逻辑 ...
        return Signal(SignalType.HOLD, symbol)
```
