"""统一日志初始化。

所有模块通过 ``from logger import get_logger`` 获取 logger，
进程入口调用一次 :func:`setup_logging`。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_FILE = "app.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 5


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger，未初始化时以默认配置工作。"""
    return logging.getLogger(name)


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    console: bool = True,
) -> logging.Logger:
    """初始化根 logger：stdout + 滚动文件双通道。

    参数：
        level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）。
        log_dir: 日志目录，默认 ``logs``，自动创建。
        console: 是否同时输出到 stdout。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 幂等：清空已注册 handler，避免重复初始化
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path / _LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    root.info(
        "日志初始化完成 level=%s file=%s console=%s",
        level,
        log_path / _LOG_FILE,
        console,
    )
    return root
