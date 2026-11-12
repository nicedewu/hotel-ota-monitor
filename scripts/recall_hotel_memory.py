from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.memory_provider import recall_hotel  # noqa: E402


# Manual diagnostic entry point; it is intentionally not scheduled.
def main() -> None:
    parser = argparse.ArgumentParser(description="只读检索酒店 Mnemosyne 经验")
    parser.add_argument("--hotel-key", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    rows = recall_hotel(args.hotel_key, args.query, max(1, min(args.limit, 20)))
    print(json.dumps(rows, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
