from gosync import __version__
from gosync.config import DEBUG, DISPLAY_WEB_PORT, WEB_HOST, WEB_PORT, parse_args
from gosync.events import log_event
from gosync.logging_config import LOGGER, configure_console_logging
from gosync.runtime import run_once
from gosync.web import create_app


def main() -> int:
    args = parse_args()
    if args.run_once:
        return run_once(args)

    app = create_app(args)
    configure_console_logging()
    log_event(
        "app.starting",
        f"GoSync {__version__} starting",
        version=__version__,
        debug=DEBUG,
        cli_message=f"GoSync {__version__} starting",
    )
    log_event(
        "app.ready",
        "GoSync web UI ready",
        source=f"http://localhost:{DISPLAY_WEB_PORT}",
        cli_message=f"Open http://localhost:{DISPLAY_WEB_PORT}",
    )
    log_event(
        "web.server.started",
        "Flask server starting",
        source=f"{WEB_HOST}:{WEB_PORT}",
        debug=DEBUG,
        cli_message=f"Debug mode: {'enabled' if DEBUG else 'disabled'}",
    )
    LOGGER.info(
        "Starting Flask server. host=%s port=%s debug=%s",
        WEB_HOST,
        WEB_PORT,
        DEBUG,
    )
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, debug=DEBUG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
