# Aether 论文调研 — 可行策略方向

## 已验证有效的方向

### 1. Dynamic Grid Trading (DGT) — arXiv:2506.11921
- **思路**: 不预测方向，在价格区间内动态调整网格，低买高卖吃波动
- **结果**: BTC/ETH 分钟级回测正收益
- **难度**: ⭐⭐ (易实现)
- **互补性**: 与 TrendFollow 互补（趋势+震荡都能赚钱）

### 2. Anti-Overfitting DRL Framework — arXiv:2209.05559
- **思路**: 用 Deflated Sharpe Ratio 检验策略是否真的有效，防过拟合
- **结果**: DRL agent + 统计显著性检验
- **难度**: ⭐⭐⭐⭐

### 3. XGBoost/RF Trend Classification — arXiv:2105.06827
- **思路**: kNN/XGBoost/RF 分类趋势（跟我们试过的 LightGBM 同类）
- **结果**: 3个币种盈利
- **难度**: ⭐⭐⭐
- **注意**: 我们的 LightGBM 过拟合严重，需参考2209.05559的防过拟合框架

### 4. SVM Crypto Prediction — arXiv:1911.11819
- **思路**: SVM + 大量技术指标预测短期涨跌
- **结果**: PPV/NPV 指标表现好
- **难度**: ⭐⭐

### 5. Order Flow Entropy → Predict Magnitude — arXiv:2512.15720
- **核心洞察**: 方向不可预测，但波动幅度可以
- **启示**: 把 ML 目标从"预测涨跌"改为"预测波动率"

### 6. Transformer Quantformer — arXiv:2404.00424
- **思路**: 预训练 Transformer 迁移到量化交易
- **难度**: ⭐⭐⭐⭐⭐

## 决策原则
- 以盈利为唯一目标
- 优先选择实现快、验证容易的方向
- 可以与现有 TrendFollow 互补
- 必须通过统计显著性检验（不能只看回测收益率）
