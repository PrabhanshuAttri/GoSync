from gosync.config import WEB_HOST, WEB_PORT, parse_args
from gosync.runtime import run_once
from gosync.web import create_app


def main() -> int:
    args = parse_args()
    if args.run_once:
        return run_once(args)

    app = create_app(args)
    print("========================================", flush=True)
    print("             GoSync Web UI              ", flush=True)
    print("========================================", flush=True)
    print(f"Open http://localhost:{WEB_PORT}", flush=True)
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
