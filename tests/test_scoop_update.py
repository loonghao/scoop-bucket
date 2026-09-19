"""Unit tests for scripts/scoop_update.py.

Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scoop_update as su  # noqa: E402

BUCKET = Path(__file__).resolve().parent.parent / "bucket"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "spec,expected",
    [
        ("loonghao/vx", ("loonghao", "vx")),
        ("https://github.com/loonghao/vx", ("loonghao", "vx")),
        ("https://github.com/loonghao/vx.git", ("loonghao", "vx")),
        ("git@github.com:loonghao/vx.git", ("loonghao", "vx")),
        ("https://www.github.com/loonghao/scoop-bucket/", ("loonghao", "scoop-bucket")),
    ],
)
def test_parse_repo(spec, expected):
    assert su.parse_repo(spec) == expected


def test_parse_repo_rejects_garbage():
    with pytest.raises(su.SweepError):
        su.parse_repo("not a repo at all")


def test_substitute():
    assert su.substitute("https://x/v$version/a-$version.zip", "1.2.3") == (
        "https://x/v1.2.3/a-1.2.3.zip"
    )
    assert su.substitute("$url.sha256", "1.2.3", "https://x/a.zip") == "https://x/a.zip.sha256"


def test_extract_version_strips_leading_v():
    assert su.extract_version("v0.9.32", None) == "0.9.32"
    assert su.extract_version("0.9.32", None) == "0.9.32"


def test_extract_version_numbered_group():
    assert su.extract_version("mcpcall-v0.4.1", r"^mcpcall-v([\d.]+)$") == "0.4.1"


def test_extract_version_named_group():
    assert su.extract_version("v2.0.0", r"^v(?P<version>[\d.]+)$") == "2.0.0"


def test_extract_version_regex_mismatch_raises():
    with pytest.raises(su.SweepError):
        su.extract_version("v1.0.0", r"^nope-(\d+)$")


def test_version_tuple_orders_numerically():
    assert su.version_tuple("0.9.32") > su.version_tuple("0.9.31")
    assert su.version_tuple("0.10.0") > su.version_tuple("0.9.32")


def test_checkver_repo_prefers_checkver_then_homepage():
    manifest = {"checkver": {"github": "https://github.com/o/r"}, "homepage": "https://github.com/h/p"}
    assert su.checkver_repo(manifest) == ("o", "r")
    assert su.checkver_repo({"homepage": "https://github.com/h/p"}) == ("h", "p")


def test_checkver_repo_without_source_raises():
    with pytest.raises(su.SweepError):
        su.checkver_repo({"version": "1.0.0"})


def test_resolve_hash_parses_checksum_file(monkeypatch):
    monkeypatch.setattr(
        su,
        "_request",
        lambda url: b"deadbeef" * 8 + b"  turbo-cdn-x86_64.zip\n",
    )
    got = su.resolve_hash(
        "https://x/a.zip",
        "1.0.0",
        {"url": "$url.sha256", "regex": r"^\s*([0-9a-f]{64})"},
    )
    assert got == ("deadbeef" * 8)


def test_resolve_hash_falls_back_to_download(monkeypatch):
    monkeypatch.setattr(su, "_request", lambda url: b"not a checksum file")
    monkeypatch.setattr(su, "download_sha256", lambda url: "f" * 64)
    assert su.resolve_hash("https://x/a.zip", "1.0.0", {"url": "$url.sha256"}) == "f" * 64


def test_apply_autoupdate_rewrites_version_url_and_hash(monkeypatch):
    monkeypatch.setattr(su, "download_sha256", lambda url: "a" * 64)
    manifest = {
        "version": "1.0.0",
        "architecture": {"64bit": {"url": "https://x/1.0.0/a.zip", "hash": "b" * 64}},
        "autoupdate": {"architecture": {"64bit": {"url": "https://x/$version/a.zip"}}},
    }
    updated, changes = su.apply_autoupdate(manifest, "2.0.0")
    assert updated["version"] == "2.0.0"
    assert updated["architecture"]["64bit"]["url"] == "https://x/2.0.0/a.zip"
    assert updated["architecture"]["64bit"]["hash"] == "a" * 64
    assert list(updated) == ["version", "architecture", "autoupdate"]  # key order kept
    assert len(changes) == 3


def test_apply_autoupdate_requires_autoupdate_block():
    with pytest.raises(su.SweepError):
        su.apply_autoupdate({"version": "1.0.0"}, "2.0.0")


def test_resolve_hash_surfaces_network_errors(monkeypatch):
    """Non-HTTP network failures must not escape as raw tracebacks.

    Patched at urlopen so the real download_sha256 wrapper is exercised, rather
    than replacing that wrapper itself.
    """
    import socket
    import urllib.error
    import urllib.request

    manifest = {
        "version": "1.0.0",
        "architecture": {"64bit": {"url": "https://x/1.0.0/a.zip", "hash": "b" * 64}},
        "autoupdate": {"architecture": {"64bit": {"url": "https://x/$version/a.zip"}}},
    }
    for exc in (
        urllib.error.URLError("dns failure"),
        socket.timeout("timed out"),
        TimeoutError("timed out"),
        OSError(104, "connection reset by peer"),
    ):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(exc)
        )
        with pytest.raises(su.SweepError):
            su.apply_autoupdate(manifest, "2.0.0")


def test_download_sha256_wraps_oserror(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise OSError(104, "connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(su.SweepError):
        su.download_sha256("https://x/a.zip")


# --------------------------------------------------------------------------- #
# the shipped manifests
# --------------------------------------------------------------------------- #
def _manifests():
    return sorted(BUCKET.glob("*.json"))


# Upstream release tag layout, when it is not simply "v{version}".
TAG_TEMPLATES = {
    "mcpcall": "mcpcall-v{version}",
}


def test_every_manifest_is_valid_json():
    assert _manifests(), "no manifests found"
    for path in _manifests():
        json.loads(path.read_text(encoding="utf-8"))  # raises on invalid JSON


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.stem)
def test_manifest_shape(path):
    m = json.loads(path.read_text(encoding="utf-8"))
    for field in ("version", "description", "homepage", "architecture", "bin"):
        assert field in m, f"{path.name} missing {field}"

    # A sweepable manifest must declare checkver + autoupdate, and every
    # architecture it pins must have a matching autoupdate URL template.
    assert "checkver" in m, f"{path.name} has no checkver"
    arch_auto = (m.get("autoupdate") or {}).get("architecture")
    assert arch_auto, f"{path.name} has no autoupdate.architecture"
    for arch in m["architecture"]:
        assert arch in arch_auto, f"{path.name}: {arch} pinned but not autoupdated"

    # Version must survive the upstream tag -> version transform, and the
    # autoupdate template must reproduce the pinned URL exactly.
    checkver = m["checkver"]
    regex = checkver.get("regex") if isinstance(checkver, dict) else None
    tag = TAG_TEMPLATES.get(path.stem, "v{version}").format(version=m["version"])
    assert su.extract_version(tag, regex) == m["version"]
    for arch, pinned in m["architecture"].items():
        expected = su.substitute(arch_auto[arch]["url"], m["version"])
        assert pinned["url"] == expected, f"{path.name}/{arch}: url does not match template"
        assert len(pinned["hash"]) == 64, f"{path.name}/{arch}: hash is not sha256"


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.stem)
def test_manifest_repo_is_reachable(path):
    """The checkver target must resolve to a real loonghao repository."""
    m = json.loads(path.read_text(encoding="utf-8"))
    owner, repo = su.checkver_repo(m)
    assert owner == "loonghao"
    assert repo
