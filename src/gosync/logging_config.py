import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_FILE_NAME = "gosync.log"
LOGGER = logging.getLogger("gosync")


def configure_file_logging(data_dir: Path) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_file
        for handler in root_logger.handlers
    ):
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(file_handler)

    return log_file
