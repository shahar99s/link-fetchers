import argparse
import sys

from link_fetchers.fetcher_registry import create_fetcher
from link_fetchers.utils import Mode

_MODE_MAP = {
    "info": Mode.INFO,
    "fetch": Mode.FETCH,
    "force-fetch": Mode.FORCE_FETCH,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="link-fetchers",
        description="Fetch file metadata and download links from file-sharing services.",
    )
    parser.add_argument("url", help="File-sharing URL to process")
    parser.add_argument(
        "--mode",
        choices=list(_MODE_MAP),
        default="fetch",
        metavar="MODE",
        help="Operation mode: info, fetch, force-fetch (default: fetch)",
    )
    parser.add_argument(
        "--save-path",
        metavar="DIR",
        default=None,
        help="Directory to save downloaded files (default: current directory)",
    )
    parser.add_argument(
        "--log-details",
        action="store_true",
        help="Enable verbose HTTP request/response logging",
    )
    parser.add_argument(
        "--retry-times",
        type=int,
        default=3,
        metavar="N",
        help="Number of retry attempts for transient errors (default: 3)",
    )
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=2.0,
        metavar="SECS",
        help="Seconds to wait between retries (default: 2.0)",
    )
    parser.add_argument(
        "--opt",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help=(
            "Provider-specific option, may be repeated "
            "(e.g. --opt access_token=TOKEN --opt password=secret)"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    extra: dict = {}
    for pair in args.opt:
        if "=" not in pair:
            print(f"Error: --opt must be KEY=VALUE, got: {pair!r}", file=sys.stderr)
            sys.exit(1)
        key, value = pair.split("=", 1)
        extra[key.strip()] = value.strip()

    try:
        fetcher = create_fetcher(
            args.url,
            mode=_MODE_MAP[args.mode],
            log_details=args.log_details,
            save_path=args.save_path,
            retry_times=args.retry_times,
            retry_interval=args.retry_interval,
            **extra,
        )
        fetcher.run()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
