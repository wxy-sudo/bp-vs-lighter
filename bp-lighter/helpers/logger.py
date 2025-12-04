"""
Trading logger with structured output and error handling.
"""

import os
import csv
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import pytz


class TradingLogger:
    """Enhanced logging with structured output and error handling."""

    def __init__(self, exchange: str, ticker: str, log_to_console: bool = False):
        self.exchange = exchange
        self.ticker = ticker

        project_root = Path(__file__).resolve().parents[1]
        logs_dir = project_root / "logs"
        logs_dir.mkdir(exist_ok=True)

        order_file_name = f"{exchange}_{ticker}_orders.csv"
        debug_log_file_name = f"{exchange}_{ticker}_activity.log"

        account_name = os.getenv('ACCOUNT_NAME')
        if account_name:
            order_file_name = f"{exchange}_{ticker}_{account_name}_orders.csv"
            debug_log_file_name = f"{exchange}_{ticker}_{account_name}_activity.log"

        self.log_file = logs_dir / order_file_name
        self.debug_log_file = logs_dir / debug_log_file_name
        self.timezone = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Shanghai'))
        self.logger = self._setup_logger(log_to_console)

    def _setup_logger(self, log_to_console: bool) -> logging.Logger:
        """Setup the logger with proper configuration."""
        logger = logging.getLogger(f"trading_bot_{self.exchange}_{self.ticker}")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if logger.handlers:
            return logger

        class TimeZoneFormatter(logging.Formatter):
            def __init__(self, fmt=None, datefmt=None, tz=None):
                super().__init__(fmt=fmt, datefmt=datefmt)
                self.tz = tz

            def formatTime(self, record, datefmt=None):
                dt = datetime.fromtimestamp(record.created, tz=self.tz)
                if datefmt:
                    return dt.strftime(datefmt)
                return dt.isoformat()

        formatter = TimeZoneFormatter(
            "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            tz=self.timezone
        )

        file_handler = logging.FileHandler(self.debug_log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        if log_to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        return logger

    def log(self, message: str, level: str = "INFO"):
        """Log a message with the specified level."""
        formatted_message = f"[{self.exchange.upper()}_{self.ticker.upper()}] {message}"
        level = level.upper()
        if level == "DEBUG":
            self.logger.debug(formatted_message)
        elif level == "INFO":
            self.logger.info(formatted_message)
        elif level == "WARNING":
            self.logger.warning(formatted_message)
        elif level == "ERROR":
            self.logger.error(formatted_message)
        else:
            self.logger.info(formatted_message)

    def log_transaction(self, order_id: str, side: str, quantity: Decimal, price: Decimal, status: str):
        """Log a transaction to CSV file."""
        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            row = [timestamp, order_id, side, quantity, price, status]

            file_exists = self.log_file.exists()

            with self.log_file.open('a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['Timestamp', 'OrderID', 'Side', 'Quantity', 'Price', 'Status'])
                writer.writerow(row)

        except Exception as err:
            self.log(f"Failed to log transaction: {err}", "ERROR")

