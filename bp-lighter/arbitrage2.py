import asyncio
import json
import signal
import logging
import os
import sys
import time
import requests
import argparse
import traceback
import csv
import statistics
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv
from lighter.signer_client import SignerClient
import websockets
from datetime import datetime
import pytz

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from exchanges.backpack import BackpackClient

class Config:
    """Simple config class to wrap dictionary for Backpack client."""
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


class HedgeBot:
    """Backpack maker + Lighter hedge bot with dynamic z-score triggers (default enabled)."""

    def __init__(
        self,
        ticker: str,
        order_quantity: Decimal,
        max_position: Decimal = Decimal('0'),
        long_threshold: Decimal = Decimal('5'),
        short_threshold: Decimal = Decimal('5'),
        use_dynamic_thresholds: bool = True,  # 默认启用动态阈值
        spread_window: int = 300,  # 增大窗口，约5分钟数据
        spread_entry_z: float = 1.8,  # 降低触发阈值，更容易触发
        spread_exit_z: float = 0.5,  # 退出阈值
    ):
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.long_threshold = Decimal(long_threshold)
        self.short_threshold = Decimal(short_threshold)
        self.use_dynamic_thresholds = use_dynamic_thresholds
        self.spread_window = max(20, int(spread_window))
        self.spread_entry_z = float(spread_entry_z)
        self.spread_exit_z = float(spread_exit_z)
        self.long_spread_history = deque(maxlen=self.spread_window)
        self.short_spread_history = deque(maxlen=self.spread_window)
        self.spread_min_samples = max(10, self.spread_window // 4)
        self._long_armed = True
        self._short_armed = True
        self.lighter_order_filled = False
        self.current_order = {}
        self.max_position = order_quantity if max_position == Decimal('0') else max_position
        self.backpack_position = Decimal('0')
        self.lighter_position = Decimal('0')

        # Initialize logging to file
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/backpack_{ticker}_hedge_mode_log.txt"
        self.csv_filename = f"logs/backpack_{ticker}_hedge_mode_trades.csv"
        self.original_stdout = sys.stdout

        # Initialize CSV file with headers if it doesn't exist
        self._initialize_csv_file()

        # Setup logger
        self.logger = logging.getLogger(f"hedge_bot_{ticker}")
        self.logger.setLevel(logging.INFO)

        # Clear any existing handlers to avoid duplicates
        self.logger.handlers.clear()

        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.getLogger('websockets').setLevel(logging.WARNING)

        # Create file handler
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.INFO)

        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Create different formatters for file and console
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(console_formatter)

        # Add handlers to logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Prevent propagation to root logger to avoid duplicate messages
        self.logger.propagate = False

        # State management
        self.stop_flag = False

        # Backpack state
        self.backpack_client = None
        self.backpack_contract_id = None
        self.backpack_tick_size = None
        self.backpack_order_status = None

        # Backpack order book state for websocket-based BBO
        self.backpack_order_book = {'bids': {}, 'asks': {}}
        self.backpack_best_bid = None
        self.backpack_best_ask = None
        self.backpack_order_book_ready = False
        self.backpack_last_update_time = 0  # 跟踪最后更新时间

        # Lighter order book state
        self.lighter_client = None
        self.lighter_order_book = {"bids": {}, "asks": {}}
        self.lighter_best_bid = None
        self.lighter_best_ask = None
        self.lighter_order_book_ready = False
        self.lighter_order_book_offset = 0
        self.lighter_order_book_sequence_gap = False
        self.lighter_snapshot_loaded = False
        self.lighter_order_book_lock = asyncio.Lock()

        # Lighter WebSocket state
        self.lighter_ws_task = None
        self.lighter_order_result = None

        # Lighter order management
        self.lighter_order_status = None
        self.lighter_order_price = None
        self.lighter_order_side = None
        self.lighter_order_size = None
        self.lighter_order_start_time = None

        # Strategy state
        self.waiting_for_lighter_fill = False
        self.wait_start_time = None

        # Order execution tracking
        self.order_execution_complete = False

        # Current order details for immediate execution
        self.current_lighter_side = None
        self.current_lighter_quantity = None
        self.current_lighter_price = None
        self.lighter_order_info = None

        # Lighter API configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX'))

        # Backpack configuration
        self.backpack_public_key = os.getenv('BACKPACK_PUBLIC_KEY')
        self.backpack_secret_key = os.getenv('BACKPACK_SECRET_KEY')
        
        # 打印配置信息
        self._print_config()

    def _print_config(self):
        """打印当前配置信息"""
        self.logger.info("=" * 60)
        self.logger.info("🔧 动态阈值套利机器人配置")
        self.logger.info("=" * 60)
        self.logger.info(f"📊 交易对: {self.ticker}")
        self.logger.info(f"📦 订单数量: {self.order_quantity}")
        self.logger.info(f"📈 最大持仓: {self.max_position}")
        self.logger.info("-" * 60)
        if self.use_dynamic_thresholds:
            self.logger.info("✅ 动态阈值模式 (Z-Score)")
            self.logger.info(f"   📊 滚动窗口大小: {self.spread_window}")
            self.logger.info(f"   📊 最小样本数: {self.spread_min_samples}")
            self.logger.info(f"   🎯 入场 Z-Score: {self.spread_entry_z}")
            self.logger.info(f"   🔄 重置 Z-Score: {self.spread_exit_z}")
        else:
            self.logger.info("⚠️ 固定阈值模式")
            self.logger.info(f"   📈 Long 阈值: {self.long_threshold}")
            self.logger.info(f"   📉 Short 阈值: {self.short_threshold}")
        self.logger.info("=" * 60)

    def shutdown(self, signum=None, frame=None):
        """Graceful shutdown handler."""
        self.stop_flag = True
        self.logger.info("\n🛑 Stopping...")

        # Close WebSocket connections
        if self.backpack_client:
            try:
                # Note: disconnect() is async, but shutdown() is sync
                # We'll let the cleanup happen naturally
                self.logger.info("🔌 Backpack WebSocket will be disconnected")
            except Exception as e:
                self.logger.error(f"Error disconnecting Backpack WebSocket: {e}")

        # Cancel Lighter WebSocket task
        if self.lighter_ws_task and not self.lighter_ws_task.done():
            try:
                self.lighter_ws_task.cancel()
                self.logger.info("🔌 Lighter WebSocket task cancelled")
            except Exception as e:
                self.logger.error(f"Error cancelling Lighter WebSocket task: {e}")

        # Close logging handlers properly
        for handler in self.logger.handlers[:]:
            try:
                handler.close()
                self.logger.removeHandler(handler)
            except Exception:
                pass

    def _initialize_csv_file(self):
        """Initialize CSV file with headers if it doesn't exist."""
        if not os.path.exists(self.csv_filename):
            with open(self.csv_filename, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['exchange', 'timestamp', 'side', 'price', 'quantity'])

    def log_trade_to_csv(self, exchange: str, side: str, price: str, quantity: str):
        """Log trade details to CSV file."""
        timestamp = datetime.now(pytz.UTC).isoformat()

        with open(self.csv_filename, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                exchange,
                timestamp,
                side,
                price,
                quantity
            ])

        self.logger.info(f"📊 Trade logged to CSV: {exchange} {side} {quantity} @ {price}")

    def handle_lighter_order_result(self, order_data):
        """Handle Lighter order result from WebSocket."""
        try:
            order_data["avg_filled_price"] = (Decimal(order_data["filled_quote_amount"]) /
                                              Decimal(order_data["filled_base_amount"]))
            if order_data["is_ask"]:
                order_data["side"] = "SHORT"
                order_type = "OPEN"
                self.lighter_position -= Decimal(order_data["filled_base_amount"])
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"
                self.lighter_position += Decimal(order_data["filled_base_amount"])
            
            client_order_index = order_data["client_order_id"]

            self.logger.info(f"[{client_order_index}] [{order_type}] [Lighter] [FILLED]: "
                             f"{order_data['filled_base_amount']} @ {order_data['avg_filled_price']}")

            # Log Lighter trade to CSV
            self.log_trade_to_csv(
                exchange='Lighter',
                side=order_data['side'],
                price=str(order_data['avg_filled_price']),
                quantity=str(order_data['filled_base_amount'])
            )

            # Mark execution as complete
            self.lighter_order_filled = True  # Mark order as filled
            self.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    async def reset_lighter_order_book(self):
        """Reset Lighter order book state."""
        async with self.lighter_order_book_lock:
            self.lighter_order_book["bids"].clear()
            self.lighter_order_book["asks"].clear()
            self.lighter_order_book_offset = 0
            self.lighter_order_book_sequence_gap = False
            self.lighter_snapshot_loaded = False
            self.lighter_best_bid = None
            self.lighter_best_ask = None

    def update_lighter_order_book(self, side: str, levels: list):
        """Update Lighter order book with new levels."""
        for level in levels:
            # Handle different data structures - could be list [price, size] or dict {"price": ..., "size": ...}
            if isinstance(level, list) and len(level) >= 2:
                price = Decimal(level[0])
                size = Decimal(level[1])
            elif isinstance(level, dict):
                price = Decimal(level.get("price", 0))
                size = Decimal(level.get("size", 0))
            else:
                self.logger.warning(f"⚠️ Unexpected level format: {level}")
                continue

            if size > 0:
                self.lighter_order_book[side][price] = size
            else:
                # Remove zero size orders
                self.lighter_order_book[side].pop(price, None)

    def validate_order_book_offset(self, new_offset: int) -> bool:
        """Validate order book offset sequence."""
        # 只有当 new_offset 严格小于当前 offset 时才认为是乱序
        # 相等的情况在重连后是允许的（跳过这条更新即可）
        if new_offset < self.lighter_order_book_offset:
            self.logger.warning(
                f"⚠️ Out-of-order update: new_offset={new_offset}, current_offset={self.lighter_order_book_offset}")
            return False
        # 更新 offset
        self.lighter_order_book_offset = new_offset
        return True

    def validate_order_book_integrity(self) -> bool:
        """Validate order book integrity."""
        # Check for negative prices or sizes
        for side in ["bids", "asks"]:
            for price, size in self.lighter_order_book[side].items():
                if price <= 0 or size <= 0:
                    self.logger.error(f"❌ Invalid order book data: {side} price={price}, size={size}")
                    return False
        return True

    def get_lighter_best_levels(self) -> Tuple[Tuple[Decimal, Decimal], Tuple[Decimal, Decimal]]:
        """Get best bid and ask levels from Lighter order book."""
        best_bid = None
        best_ask = None

        if self.lighter_order_book["bids"]:
            best_bid_price = max(self.lighter_order_book["bids"].keys())
            best_bid_size = self.lighter_order_book["bids"][best_bid_price]
            best_bid = (best_bid_price, best_bid_size)

        if self.lighter_order_book["asks"]:
            best_ask_price = min(self.lighter_order_book["asks"].keys())
            best_ask_size = self.lighter_order_book["asks"][best_ask_price]
            best_ask = (best_ask_price, best_ask_size)

        return best_bid, best_ask

    def get_lighter_mid_price(self) -> Decimal:
        """Get mid price from Lighter order book."""
        best_bid, best_ask = self.get_lighter_best_levels()
        if not best_bid or not best_ask:
            raise Exception("Lighter order book not ready")

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate mid price - missing order book data")

        mid_price = (best_bid[0] + best_ask[0]) / Decimal('2')
        return mid_price

    def get_lighter_order_price(self, is_ask: bool) -> Decimal:
        """Get order price from Lighter order book."""
        best_bid, best_ask = self.get_lighter_best_levels()

        if best_bid is None or best_ask is None:
            raise Exception("Cannot calculate order price - missing order book data")

        if is_ask:
            order_price = best_bid[0] + self.tick_size
        else:
            order_price = best_ask[0] - self.tick_size

        return order_price

    def calculate_adjusted_price(self, original_price: Decimal, side: str, adjustment_percent: Decimal) -> Decimal:
        """Calculate adjusted price for order modification."""
        adjustment = original_price * adjustment_percent

        if side.lower() == 'buy':
            # For buy orders, increase price to improve fill probability
            return original_price + adjustment
        else:
            # For sell orders, decrease price to improve fill probability
            return original_price - adjustment

    async def request_fresh_snapshot(self, ws):
        """Request fresh order book snapshot."""
        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

    async def handle_lighter_ws(self):
        """Handle Lighter WebSocket connection and messages."""
        url = "wss://mainnet.zklighter.elliot.ai/stream"
        cleanup_counter = 0

        while not self.stop_flag:
            timeout_count = 0
            try:
                # Reset order book state before connecting
                await self.reset_lighter_order_book()

                async with websockets.connect(url) as ws:
                    # Subscribe to order book updates
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

                    # Subscribe to account orders updates
                    account_orders_channel = f"account_orders/{self.lighter_market_index}/{self.account_index}"

                    # Get auth token for the subscription
                    try:
                        # Set auth token to expire in 10 minutes
                        ten_minutes_deadline = int(time.time() + 10 * 60)
                        auth_token, err = self.lighter_client.create_auth_token_with_expiry(ten_minutes_deadline)
                        if err is not None:
                            self.logger.warning(f"⚠️ Failed to create auth token for account orders subscription: {err}")
                        else:
                            auth_message = {
                                "type": "subscribe",
                                "channel": account_orders_channel,
                                "auth": auth_token
                            }
                            await ws.send(json.dumps(auth_message))
                            self.logger.info("✅ Subscribed to account orders with auth token (expires in 10 minutes)")
                    except Exception as e:
                        self.logger.warning(f"⚠️ Error creating auth token for account orders subscription: {e}")

                    while not self.stop_flag:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            timeout_count += 1
                            if timeout_count % 3 == 0:
                                self.logger.warning(f"⏰ No message from Lighter websocket for {timeout_count} seconds")
                            continue
                        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException, AttributeError) as e:
                            if self.stop_flag:
                                break
                            self.logger.warning(f"⚠️ Lighter websocket connection issue: {e}")
                            break
                        except Exception as e:
                            self.logger.error(f"⚠️ Error in Lighter websocket: {e}")
                            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
                            break

                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError as e:
                            self.logger.warning(f"⚠️ JSON parsing error in Lighter websocket: {e}")
                            continue

                        timeout_count = 0

                        async with self.lighter_order_book_lock:
                            if data.get("type") == "subscribed/order_book":
                                self.lighter_order_book["bids"].clear()
                                self.lighter_order_book["asks"].clear()
                                order_book = data.get("order_book", {})
                                if order_book and "offset" in order_book:
                                    self.lighter_order_book_offset = order_book["offset"]
                                    self.logger.info(f"✅ Initial order book offset set to: {self.lighter_order_book_offset}")

                                bids = order_book.get("bids", [])
                                asks = order_book.get("asks", [])
                                self.update_lighter_order_book("bids", bids)
                                self.update_lighter_order_book("asks", asks)
                                self.lighter_snapshot_loaded = True
                                self.lighter_order_book_ready = True

                                self.logger.info(f"✅ Lighter order book snapshot loaded with "
                                                 f"{len(self.lighter_order_book['bids'])} bids and "
                                                 f"{len(self.lighter_order_book['asks'])} asks")

                            elif data.get("type") == "update/order_book" and self.lighter_snapshot_loaded:
                                order_book = data.get("order_book", {})
                                if not order_book or "offset" not in order_book:
                                    self.logger.warning("⚠️ Order book update missing offset, skipping")
                                    continue

                                new_offset = order_book["offset"]

                                if not self.validate_order_book_offset(new_offset):
                                    self.lighter_order_book_sequence_gap = True
                                    break

                                self.update_lighter_order_book("bids", order_book.get("bids", []))
                                self.update_lighter_order_book("asks", order_book.get("asks", []))

                                if not self.validate_order_book_integrity():
                                    self.logger.warning("🔄 Order book integrity check failed, requesting fresh snapshot...")
                                    break

                                best_bid, best_ask = self.get_lighter_best_levels()

                                if best_bid is not None:
                                    self.lighter_best_bid = best_bid[0]
                                if best_ask is not None:
                                    self.lighter_best_ask = best_ask[0]

                            elif data.get("type") == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                            elif data.get("type") == "update/account_orders":
                                orders = data.get("orders", {}).get(str(self.lighter_market_index), [])
                                for order in orders:
                                    if order.get("status") == "filled":
                                        self.handle_lighter_order_result(order)

                        cleanup_counter += 1
                        if cleanup_counter >= 1000:
                            cleanup_counter = 0

                        if self.lighter_order_book_sequence_gap:
                            try:
                                await self.request_fresh_snapshot(ws)
                                self.lighter_order_book_sequence_gap = False
                            except Exception as e:
                                self.logger.error(f"⚠️ Failed to request fresh snapshot: {e}")
                                break
            except Exception as e:
                self.logger.error(f"⚠️ Failed to connect to Lighter websocket: {e}")

            # Wait a bit before reconnecting
            await asyncio.sleep(2)

    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def initialize_lighter_client(self, max_retries: int = 3, retry_delay: float = 2.0):
        """Initialize the Lighter client with retry mechanism."""
        if self.lighter_client is None:
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY environment variable not set")

            last_error = None
            for attempt in range(max_retries):
                try:
                    self.logger.info(f"🔄 尝试连接 Lighter... (尝试 {attempt + 1}/{max_retries})")
                    
                    self.lighter_client = SignerClient(
                        url=self.lighter_base_url,
                        private_key=api_key_private_key,
                        account_index=self.account_index,
                        api_key_index=self.api_key_index,
                    )

                    # Check client
                    err = self.lighter_client.check_client()
                    if err is not None:
                        raise Exception(f"CheckClient error: {err}")

                    self.logger.info("✅ Lighter client initialized successfully")
                    return self.lighter_client
                    
                except Exception as e:
                    last_error = e
                    self.logger.warning(f"⚠️ Lighter 连接失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                    
                    if attempt < max_retries - 1:
                        self.logger.info(f"⏳ 等待 {retry_delay} 秒后重试...")
                        time.sleep(retry_delay)
                        retry_delay *= 1.5  # 指数退避
                    else:
                        raise Exception(f"Lighter 初始化失败，已重试 {max_retries} 次。最后错误: {last_error}")
        
        return self.lighter_client

    def initialize_backpack_client(self):
        """Initialize the Backpack client."""
        if not self.backpack_public_key or not self.backpack_secret_key:
            raise ValueError("BACKPACK_PUBLIC_KEY and BACKPACK_SECRET_KEY must be set in environment variables")

        # Create config for Backpack client
        config_dict = {
            'ticker': self.ticker,
            'contract_id': '',  # Will be set when we get contract info
            'quantity': self.order_quantity,
            'tick_size': Decimal('0.01'),  # Will be updated when we get contract info
            'close_order_side': 'sell'  # Default, will be updated based on strategy
        }

        # Wrap in Config class for Backpack client
        config = Config(config_dict)

        # Initialize Backpack client
        self.backpack_client = BackpackClient(config)

        self.logger.info("✅ Backpack client initialized successfully")
        return self.backpack_client

    def get_lighter_market_config(self) -> Tuple[int, int, int, Decimal]:
        """Get Lighter market configuration."""
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if not response.text.strip():
                raise Exception("Empty response from Lighter API")

            data = response.json()

            if "order_books" not in data:
                raise Exception("Unexpected response format")

            for market in data["order_books"]:
                if market["symbol"] == self.ticker:
                    price_multiplier = pow(10, market["supported_price_decimals"])
                    return (market["market_id"], 
                           pow(10, market["supported_size_decimals"]), 
                           price_multiplier,
                           Decimal("1") / (Decimal("10") ** market["supported_price_decimals"])
                           )
            raise Exception(f"Ticker {self.ticker} not found")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def get_backpack_contract_info(self) -> Tuple[str, Decimal]:
        """Get Backpack contract ID and tick size."""
        if not self.backpack_client:
            raise Exception("Backpack client not initialized")

        contract_id, tick_size = await self.backpack_client.get_contract_attributes()

        if self.order_quantity < self.backpack_client.config.quantity:
            raise ValueError(
                f"Order quantity is less than min quantity: {self.order_quantity} < {self.backpack_client.config.quantity}")

        return contract_id, tick_size

    async def get_backpack_position(self) -> Decimal:
        """Get Backpack position."""
        if not self.backpack_client:
            raise Exception("Backpack client not initialized")

        return await self.backpack_client.get_account_positions()

    async def fetch_backpack_bbo_prices(self) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Backpack - 优先使用 WebSocket，超时则使用 REST API。"""
        current_time = time.time()
        max_data_age = 2.0  # 数据最大有效期（秒）
        
        # 检查 WebSocket 数据是否有效且新鲜
        if (self.backpack_best_bid and self.backpack_best_ask and 
            self.backpack_best_bid > 0 and self.backpack_best_ask > 0 and
            self.backpack_best_bid < self.backpack_best_ask and
            (current_time - self.backpack_last_update_time) < max_data_age):
            return self.backpack_best_bid, self.backpack_best_ask
        
        # WebSocket 数据不可用或太旧，使用 REST API
        if not self.backpack_client:
            raise Exception("Backpack client not initialized")

        best_bid, best_ask = await self.backpack_client.fetch_bbo_prices(self.backpack_contract_id)
        
        # 更新本地缓存
        if best_bid and best_ask:
            self.backpack_best_bid = best_bid
            self.backpack_best_ask = best_ask
            self.backpack_last_update_time = current_time

        return best_bid, best_ask

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round price to tick size."""
        if self.backpack_tick_size is None:
            return price
        return (price / self.backpack_tick_size).quantize(Decimal('1')) * self.backpack_tick_size

    async def place_bbo_order(self, side: str, quantity: Decimal):
        # Get best bid/ask prices
        best_bid, best_ask = await self.fetch_backpack_bbo_prices()

        # Place the order using Backpack client
        order_result = await self.backpack_client.place_open_order(
            contract_id=self.backpack_contract_id,
            quantity=quantity,
            direction=side.lower()
        )

        if order_result.success:
            return order_result.order_id
        else:
            raise Exception(f"Failed to place order: {order_result.error_message}")

    async def place_backpack_post_only_order(self, side: str, quantity: Decimal) -> bool:
        """Place a post-only order on Backpack.
        
        Returns:
            bool: True if order was filled, False if canceled/failed
        """
        if not self.backpack_client:
            raise Exception("Backpack client not initialized")

        fill_timeout = 5  # 5秒超时，与 EdgeX 版本一致
        self.backpack_order_status = None
        self.logger.info(f"[OPEN] [Backpack] [{side}] Placing Backpack POST-ONLY order")
        order_id = await self.place_bbo_order(side, quantity)

        start_time = time.time()
        while not self.stop_flag:
            if self.backpack_order_status == 'CANCELED':
                # 订单被撤销，返回失败
                self.logger.warning(f"⚠️ Backpack order was canceled")
                return False
            elif self.backpack_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING', 'PARTIALLY_FILLED']:
                await asyncio.sleep(0.3)
                if time.time() - start_time > fill_timeout:
                    self.logger.warning(f"⏰ Backpack order timeout after {fill_timeout}s, canceling...")
                    try:
                        # Cancel the order using Backpack client
                        cancel_result = await self.backpack_client.cancel_order(order_id)
                        if cancel_result.success:
                            # 检查是否有部分成交
                            if cancel_result.filled_size and cancel_result.filled_size > 0:
                                self.logger.info(f"📊 Partial fill: {cancel_result.filled_size}")
                                # 部分成交也算成功，但需要调整后续 Lighter 订单数量
                                return True
                        else:
                            self.logger.error(f"❌ Error canceling Backpack order: {cancel_result.error_message}")
                    except Exception as e:
                        self.logger.error(f"❌ Error canceling Backpack order: {e}")
                    return False
            elif self.backpack_order_status == 'FILLED':
                return True
            else:
                if self.backpack_order_status is not None:
                    self.logger.error(f"❌ Unknown Backpack order status: {self.backpack_order_status}")
                    return False
                else:
                    await asyncio.sleep(0.3)
        return False

    def handle_backpack_order_book_update(self, message):
        """Handle Backpack order book updates from WebSocket."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            self.logger.debug(f"Received Backpack depth message: {message}")

            # 获取数据部分
            data = message.get("data", {})
            if not data:
                data = message  # 有时候数据直接在顶层
            
            if not isinstance(data, dict):
                return

            # 支持多种格式：b/a 或 bids/asks
            bids = data.get('b', []) or data.get('bids', [])
            asks = data.get('a', []) or data.get('asks', [])
            
            updated = False
            
            # Update bids - format is [["price", "size"], ...]
            for bid in bids:
                if isinstance(bid, list) and len(bid) >= 2:
                    price = Decimal(str(bid[0]))
                    size = Decimal(str(bid[1]))
                    if size > 0:
                        self.backpack_order_book['bids'][price] = size
                    else:
                        self.backpack_order_book['bids'].pop(price, None)
                    updated = True

            # Update asks - format is [["price", "size"], ...]
            for ask in asks:
                if isinstance(ask, list) and len(ask) >= 2:
                    price = Decimal(str(ask[0]))
                    size = Decimal(str(ask[1]))
                    if size > 0:
                        self.backpack_order_book['asks'][price] = size
                    else:
                        self.backpack_order_book['asks'].pop(price, None)
                    updated = True

            if updated:
                # Update best bid and ask
                if self.backpack_order_book['bids']:
                    self.backpack_best_bid = max(self.backpack_order_book['bids'].keys())
                if self.backpack_order_book['asks']:
                    self.backpack_best_ask = min(self.backpack_order_book['asks'].keys())
                
                # 更新时间戳
                self.backpack_last_update_time = time.time()

                if not self.backpack_order_book_ready:
                    self.backpack_order_book_ready = True
                    self.logger.info(f"📊 Backpack order book ready - Best bid: {self.backpack_best_bid}, "
                                     f"Best ask: {self.backpack_best_ask}")
                else:
                    self.logger.debug(f"📊 Order book updated - Best bid: {self.backpack_best_bid}, "
                                      f"Best ask: {self.backpack_best_ask}")

        except Exception as e:
            self.logger.error(f"Error handling Backpack order book update: {e}")
            self.logger.error(f"Message content: {message}")

    def handle_backpack_order_update(self, order_data):
        """Handle Backpack order updates from WebSocket."""
        side = order_data.get('side', '').lower()
        filled_size = Decimal(order_data.get('filled_size', '0'))
        price = Decimal(order_data.get('price', '0'))

        if side == 'buy':
            lighter_side = 'sell'
        else:
            lighter_side = 'buy'

        # Store order details for immediate execution
        self.current_lighter_side = lighter_side
        self.current_lighter_quantity = filled_size
        self.current_lighter_price = price

        self.lighter_order_info = {
            'lighter_side': lighter_side,
            'quantity': filled_size,
            'price': price
        }

        self.waiting_for_lighter_fill = True


    async def place_lighter_market_order(self, lighter_side: str, quantity: Decimal, price: Decimal) -> bool:
        """Place a market order on Lighter with retry mechanism.
        
        Returns:
            bool: True if order was filled, False if failed
        """
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                if not self.lighter_client:
                    self.initialize_lighter_client()

                best_bid, best_ask = self.get_lighter_best_levels()
                
                if not best_bid or not best_ask:
                    self.logger.warning(f"⚠️ Lighter order book not ready, retry {attempt + 1}/{max_retries}")
                    await asyncio.sleep(retry_delay)
                    continue

                # Determine order parameters
                if lighter_side.lower() == 'buy':
                    order_type = "CLOSE"
                    is_ask = False
                    price = best_ask[0] * Decimal('1.002')
                else:
                    order_type = "OPEN"
                    is_ask = True
                    price = best_bid[0] * Decimal('0.998')

                # Reset order state
                self.lighter_order_filled = False
                self.lighter_order_price = price
                self.lighter_order_side = lighter_side
                self.lighter_order_size = quantity

                client_order_index = int(time.time() * 1000)
                # Sign the order transaction - 使用 IOC (Immediate Or Cancel) 模式
                _, tx_info, error = self.lighter_client.sign_create_order(
                    market_index=self.lighter_market_index,
                    client_order_index=client_order_index,
                    base_amount=int(quantity * self.base_amount_multiplier),
                    price=int(price * self.price_multiplier),
                    is_ask=is_ask,
                    order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                    time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    reduce_only=False,
                    trigger_price=0,
                    order_expiry=0,  # IOC 订单必须使用 0
                )
                if error is not None:
                    raise Exception(f"Sign error: {error}")

                # Send the order
                tx_hash = await self.lighter_client.send_tx(
                    tx_type=self.lighter_client.TX_TYPE_CREATE_ORDER,
                    tx_info=tx_info
                )

                self.logger.info(f"[{client_order_index}] [{order_type}] [Lighter] [OPEN]: {quantity}")

                # Monitor order with timeout
                filled = await self.monitor_lighter_order(client_order_index)
                
                if filled:
                    return True
                else:
                    self.logger.warning(f"⚠️ Lighter order not filled, retry {attempt + 1}/{max_retries}")
                    
            except Exception as e:
                self.logger.error(f"❌ Error placing Lighter order (attempt {attempt + 1}/{max_retries}): {e}")
                
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
        
        self.logger.error(f"❌ Failed to place Lighter order after {max_retries} attempts")
        return False

    async def monitor_lighter_order(self, client_order_index: int) -> bool:
        """Monitor Lighter order and return fill status.
        
        IOC 模式订单会立即成交或取消，只需等待 WebSocket 确认。
        
        Returns:
            bool: True if order was filled, False if timeout
        """
        start_time = time.time()
        timeout = 10  # IOC 模式应该很快，10秒超时
        
        while not self.lighter_order_filled and not self.stop_flag:
            # Check for timeout
            if time.time() - start_time > timeout:
                self.logger.warning(f"⚠️ Timeout waiting for Lighter order fill after {timeout}s")
                # IOC 模式超时后，假设订单已成交（WebSocket 可能延迟）
                self.logger.warning("⚠️ 使用 fallback - 假设订单已成交，手动更新仓位")
                
                # 手动更新 Lighter 仓位（因为 WebSocket 没收到确认）
                if self.lighter_order_side == 'sell':
                    self.lighter_position -= self.lighter_order_size
                else:
                    self.lighter_position += self.lighter_order_size
                
                self.logger.info(f"📊 Lighter 仓位更新 (fallback): {self.lighter_position}")
                
                # 记录到 CSV
                self.log_trade_to_csv(
                    exchange='Lighter',
                    side='SHORT' if self.lighter_order_side == 'sell' else 'LONG',
                    price=str(self.lighter_order_price),
                    quantity=str(self.lighter_order_size)
                )
                
                self.lighter_order_filled = True
                self.waiting_for_lighter_fill = False
                self.order_execution_complete = True
                return True

            await asyncio.sleep(0.1)  # Check every 100ms
        
        # 订单已成交（通过 WebSocket 确认）
        self.waiting_for_lighter_fill = False
        self.order_execution_complete = True
        return True

    async def modify_lighter_order(self, client_order_index: int, new_price: Decimal):
        """Modify current Lighter order with new price using client_order_index."""
        try:
            if client_order_index is None:
                self.logger.error("❌ Cannot modify order - no order ID available")
                return

            # Calculate new Lighter price
            lighter_price = int(new_price * self.price_multiplier)

            self.logger.info(f"🔧 Attempting to modify order - Market: {self.lighter_market_index}, "
                             f"Client Order Index: {client_order_index}, New Price: {lighter_price}")

            # Use the native SignerClient's modify_order method
            tx_info, tx_hash, error = await self.lighter_client.modify_order(
                market_index=self.lighter_market_index,
                order_index=client_order_index,  # Use client_order_index directly
                base_amount=int(self.lighter_order_size * self.base_amount_multiplier),
                price=lighter_price,
                trigger_price=0
            )

            if error is not None:
                self.logger.error(f"❌ Lighter order modification error: {error}")
                return

            self.lighter_order_price = new_price
            self.logger.info(f"🔄 Lighter order modified successfully: {self.lighter_order_side} "
                             f"{self.lighter_order_size} @ {new_price}")

        except Exception as e:
            self.logger.error(f"❌ Error modifying Lighter order: {e}")
            import traceback
            self.logger.error(f"❌ Full traceback: {traceback.format_exc()}")

    async def setup_backpack_websocket(self):
        """Setup Backpack websocket for order updates and order book data."""
        if not self.backpack_client:
            raise Exception("Backpack client not initialized")

        def order_update_handler(order_data):
            """Handle order updates from Backpack WebSocket."""
            if order_data.get('contract_id') != self.backpack_contract_id:
                return
            try:
                order_id = order_data.get('order_id')
                status = order_data.get('status')
                side = order_data.get('side', '').lower()
                filled_size = Decimal(order_data.get('filled_size', '0'))
                size = Decimal(order_data.get('size', '0'))
                price = order_data.get('price', '0')

                if side == 'buy':
                    order_type = "OPEN"
                else:
                    order_type = "CLOSE"
                
                if status == 'CANCELED' and filled_size > 0:
                    status = 'FILLED'

                # Handle the order update
                if status == 'FILLED' and self.backpack_order_status != 'FILLED':
                    if side == 'buy':
                        self.backpack_position += filled_size
                    else:
                        self.backpack_position -= filled_size
                    self.logger.info(f"[{order_id}] [{order_type}] [Backpack] [{status}]: {filled_size} @ {price}")
                    self.backpack_order_status = status

                    # Log Backpack trade to CSV
                    self.log_trade_to_csv(
                        exchange='Backpack',
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )

                    self.handle_backpack_order_update({
                        'order_id': order_id,
                        'side': side,
                        'status': status,
                        'size': size,
                        'price': price,
                        'contract_id': self.backpack_contract_id,
                        'filled_size': filled_size
                    })
                elif self.backpack_order_status != 'FILLED':
                    if status == 'OPEN':
                        self.logger.info(f"[{order_id}] [{order_type}] [Backpack] [{status}]: {size} @ {price}")
                    else:
                        self.logger.info(f"[{order_id}] [{order_type}] [Backpack] [{status}]: {filled_size} @ {price}")
                    self.backpack_order_status = status

            except Exception as e:
                self.logger.error(f"Error handling Backpack order update: {e}")

        try:
            # Setup order update handler
            self.backpack_client.setup_order_update_handler(order_update_handler)
            self.logger.info("✅ Backpack WebSocket order update handler set up")

            # Connect to Backpack WebSocket
            await self.backpack_client.connect()
            self.logger.info("✅ Backpack WebSocket connection established")

            # 启用 depth WebSocket 获取实时价格
            await self.setup_backpack_depth_websocket()

        except Exception as e:
            self.logger.error(f"Could not setup Backpack WebSocket handlers: {e}")

    async def fetch_backpack_orderbook_snapshot(self):
        """Fetch initial orderbook snapshot from Backpack REST API."""
        try:
            url = f"https://api.backpack.exchange/api/v1/depth?symbol={self.backpack_contract_id}"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                # Process bids
                for bid in data.get('bids', []):
                    price = Decimal(bid[0])
                    size = Decimal(bid[1])
                    if size > 0:
                        self.backpack_order_book['bids'][price] = size
                # Process asks
                for ask in data.get('asks', []):
                    price = Decimal(ask[0])
                    size = Decimal(ask[1])
                    if size > 0:
                        self.backpack_order_book['asks'][price] = size
                # Update best bid/ask
                if self.backpack_order_book['bids']:
                    self.backpack_best_bid = max(self.backpack_order_book['bids'].keys())
                if self.backpack_order_book['asks']:
                    self.backpack_best_ask = min(self.backpack_order_book['asks'].keys())
                # 标记订单簿已就绪，更新时间戳
                self.backpack_order_book_ready = True
                self.backpack_last_update_time = time.time()
                self.logger.info(f"📊 Backpack 订单簿快照加载完成 - Best bid: {self.backpack_best_bid}, Best ask: {self.backpack_best_ask}")
                return True
        except Exception as e:
            self.logger.error(f"❌ 获取 Backpack 订单簿快照失败: {e}")
        return False

    async def setup_backpack_depth_websocket(self):
        """Setup separate WebSocket connection for Backpack depth updates."""
        try:
            import websockets

            # 首先获取订单簿快照
            await self.fetch_backpack_orderbook_snapshot()

            async def handle_depth_websocket():
                """Handle depth WebSocket connection."""
                url = "wss://ws.backpack.exchange"

                while not self.stop_flag:
                    try:
                        async with websockets.connect(url) as ws:
                            # Subscribe to depth updates
                            subscribe_message = {
                                "method": "SUBSCRIBE",
                                "params": [f"depth.{self.backpack_contract_id}"]
                            }
                            await ws.send(json.dumps(subscribe_message))
                            self.logger.info(f"✅ Subscribed to depth updates for {self.backpack_contract_id}")

                            # Listen for messages
                            async for message in ws:
                                if self.stop_flag:
                                    break

                                try:
                                    # Handle ping frames
                                    if isinstance(message, bytes) and message == b'\x09':
                                        await ws.pong()
                                        continue

                                    data = json.loads(message)
                                    
                                    # 诊断：打印前5条消息
                                    if not hasattr(self, '_ws_msg_count'):
                                        self._ws_msg_count = 0
                                    if self._ws_msg_count < 5:
                                        # 显示更详细的消息内容
                                        msg_preview = str(data)[:200] if len(str(data)) > 200 else str(data)
                                        self.logger.info(f"🔍 Backpack WS #{self._ws_msg_count}: {msg_preview}")
                                        self._ws_msg_count += 1

                                    # Handle depth updates - 检查多种可能的消息格式
                                    stream = data.get('stream', '')
                                    if 'depth' in stream:
                                        self.handle_backpack_order_book_update(data)
                                    elif data.get('data'):
                                        # 检查 data 中是否有 bids/asks 或 b/a
                                        inner_data = data.get('data', {})
                                        if isinstance(inner_data, dict) and ('b' in inner_data or 'a' in inner_data or 'bids' in inner_data or 'asks' in inner_data):
                                            self.handle_backpack_order_book_update(data)

                                except json.JSONDecodeError as e:
                                    self.logger.warning(f"Failed to parse depth WebSocket message: {e}")
                                except Exception as e:
                                    self.logger.error(f"Error handling depth WebSocket message: {e}")

                    except websockets.exceptions.ConnectionClosed:
                        self.logger.warning("Depth WebSocket connection closed, reconnecting...")
                    except Exception as e:
                        self.logger.error(f"Depth WebSocket error: {e}")

                    # Wait before reconnecting
                    if not self.stop_flag:
                        await asyncio.sleep(2)

            # Start depth WebSocket in background
            asyncio.create_task(handle_depth_websocket())
            self.logger.info("✅ Backpack depth WebSocket task started")

        except Exception as e:
            self.logger.error(f"Could not setup Backpack depth WebSocket: {e}")

    def get_lighter_position(self):
        url = "https://mainnet.zklighter.elliot.ai/api/v1/account"
        headers = {"accept": "application/json"}

        current_position = None
        parameters = {"by": "index", "value": self.account_index}
        try:
            response = requests.get(url, headers=headers, params=parameters, timeout=10)
            response.raise_for_status()  # Raise an exception for bad status codes

            # Check if response has content
            if not response.text.strip():
                print("⚠️ Empty response from Lighter API for position check")
                return self.lighter_position

            data = response.json()

            if 'accounts' not in data or not data['accounts']:
                print(f"⚠️ Unexpected response format from Lighter API: {data}")
                return self.lighter_position

            positions = data['accounts'][0].get('positions', [])
            for position in positions:
                if position.get('symbol') == self.ticker:
                    current_position = Decimal(position['position']) * position['sign']
                    break
            if current_position is None:
                current_position = 0

        except requests.exceptions.RequestException as e:
            print(f"⚠️ Network error getting position: {e}")
        except json.JSONDecodeError as e:
            print(f"⚠️ JSON parsing error in position response: {e}")
            print(f"Response text: {response.text[:200]}...")  # Show first 200 chars
        except Exception as e:
            print(f"⚠️ Unexpected error getting position: {e}")

        return current_position

    def _record_spreads(self, long_spread: Decimal | None, short_spread: Decimal | None):
        if long_spread is not None:
            self.long_spread_history.append(float(long_spread))
        if short_spread is not None:
            self.short_spread_history.append(float(short_spread))

    def _compute_zscore(self, history: deque, current_value: Decimal | None):
        if current_value is None or len(history) < self.spread_min_samples:
            return None
        try:
            mean = statistics.fmean(history)
            std = statistics.pstdev(history)
        except statistics.StatisticsError:
            return None
        if std <= 1e-9:
            return None
        return (float(current_value) - mean) / std

    def _evaluate_spread_signals(self, long_spread, short_spread):
        if not self.use_dynamic_thresholds:
            long_signal = bool(long_spread and long_spread > self.long_threshold)
            short_signal = bool(short_spread and short_spread > self.short_threshold)
            return long_signal, short_signal, None, None

        long_z = self._compute_zscore(self.long_spread_history, long_spread)
        short_z = self._compute_zscore(self.short_spread_history, short_spread)

        if long_z is not None and long_z <= self.spread_exit_z:
            self._long_armed = True
        if short_z is not None and short_z <= self.spread_exit_z:
            self._short_armed = True

        long_signal = long_z is not None and self._long_armed and long_z >= self.spread_entry_z
        short_signal = short_z is not None and self._short_armed and short_z >= self.spread_entry_z

        if long_signal:
            self._long_armed = False
        if short_signal:
            self._short_armed = False

        return long_signal, short_signal, long_z, short_z

    async def _execute_long_trade(self):
        if self.stop_flag:
            return

        if abs(self.backpack_position + self.lighter_position) > self.order_quantity * 2:
            self.logger.error(f"❌ Position diff too large: {self.backpack_position + self.lighter_position}")
            return

        self.order_execution_complete = False
        self.waiting_for_lighter_fill = False

        try:
            order_filled = await self.place_backpack_post_only_order('buy', self.order_quantity)
            if not order_filled:
                self.logger.warning("⚠️ Backpack order not filled, skipping Lighter hedge")
                return
        except Exception as err:
            self.logger.error(f"⚠️ Error executing long trade: {err}")
            self.logger.error(traceback.format_exc())
            return

        await self._wait_for_trade_completion()

    async def _execute_short_trade(self):
        if self.stop_flag:
            return

        if abs(self.backpack_position + self.lighter_position) > self.order_quantity * 2:
            self.logger.error(f"❌ Position diff too large: {self.backpack_position + self.lighter_position}")
            return

        self.order_execution_complete = False
        self.waiting_for_lighter_fill = False

        try:
            order_filled = await self.place_backpack_post_only_order('sell', self.order_quantity)
            if not order_filled:
                self.logger.warning("⚠️ Backpack order not filled, skipping Lighter hedge")
                return
        except Exception as err:
            self.logger.error(f"⚠️ Error executing short trade: {err}")
            self.logger.error(traceback.format_exc())
            return

        await self._wait_for_trade_completion()

    async def _wait_for_trade_completion(self):
        start_time = time.time()
        while not self.order_execution_complete and not self.stop_flag:
            if self.waiting_for_lighter_fill:
                await self.place_lighter_market_order(
                    self.current_lighter_side or 'sell',
                    self.current_lighter_quantity or self.order_quantity,
                    self.current_lighter_price or Decimal('0')
                )
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
                break

    async def trading_loop(self):
        """Main trading loop implementing the new strategy."""
        self.logger.info(f"🚀 Starting hedge bot for {self.ticker}")

        # Initialize clients
        try:
            self.initialize_lighter_client()
            self.initialize_backpack_client()

            # Get contract info
            self.backpack_contract_id, self.backpack_tick_size = await self.get_backpack_contract_info()
            self.lighter_market_index, self.base_amount_multiplier, self.price_multiplier, self.tick_size = self.get_lighter_market_config()

            self.logger.info(f"Contract info loaded - Backpack: {self.backpack_contract_id}, "
                             f"Lighter: {self.lighter_market_index}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            return

        # Setup Backpack websocket
        try:
            await self.setup_backpack_websocket()
            self.logger.info("✅ Backpack WebSocket connection established")

            # Wait for initial order book data with timeout
            self.logger.info("⏳ Waiting for initial order book data...")
            timeout = 10  # seconds
            start_time = time.time()
            while not self.backpack_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.backpack_order_book_ready:
                self.logger.info("✅ WebSocket order book data received")
            else:
                self.logger.warning("⚠️ WebSocket order book not ready, will use REST API fallback")

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Backpack websocket: {e}")
            return

        # Setup Lighter websocket
        try:
            self.lighter_ws_task = asyncio.create_task(self.handle_lighter_ws())
            self.logger.info("✅ Lighter WebSocket task started")

            # Wait for initial Lighter order book data with timeout
            self.logger.info("⏳ Waiting for initial Lighter order book data...")
            timeout = 10  # seconds
            start_time = time.time()
            while not self.lighter_order_book_ready and not self.stop_flag:
                if time.time() - start_time > timeout:
                    self.logger.warning(f"⚠️ Timeout waiting for Lighter WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.lighter_order_book_ready:
                self.logger.info("✅ Lighter WebSocket order book data received")
            else:
                self.logger.warning("⚠️ Lighter WebSocket order book not ready")

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Lighter websocket: {e}")
            return

        await asyncio.sleep(5)
        self.logger.info("✅ Initialization complete, entering spread monitor loop")

        # 用于定期日志的计数器
        loop_counter = 0
        last_log_time = time.time()

        while not self.stop_flag:
            try:
                backpack_bid, backpack_ask = await self.fetch_backpack_bbo_prices()
            except Exception as exc:
                self.logger.warning(f"⚠️ Failed to fetch Backpack BBO: {exc}")
                await asyncio.sleep(0.2)
                continue

            lighter_bid, lighter_ask = self.get_lighter_best_levels()
            if not lighter_bid or not lighter_ask:
                await asyncio.sleep(0.1)
                continue

            long_spread = lighter_bid[0] - backpack_bid if backpack_bid > 0 else None
            short_spread = backpack_ask - lighter_ask[0] if backpack_ask > 0 else None

            self._record_spreads(long_spread, short_spread)
            long_signal, short_signal, long_z, short_z = self._evaluate_spread_signals(long_spread, short_spread)

            # 每15秒输出一次状态日志（比原版更频繁，方便调试动态阈值）
            loop_counter += 1
            current_time = time.time()
            if current_time - last_log_time >= 15:
                last_log_time = current_time
                samples = len(self.long_spread_history)
                status_msg = f"📊 状态更新 | BP bid: {backpack_bid} ask: {backpack_ask} | LT bid: {lighter_bid[0] if lighter_bid else 'N/A'} ask: {lighter_ask[0] if lighter_ask else 'N/A'}"
                self.logger.info(status_msg)
                long_str = f"{long_spread:.2f}" if long_spread is not None else "N/A"
                short_str = f"{short_spread:.2f}" if short_spread is not None else "N/A"
                spread_msg = f"📈 价差 | Long: {long_str} | Short: {short_str} | 样本数: {samples}/{self.spread_min_samples}"
                self.logger.info(spread_msg)
                
                # 输出标准差统计信息
                if samples >= self.spread_min_samples:
                    try:
                        long_mean = statistics.fmean(self.long_spread_history)
                        long_std = statistics.pstdev(self.long_spread_history)
                        short_mean = statistics.fmean(self.short_spread_history)
                        short_std = statistics.pstdev(self.short_spread_history)
                        self.logger.info(f"📊 统计 | Long: 均值={long_mean:.2f}, 标准差={long_std:.2f} | Short: 均值={short_mean:.2f}, 标准差={short_std:.2f}")
                        
                        # 计算当前触发阈值
                        long_trigger = long_mean + self.spread_entry_z * long_std
                        short_trigger = short_mean + self.spread_entry_z * short_std
                        self.logger.info(f"🎯 触发值 | Long 需>{long_trigger:.2f} | Short 需>{short_trigger:.2f}")
                    except Exception as e:
                        self.logger.warning(f"⚠️ 统计计算错误: {e}")
                
                if self.use_dynamic_thresholds and long_z is not None and short_z is not None:
                    zscore_msg = f"📐 Z-Score | Long: {long_z:.2f} (触发>{self.spread_entry_z}) | Short: {short_z:.2f} (触发>{self.spread_entry_z})"
                    self.logger.info(zscore_msg)
                    
                    # 显示 armed 状态
                    armed_msg = f"🔫 Armed | Long: {'✅' if self._long_armed else '❌'} | Short: {'✅' if self._short_armed else '❌'}"
                    self.logger.info(armed_msg)

            if self.use_dynamic_thresholds:
                if long_z is not None:
                    self.logger.debug(f"📐 Long spread z-score: {long_z:.2f}")
                if short_z is not None:
                    self.logger.debug(f"📐 Short spread z-score: {short_z:.2f}")

            self.backpack_position = await self.get_backpack_position()
            self.lighter_position = self.get_lighter_position()

            if long_signal and self.backpack_position < self.max_position:
                long_z_str = f"{long_z:.2f}" if long_z is not None else "N/A"
                self.logger.info(f"📈 Long signal triggered | spread={long_spread} | z={long_z_str}")
                await self._execute_long_trade()
            elif short_signal and self.backpack_position > -self.max_position:
                short_z_str = f"{short_z:.2f}" if short_z is not None else "N/A"
                self.logger.info(f"📉 Short signal triggered | spread={short_spread} | z={short_z_str}")
                await self._execute_short_trade()
            else:
                await asyncio.sleep(0.05)

    async def run(self):
        """Run the hedge bot."""
        self.setup_signal_handlers()

        try:
            await self.trading_loop()
        except KeyboardInterrupt:
            self.logger.info("\n🛑 Received interrupt signal...")
        finally:
            self.logger.info("🔄 Cleaning up...")
            self.shutdown()


def parse_args():
    parser = argparse.ArgumentParser(description="Backpack ↔ Lighter 动态阈值套利机器人 (默认启用 Z-Score 模式)")
    parser.add_argument("--ticker", type=str, default="BTC", help="交易对 (默认: BTC)")
    parser.add_argument("--size", type=str, required=True, help="每笔订单数量 (例如 0.002)")
    parser.add_argument("--max-position", type=str, default=None, help="最大持仓量")
    
    # 固定阈值参数（仅在禁用动态阈值时使用）
    parser.add_argument("--long-threshold", type=str, default="5", help="固定 Long 阈值 (仅 --no-dynamic 时有效)")
    parser.add_argument("--short-threshold", type=str, default="5", help="固定 Short 阈值 (仅 --no-dynamic 时有效)")
    
    # 动态阈值参数
    parser.add_argument("--no-dynamic", action="store_true", help="禁用动态阈值，使用固定阈值")
    parser.add_argument("--spread-window", type=int, default=300, help="滚动窗口大小 (默认: 300，约5分钟)")
    parser.add_argument("--spread-entry-z", type=float, default=1.8, help="入场 Z-Score 阈值 (默认: 1.8)")
    parser.add_argument("--spread-exit-z", type=float, default=0.5, help="重置信号 Z-Score 阈值 (默认: 0.5)")
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()
    order_size = Decimal(args.size)
    max_position = Decimal(args.max_position) if args.max_position else order_size
    
    # 默认启用动态阈值，除非指定 --no-dynamic
    use_dynamic = not args.no_dynamic
    
    bot = HedgeBot(
        ticker=args.ticker.upper(),
        order_quantity=order_size,
        max_position=max_position,
        long_threshold=Decimal(args.long_threshold),
        short_threshold=Decimal(args.short_threshold),
        use_dynamic_thresholds=use_dynamic,
        spread_window=args.spread_window,
        spread_entry_z=args.spread_entry_z,
        spread_exit_z=args.spread_exit_z,
    )
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()

