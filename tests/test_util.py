import os
import stat

import pytest

from acctsw.util import atomic_write_text, read_text, read_text_lossless, write_json, jwt_payload, parse_iso, iso, now


def test_text_helpers_keep_owned_files_strict_and_foreign_bytes_reversible(tmp_path):
    p = tmp_path / "text"
    atomic_write_text(p, "café — UTF-8")
    assert read_text(p) == "café — UTF-8"
    original = b"# caf\xe9\r\nexport EDITOR=vi\r"
    p.write_bytes(original)
    with pytest.raises(UnicodeDecodeError):
        read_text(p)
    foreign = read_text_lossless(p)
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(p, foreign)
    assert p.read_bytes() == original
    atomic_write_text(p, foreign, errors="surrogateescape")
    assert p.read_bytes() == original


def test_atomic_write_creates_file_with_mode(tmp_path):
    p = tmp_path / "sub" / "secret.txt"
    atomic_write_text(p, "hello", mode=0o600)
    assert p.read_text() == "hello"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_atomic_write_overwrites_atomically(tmp_path):
    p = tmp_path / "f.json"
    write_json(p, {"a": 1})
    write_json(p, {"a": 2})
    assert p.read_text().strip().endswith("}")
    import json
    assert json.loads(p.read_text())["a"] == 2
    # no leftover temp files in the dir
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]


def test_jwt_payload_decodes_email():
    import base64, json
    payload = base64.urlsafe_b64encode(json.dumps({"email": "a@b.com"}).encode()).decode().rstrip("=")
    assert jwt_payload(f"h.{payload}.s")["email"] == "a@b.com"


def test_jwt_payload_bad_input():
    assert jwt_payload("not-a-jwt") == {}
    assert jwt_payload("") == {}


def test_iso_roundtrip():
    dt = now()
    assert parse_iso(iso(dt)) == dt
    assert parse_iso(None) is None
    assert parse_iso("garbage") is None
