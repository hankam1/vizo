"""Centralised logging: rotating file in USER_DATA_DIR/logs + global excepthooks."""
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from logging import Logger

from config import USER_DATA_DIR

LOG_DIR = os.path.join(USER_DATA_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "vizo.log")

_initialised = False


def setup() -> Logger:
    """Configure the root logger. Safe to call multiple times."""
    global _initialised
    if _initialised:
        return logging.getLogger("vizo")

    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("vizo.uncaught").critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args):
        logging.getLogger("vizo.thread").critical(
            "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    _initialised = True
    log = logging.getLogger("vizo")
    log.info("Logger initialised. File: %s", LOG_FILE)
    return log


def get(name: str = "vizo") -> Logger:
    return logging.getLogger(name)
