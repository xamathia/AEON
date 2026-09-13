"""Command-line entry point for the ÆON engine."""

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from .engine import simulate_day


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"JSON constant {value} is not allowed")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate an ÆON canonical day")
    parser.add_argument("payload", type=Path, help="path to a canonical JSON payload")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(
            args.payload.read_text(encoding="utf-8"),
            parse_constant=_invalid_json_constant,
        )
        result = simulate_day(payload)
        output = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
