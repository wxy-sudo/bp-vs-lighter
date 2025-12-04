# Backpack ↔ Lighter Arbitrage

该目录提供一个最小化的 Backpack 做市 + Lighter 对冲套利机器人。逻辑与主仓库中的 edgeX ↔ Lighter 版本类似：当检测到

- `lighter_bid - backpack_bid > long_threshold` 时，在 Backpack 放置 post-only 买单并在 Lighter 立刻卖出对冲；
- `backpack_ask - lighter_ask > short_threshold` 时，在 Backpack 放置 post-only 卖单并在 Lighter 买入对冲。

## 1. 安装依赖

建议在独立虚拟环境中安装：

```bash
cd bp-lighter
pip install -r requirements.txt
```

> 说明：`requirements.txt` 只列出 Python 包依赖；Lighter SDK 和 Backpack SDK 分别来自 `lighter-python-main/` 与 `bpx-py`。如果尚未在全局环境安装 Lighter SDK，可在仓库根目录执行 `pip install -e ./lighter-python-main`。

## 2. 配置环境变量

复制示例文件并填写实际值：

```bash
cp env_example.txt .env
```

必填字段：

- `BACKPACK_PUBLIC_KEY` / `BACKPACK_SECRET_KEY`
- `API_KEY_PRIVATE_KEY` / `LIGHTER_ACCOUNT_INDEX` / `LIGHTER_API_KEY_INDEX`

可选字段 `ACCOUNT_NAME`、`TIMEZONE` 仅用于日志文件命名与时间显示。

## 3. 运行示例

```bash
python arbitrage.py \
  --ticker BTC \
  --size 0.002 \
  --max-position 0.04 \
  --long-threshold 8 \
  --short-threshold 8

# 开启滑动窗口（动态阈值）示例
python3 arbitrage2.py --ticker BTC --size 0.002 --max-position 0.02 --spread-window 500 --spread-entry-z 0.7
```

参数说明：

- `--size`：每次在 Backpack 放置的 maker 单数量；
- `--max-position`：Backpack 侧允许的最大绝对持仓，未提供时默认等于 `size`；
- `--long-threshold`：当 `lighter_bid - backpack_bid` 大于该值（单位：合约报价币种）即触发在 Backpack 买入、Lighter 卖出的组合；
- `--short-threshold`：当 `backpack_ask - lighter_ask` 超过该值即触发在 Backpack 卖出、Lighter 买入的组合。
- `--use-dynamic-thresholds`：改为使用 rolling window 统计的 z-score 触发；可搭配 `--spread-window`（样本数量）、`--spread-entry-z`（触发 z 值）、`--spread-exit-z`（解除“保险”的 z 值）微调灵敏度。

机器人会持续监控两个交易所的订单簿，通过 Backpack 官方 WebSocket/HTTP 获取 BBO，并在订单成交后自动在 Lighter 侧市价对冲。所有交易与 BBO 更新会输出到 `logs/` 目录。

