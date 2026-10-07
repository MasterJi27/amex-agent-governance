from __future__ import annotations

from gateway.hashchain import GENESIS, canonical_json, link_hash, verify_chain


def test_canonical_json_is_stable() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_chain_accepts_linked_rows_and_names_the_tampered_one() -> None:
    first = canonical_json({"amount_cents": 18000, "decision": "allow"})
    second = canonical_json({"amount_cents": 0, "decision": "allow"})
    first_hash = link_hash(GENESIS, first)
    second_hash = link_hash(first_hash, second)
    rows = [
        {"seq": 1, "prev_hash": GENESIS, "hash": first_hash, "payload": first},
        {"seq": 2, "prev_hash": first_hash, "hash": second_hash, "payload": second},
    ]
    assert verify_chain(rows) is None
    rows[1]["payload"] = second.replace("0", "1", 1)
    assert verify_chain(rows) == 2
