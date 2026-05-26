import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
CONSOLE_FORMAT = "%(message)s"
LOG_FILE_NAME = "gosync.log"
LOGGER = logging.getLogger("gosync")


class GoSyncFileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        event = getattr(record, "gosync_event", None)
        if not isinstance(event, dict):
            return message

        context = []
        if event.get("event"):
            context.append(f"event={event['event']}")
        if event.get("group"):
            context.append(f"group={event['group']}")
        if event.get("run_id"):
            context.append(f"run_id={event['run_id']}")
        return f"{message} ({' '.join(context)})" if context else message


def configure_console_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(handler, logging.StreamHandler)
        and getattr(handler, "_gosync_console", False)
        for handler in root_logger.handlers
    ):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        console_handler._gosync_console = True
        root_logger.addHandler(console_handler)


def configure_file_logging(data_dir: Path) -> Path:
    configure_console_logging()
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
        file_handler.setFormatter(GoSyncFileFormatter(LOG_FORMAT))
        root_logger.addHandler(file_handler)

    return log_file
