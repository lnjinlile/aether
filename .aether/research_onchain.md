# 链上数据 Alpha 策略研究

## 核心论文

### [2602.08429] On- and off-chain demand and supply drivers of Bitcoin price (2026)
- **发现**: 链下数据(交易所流量)对价格影响 > 链上数据
- **关键特征**: 交易所净流量、活跃地址、交易量
- **方法**: 时间序列分析，供需模型

### [1812.09452] Bitcoin Price: GARCH Evidence from High Frequency Data (2018)
- **方法**: GARCH 波动率建模 + 供需因子
- **结论**: 交易需求是价格的主要驱动力

## 我们已有的链上/衍生品数据
| 数据 | 来源 | 状态 |
|------|------|------|
| 持仓量 (OI) | Binance fapi | ✅ 每5分钟 |
| 资金费率 | Binance fapi | ✅ 每5分钟 |
| 多空比 | Binance fapi | ⏳ 待采集 |
| 订单簿 | Binance fapi | ✅ 每5分钟 |
| 主动买卖量 | Binance fapi | ⏳ 待采集 |

## 建议新增特征(用于 ML 策略)
基于论文结论和现有数据，以下特征可显著提升预测能力:

1. **OI 变化率** (1h/4h/24h) — 持仓量剧烈变化预示方向
2. **资金费率极端值** — 极端负费率 → 做多信号
3. **订单簿不平衡度** — bid_vol/(bid_vol+ask_vol) 偏离0.5
4. **多空比反转** — 极端看多 → 反向信号
5. **OI + 价格背离** — 价格涨但OI跌 → 趋势衰竭
6. **主动买卖量比** — taker buy/sell ratio
