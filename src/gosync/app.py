from gosync import __version__
from gosync.config import DEBUG

from gosync.config import WEB_HOST, WEB_PORT, parse_args
from gosync.logging_config import LOGGER
from gosync.runtime import run_once
from gosync.web import create_app


def main() -> int:
    args = parse_args()
    if args.run_once:
        return run_once(args)

    app = create_app(args)
    print("========================================", flush=True)
    print(f"              GoSync {__version__}              ", flush=True)
    print("========================================", flush=True)
    print(f"Open http://localhost:{WEB_PORT}", flush=True)
    print(f"Debug mode: {'enabled' if DEBUG else 'disabled'}", flush=True)
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
