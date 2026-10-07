from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def link_hash(prev_hash: str, payload_text: str) -> str:
    return hashlib.sha256((prev_hash + payload_text).encode("utf-8")).hexdigest()


def verify_chain(rows: list[dict[str, Any]]) -> int | None:
    """Walk seq order. Return the first broken seq, or None when the chain holds."""
    prev = GENESIS
    for row in rows:
        payload_text = row["payload"]
        expected = link_hash(prev, payload_text)
        if row["prev_hash"] != prev or row["hash"] != expected:
            return int(row["seq"])
        prev = row["hash"]
    return None
