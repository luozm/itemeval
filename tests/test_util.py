from pathlib import Path

from itemeval._util import (
    atomic_write_bytes,
    canonical_json,
    drop_none,
    estimate_tokens,
    sha256_hex,
)


def test_canonical_json_is_order_insensitive():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})
    assert canonical_json({"a": 1}) == '{"a":1}'


def test_canonical_json_preserves_unicode():
    assert canonical_json({"s": "héllo"}) == '{"s":"héllo"}'


def test_sha256_hex():
    assert sha256_hex(b"") == ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_drop_none():
    assert drop_none({"a": 1, "b": None, "c": 0}) == {"a": 1, "c": 0}


def test_atomic_write_bytes(tmp_path: Path):
    target = tmp_path / "sub" / "file.json"
    atomic_write_bytes(target, b"data")
    assert target.read_bytes() == b"data"
    assert not target.with_name("file.json.tmp").exists()
    atomic_write_bytes(target, b"data2")
    assert target.read_bytes() == b"data2"
