import logging
import os


def get_logger(log_file: str, name: str = "crawler") -> logging.Logger:
    """Create and return a logger that writes to both a file and stdout."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


def close_logger(logger: logging.Logger) -> None:
    """Flush, detach, and close all handlers for a logger."""
    handlers = list(logger.handlers)
    for handler in handlers:
        try:
            handler.flush()
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)
