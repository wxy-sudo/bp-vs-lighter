#!/usr/bin/env python3
"""
价差实时绘图脚本
用于监控 Backpack 和 Lighter 之间的价差
"""

import asyncio
import json
import time
import os
import sys
from datetime import datetime
from decimal import Decimal
from collections import deque
from pathlib import Path

import requests
import websockets
from dotenv import load_dotenv

# 尝试导入绘图库
try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.animation import FuncAnimation
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("⚠️ matplotlib 未安装，将只输出文本数据")
    print("   安装命令: pip install matplotlib")

# 加载环境变量
load_dotenv()

# 添加项目路径
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class SpreadMonitor:
    """监控两个交易所的价差"""
    
    def __init__(self, ticker: str = "BTC", max_points: int = 500):
        self.ticker = ticker
        self.max_points = max_points
        
        # 数据存储
        self.timestamps = deque(maxlen=max_points)
        self.long_spreads = deque(maxlen=max_points)  # Lighter bid - BP bid
        self.short_spreads = deque(maxlen=max_points)  # BP ask - Lighter ask
        self.bp_bids = deque(maxlen=max_points)
        self.bp_asks = deque(maxlen=max_points)
        self.lt_bids = deque(maxlen=max_points)
        self.lt_asks = deque(maxlen=max_points)
        
        # Backpack 数据
        self.backpack_contract_id = f"{ticker}_USDC_PERP"
        self.backpack_best_bid = None
        self.backpack_best_ask = None
        
        # Lighter 数据
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.lighter_market_index = None
        self.lighter_best_bid = None
        self.lighter_best_ask = None
        self.lighter_order_book = {"bids": {}, "asks": {}}
        
        # 控制标志
        self.stop_flag = False
        self.data_ready = False
        
        # CSV 文件
        os.makedirs("logs", exist_ok=True)
        self.csv_filename = f"logs/spread_data_{ticker}.csv"
        self._init_csv()
    
    def _init_csv(self):
        """初始化 CSV 文件"""
        if not os.path.exists(self.csv_filename):
            with open(self.csv_filename, 'w') as f:
                f.write("timestamp,bp_bid,bp_ask,lt_bid,lt_ask,long_spread,short_spread\n")
    
    def _log_to_csv(self, bp_bid, bp_ask, lt_bid, lt_ask, long_spread, short_spread):
        """记录数据到 CSV"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(self.csv_filename, 'a') as f:
            f.write(f"{timestamp},{bp_bid},{bp_ask},{lt_bid},{lt_ask},{long_spread},{short_spread}\n")
    
    def get_lighter_market_index(self):
        """获取 Lighter 市场索引"""
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            for market in data.get("order_books", []):
                if market["symbol"] == self.ticker:
                    self.lighter_market_index = market["market_id"]
                    print(f"✅ Lighter market index: {self.lighter_market_index}")
                    return True
        except Exception as e:
            print(f"❌ 获取 Lighter 市场信息失败: {e}")
        return False
    
    async def fetch_backpack_prices(self):
        """从 Backpack REST API 获取价格"""
        try:
            url = f"https://api.backpack.exchange/api/v1/depth?symbol={self.backpack_contract_id}"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                bids = data.get('bids', [])
                asks = data.get('asks', [])
                
                # bids 需要按价格降序排列，取最高价（best bid）
                if bids:
                    sorted_bids = sorted(bids, key=lambda x: Decimal(x[0]), reverse=True)
                    self.backpack_best_bid = Decimal(sorted_bids[0][0])
                
                # asks 需要按价格升序排列，取最低价（best ask）
                if asks:
                    sorted_asks = sorted(asks, key=lambda x: Decimal(x[0]))
                    self.backpack_best_ask = Decimal(sorted_asks[0][0])
        except Exception as e:
            print(f"⚠️ Backpack REST 错误: {e}")
    
    async def handle_lighter_ws(self):
        """处理 Lighter WebSocket"""
        url = "wss://mainnet.zklighter.elliot.ai/stream"
        
        while not self.stop_flag:
            try:
                async with websockets.connect(url) as ws:
                    # 订阅订单簿
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"order_book/{self.lighter_market_index}"
                    }))
                    print(f"✅ 已订阅 Lighter 订单簿")
                    
                    while not self.stop_flag:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                            data = json.loads(msg)
                            
                            msg_type = data.get("type", "")
                            
                            if msg_type == "subscribed/order_book":
                                order_book = data.get("order_book", {})
                                self._update_lighter_book(order_book)
                                if self.lighter_best_bid and self.lighter_best_ask:
                                    print(f"📊 Lighter 订单簿已加载 - Bid: {self.lighter_best_bid}, Ask: {self.lighter_best_ask}")
                            elif msg_type == "update/order_book":
                                order_book = data.get("order_book", {})
                                self._update_lighter_book(order_book, incremental=True)
                            elif msg_type == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                            # 忽略其他消息类型
                                
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            print(f"⚠️ Lighter WS 消息处理错误: {e}")
                            import traceback
                            traceback.print_exc()
                            break
                            
            except Exception as e:
                print(f"⚠️ Lighter WS 连接错误: {e}")
                if not self.stop_flag:
                    await asyncio.sleep(2)
    
    def _update_lighter_book(self, order_book, incremental=False):
        """更新 Lighter 订单簿"""
        if not incremental:
            self.lighter_order_book = {"bids": {}, "asks": {}}
        
        for bid in order_book.get("bids", []):
            try:
                # Lighter 格式可能是 {"price": "xxx", "size": "xxx"} 或 [price, size]
                if isinstance(bid, dict):
                    price = Decimal(str(bid.get("price", 0)))
                    size = Decimal(str(bid.get("size", 0)))
                elif isinstance(bid, list) and len(bid) >= 2:
                    price = Decimal(str(bid[0]))
                    size = Decimal(str(bid[1]))
                else:
                    continue
                    
                if size > 0:
                    self.lighter_order_book["bids"][price] = size
                else:
                    self.lighter_order_book["bids"].pop(price, None)
            except Exception:
                continue
        
        for ask in order_book.get("asks", []):
            try:
                if isinstance(ask, dict):
                    price = Decimal(str(ask.get("price", 0)))
                    size = Decimal(str(ask.get("size", 0)))
                elif isinstance(ask, list) and len(ask) >= 2:
                    price = Decimal(str(ask[0]))
                    size = Decimal(str(ask[1]))
                else:
                    continue
                    
                if size > 0:
                    self.lighter_order_book["asks"][price] = size
                else:
                    self.lighter_order_book["asks"].pop(price, None)
            except Exception:
                continue
        
        if self.lighter_order_book["bids"]:
            self.lighter_best_bid = max(self.lighter_order_book["bids"].keys())
        if self.lighter_order_book["asks"]:
            self.lighter_best_ask = min(self.lighter_order_book["asks"].keys())
    
    async def collect_data(self):
        """收集价差数据"""
        print(f"\n📊 开始监控 {self.ticker} 价差...")
        print(f"📁 数据保存到: {self.csv_filename}")
        print("-" * 60)
        
        last_print_time = 0
        
        while not self.stop_flag:
            # 获取 Backpack 价格
            await self.fetch_backpack_prices()
            
            # 计算价差
            if (self.backpack_best_bid and self.backpack_best_ask and 
                self.lighter_best_bid and self.lighter_best_ask):
                
                now = datetime.now()
                long_spread = float(self.lighter_best_bid - self.backpack_best_bid)
                short_spread = float(self.backpack_best_ask - self.lighter_best_ask)
                
                # 存储数据
                self.timestamps.append(now)
                self.long_spreads.append(long_spread)
                self.short_spreads.append(short_spread)
                self.bp_bids.append(float(self.backpack_best_bid))
                self.bp_asks.append(float(self.backpack_best_ask))
                self.lt_bids.append(float(self.lighter_best_bid))
                self.lt_asks.append(float(self.lighter_best_ask))
                
                # 记录到 CSV
                self._log_to_csv(
                    self.backpack_best_bid, self.backpack_best_ask,
                    self.lighter_best_bid, self.lighter_best_ask,
                    long_spread, short_spread
                )
                
                self.data_ready = True
                
                # 每2秒打印一次
                current_time = time.time()
                if current_time - last_print_time >= 2:
                    print(f"[{now.strftime('%H:%M:%S')}] "
                          f"BP: {self.backpack_best_bid}/{self.backpack_best_ask} | "
                          f"LT: {self.lighter_best_bid}/{self.lighter_best_ask} | "
                          f"Long: {long_spread:+.2f} | Short: {short_spread:+.2f}")
                    last_print_time = current_time
            
            await asyncio.sleep(0.1)
    
    async def run_text_mode(self):
        """文本模式运行（无图形界面）"""
        if not self.get_lighter_market_index():
            return
        
        # 启动 Lighter WebSocket
        lighter_task = asyncio.create_task(self.handle_lighter_ws())
        
        try:
            await self.collect_data()
        except KeyboardInterrupt:
            print("\n🛑 停止监控...")
        finally:
            self.stop_flag = True
            lighter_task.cancel()
    
    def run_plot_mode(self):
        """绘图模式运行"""
        if not self.get_lighter_market_index():
            return
        
        # 创建图表
        fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
        fig.suptitle(f'{self.ticker} Backpack vs Lighter 价差监控', fontsize=14, fontweight='bold')
        
        # 价差图
        ax1 = axes[0]
        ax1.set_ylabel('价差 (USDC)', fontsize=10)
        ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
        ax1.grid(True, alpha=0.3)
        ax1.legend(['Long Spread (LT bid - BP bid)', 'Short Spread (BP ask - LT ask)'], loc='upper left')
        
        line_long, = ax1.plot([], [], 'g-', label='Long Spread', linewidth=1.5)
        line_short, = ax1.plot([], [], 'r-', label='Short Spread', linewidth=1.5)
        
        # 价格图
        ax2 = axes[1]
        ax2.set_ylabel('价格 (USDC)', fontsize=10)
        ax2.set_xlabel('时间', fontsize=10)
        ax2.grid(True, alpha=0.3)
        
        line_bp_bid, = ax2.plot([], [], 'b-', label='BP Bid', linewidth=1, alpha=0.7)
        line_bp_ask, = ax2.plot([], [], 'b--', label='BP Ask', linewidth=1, alpha=0.7)
        line_lt_bid, = ax2.plot([], [], 'orange', label='LT Bid', linewidth=1, alpha=0.7)
        line_lt_ask, = ax2.plot([], [], 'orange', linestyle='--', label='LT Ask', linewidth=1, alpha=0.7)
        ax2.legend(loc='upper left')
        
        # 格式化 x 轴
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        
        def init():
            return line_long, line_short, line_bp_bid, line_bp_ask, line_lt_bid, line_lt_ask
        
        def update(frame):
            if len(self.timestamps) > 0:
                times = list(self.timestamps)
                
                # 更新价差图
                line_long.set_data(times, list(self.long_spreads))
                line_short.set_data(times, list(self.short_spreads))
                
                # 更新价格图
                line_bp_bid.set_data(times, list(self.bp_bids))
                line_bp_ask.set_data(times, list(self.bp_asks))
                line_lt_bid.set_data(times, list(self.lt_bids))
                line_lt_ask.set_data(times, list(self.lt_asks))
                
                # 调整坐标轴
                for ax in axes:
                    ax.relim()
                    ax.autoscale_view()
                
                # 旋转 x 轴标签
                plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')
            
            return line_long, line_short, line_bp_bid, line_bp_ask, line_lt_bid, line_lt_ask
        
        # 在后台线程运行数据收集
        import threading
        
        def run_async_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 启动 Lighter WebSocket
            lighter_task = loop.create_task(self.handle_lighter_ws())
            collect_task = loop.create_task(self.collect_data())
            
            try:
                loop.run_until_complete(asyncio.gather(lighter_task, collect_task))
            except:
                pass
            finally:
                loop.close()
        
        data_thread = threading.Thread(target=run_async_loop, daemon=True)
        data_thread.start()
        
        # 等待数据就绪
        print("⏳ 等待数据...")
        time.sleep(3)
        
        # 启动动画
        ani = FuncAnimation(fig, update, init_func=init, interval=500, blit=False, cache_frame_data=False)
        
        plt.tight_layout()
        plt.show()
        
        self.stop_flag = True


def plot_from_csv(csv_file: str):
    """从 CSV 文件绘制历史数据"""
    if not HAS_MATPLOTLIB:
        print("❌ 需要安装 matplotlib: pip install matplotlib")
        return
    
    import pandas as pd
    
    if not os.path.exists(csv_file):
        print(f"❌ 文件不存在: {csv_file}")
        return
    
    # 读取数据
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    print(f"📊 加载了 {len(df)} 条数据记录")
    print(f"   时间范围: {df['timestamp'].min()} ~ {df['timestamp'].max()}")
    print(f"   Long Spread: 均值={df['long_spread'].mean():.2f}, 标准差={df['long_spread'].std():.2f}")
    print(f"   Short Spread: 均值={df['short_spread'].mean():.2f}, 标准差={df['short_spread'].std():.2f}")
    
    # 创建图表
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle('Backpack vs Lighter 价差历史分析', fontsize=14, fontweight='bold')
    
    # 价差图
    ax1 = axes[0]
    ax1.plot(df['timestamp'], df['long_spread'], 'g-', label='Long Spread', linewidth=0.8, alpha=0.8)
    ax1.plot(df['timestamp'], df['short_spread'], 'r-', label='Short Spread', linewidth=0.8, alpha=0.8)
    ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax1.axhline(y=df['long_spread'].mean(), color='green', linestyle=':', alpha=0.5, label=f'Long Mean ({df["long_spread"].mean():.1f})')
    ax1.axhline(y=df['short_spread'].mean(), color='red', linestyle=':', alpha=0.5, label=f'Short Mean ({df["short_spread"].mean():.1f})')
    ax1.set_ylabel('价差 (USDC)')
    ax1.legend(loc='upper right')
    ax1.grid(True, alpha=0.3)
    
    # 价差分布直方图
    ax2 = axes[1]
    ax2.hist(df['long_spread'], bins=50, alpha=0.5, color='green', label='Long Spread')
    ax2.hist(df['short_spread'], bins=50, alpha=0.5, color='red', label='Short Spread')
    ax2.axvline(x=0, color='gray', linestyle='--')
    ax2.set_ylabel('频次')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    
    # 价格图
    ax3 = axes[2]
    ax3.plot(df['timestamp'], df['bp_bid'], 'b-', label='BP Bid', linewidth=0.5, alpha=0.7)
    ax3.plot(df['timestamp'], df['lt_bid'], 'orange', label='LT Bid', linewidth=0.5, alpha=0.7)
    ax3.set_ylabel('Bid 价格 (USDC)')
    ax3.set_xlabel('时间')
    ax3.legend(loc='upper right')
    ax3.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig('logs/spread_analysis.png', dpi=150)
    print(f"📊 图表已保存到: logs/spread_analysis.png")
    plt.show()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Backpack-Lighter 价差监控工具")
    parser.add_argument("--ticker", type=str, default="BTC", help="交易对 (默认: BTC)")
    parser.add_argument("--mode", type=str, choices=["live", "csv"], default="live",
                        help="模式: live=实时监控, csv=从CSV绘图")
    parser.add_argument("--csv-file", type=str, default="logs/spread_data_BTC.csv",
                        help="CSV 文件路径 (用于 csv 模式)")
    parser.add_argument("--no-plot", action="store_true", help="不显示图表，只输出文本")
    args = parser.parse_args()
    
    if args.mode == "csv":
        plot_from_csv(args.csv_file)
    else:
        monitor = SpreadMonitor(ticker=args.ticker)
        
        if args.no_plot or not HAS_MATPLOTLIB:
            asyncio.run(monitor.run_text_mode())
        else:
            monitor.run_plot_mode()


if __name__ == "__main__":
    main()

