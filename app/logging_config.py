"""
Enhanced Logging Configuration

Provides structured logging with:
- Category-based log files
- Daily rotation with size limits
- Automatic cleanup of old logs
- Optional JSON formatting
"""

import logging
import os
import json
import glob
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from app.config import settings


# Log categories and their corresponding directories
LOG_CATEGORIES = {
    'app': 'app',           # General application logs
    'auth': 'auth',         # Authentication events
    'bots': 'bots',         # Bot lifecycle
    'conversations': 'conversations',  # Message handling
    'ai': 'ai',             # AI provider calls
    'hubs': 'hubs',         # Hub orchestration
    'tools': 'tools',       # Tool execution
    'scheduler': 'scheduler',  # Background tasks
    'errors': 'errors',     # All errors (aggregated)
}

# Base logs directory (relative to project root for cross-OS consistency)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_BASE_DIR = _PROJECT_ROOT / "logs"


class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)

        # Add extra fields if present
        if hasattr(record, 'extra_data'):
            log_data['extra'] = record.extra_data

        return json.dumps(log_data)


class DailyRotatingFileHandler(TimedRotatingFileHandler):
    """
    Combines daily rotation with size-based rotation.
    Rotates daily AND when file exceeds max size.
    """

    def __init__(
        self,
        filename: str,
        max_bytes: int = 10 * 1024 * 1024,  # 10MB default
        backup_count: int = 30,
        encoding: str = 'utf-8'
    ):
        # Create directory if needed
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        super().__init__(
            filename,
            when='midnight',
            interval=1,
            backupCount=backup_count,
            encoding=encoding
        )
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        """Check if rollover should occur (time-based or size-based)."""
        # Check time-based rollover first
        if super().shouldRollover(record):
            return True

        # Check size-based rollover
        if self.stream is None:
            self.stream = self._open()

        if self.max_bytes > 0:
            self.stream.seek(0, 2)  # Seek to end
            if self.stream.tell() + len(self.format(record)) >= self.max_bytes:
                return True

        return False


class CategoryLogger:
    """Logger for a specific category with automatic file routing."""

    def __init__(self, category: str, use_json: bool = False):
        self.category = category
        self.logger = logging.getLogger(f'chathub.{category}')
        self.use_json = use_json

    def _log(self, level: int, msg: str, *args, **kwargs):
        extra_data = kwargs.pop('extra_data', None)
        if extra_data:
            # Create a LogRecord with extra data
            record = self.logger.makeRecord(
                self.logger.name, level, '', 0, msg, args, None
            )
            record.extra_data = extra_data
            self.logger.handle(record)
        else:
            self.logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)


def get_category_logger(category: str) -> CategoryLogger:
    """Get a logger for a specific category."""
    if category not in LOG_CATEGORIES:
        category = 'app'  # Default to app category
    return CategoryLogger(category, use_json=settings.LOG_JSON_FORMAT)


def cleanup_old_logs(max_age_days: Optional[int] = None) -> int:
    """
    Remove log files older than max_age_days.

    Args:
        max_age_days: Maximum age in days. If None, uses LOG_MAX_AGE_DAYS setting.

    Returns:
        Number of files deleted.
    """
    if max_age_days is None:
        max_age_days = settings.LOG_MAX_AGE_DAYS

    if max_age_days <= 0:
        return 0  # Cleanup disabled

    cutoff_date = datetime.now() - timedelta(days=max_age_days)
    deleted_count = 0

    for category_dir in LOGS_BASE_DIR.iterdir():
        if category_dir.is_dir():
            for log_file in category_dir.glob('*.log*'):
                try:
                    file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                    if file_mtime < cutoff_date:
                        log_file.unlink()
                        deleted_count += 1
                except Exception as e:
                    print(f"Error deleting old log file {log_file}: {e}")

    return deleted_count


def setup_logging() -> None:
    """
    Set up the enhanced logging system.

    Creates category directories and configures handlers for each category.
    """
    # Get settings
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    max_bytes = settings.LOG_MAX_SIZE_MB * 1024 * 1024
    backup_count = settings.LOG_BACKUP_COUNT
    use_json = settings.LOG_JSON_FORMAT

    # Create base logs directory
    LOGS_BASE_DIR.mkdir(exist_ok=True)

    # Create category directories and set up handlers
    for category, dir_name in LOG_CATEGORIES.items():
        category_dir = LOGS_BASE_DIR / dir_name
        category_dir.mkdir(exist_ok=True)

        log_file = category_dir / f'{category}.log'

        # Create logger for this category
        logger = logging.getLogger(f'chathub.{category}')
        logger.setLevel(log_level)
        logger.propagate = False  # Don't propagate to root logger

        # Create handler with rotation
        handler = DailyRotatingFileHandler(
            str(log_file),
            max_bytes=max_bytes,
            backup_count=backup_count
        )
        handler.setLevel(log_level)

        # Set formatter
        if use_json:
            formatter = JsonFormatter()
        else:
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
        handler.setFormatter(formatter)

        # Add handler to logger
        logger.addHandler(handler)

        # Also add error handler to errors category for ERROR and above
        if category != 'errors':
            errors_log = LOGS_BASE_DIR / 'errors' / 'errors.log'
            error_handler = DailyRotatingFileHandler(
                str(errors_log),
                max_bytes=max_bytes,
                backup_count=backup_count
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(formatter)
            logger.addHandler(error_handler)

    # Configure root logger for console output
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_formatter)

    # Clear existing handlers and add console
    root_logger.handlers = []
    root_logger.addHandler(console_handler)

    # Also add a general app log file
    app_log = LOGS_BASE_DIR / 'app' / 'app.log'
    app_handler = DailyRotatingFileHandler(
        str(app_log),
        max_bytes=max_bytes,
        backup_count=backup_count
    )
    app_handler.setLevel(log_level)
    if use_json:
        app_handler.setFormatter(JsonFormatter())
    else:
        app_handler.setFormatter(console_formatter)
    root_logger.addHandler(app_handler)

    # Run cleanup on startup
    try:
        deleted = cleanup_old_logs()
        if deleted > 0:
            logging.info(f"Cleaned up {deleted} old log files")
    except Exception as e:
        logging.warning(f"Log cleanup failed: {e}")

    logging.info("Enhanced logging system initialized")


# Convenience loggers for direct import
auth_logger = get_category_logger('auth')
bots_logger = get_category_logger('bots')
conversations_logger = get_category_logger('conversations')
ai_logger = get_category_logger('ai')
hubs_logger = get_category_logger('hubs')
tools_logger = get_category_logger('tools')
scheduler_logger = get_category_logger('scheduler')
app_logger = get_category_logger('app')
