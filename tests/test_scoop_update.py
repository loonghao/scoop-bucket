"""Unit tests for scripts/scoop_update.py.

Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import argparse
import json
import subprocess
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


# --------------------------------------------------------------------------- #
# the write path (sweep -> branch -> write -> commit -> push -> PR)
# --------------------------------------------------------------------------- #
# These exercise sweep() end to end against a throwaway git repository with a
# local bare remote. They exist because every other check is structurally
# incapable of covering the write path: --dry-run never writes by design, the
# unit tests above call apply_autoupdate() (which returns a dict) and never
# sweep(), and --check-hashes is read-only. A sweep that computes the new
# version and hash and then silently writes nothing still exits 0, so only an
# assertion that reads the file back from disk can catch it.

SWEEP_MANIFEST = {
    "version": "0.9.31",
    "description": "fixture app",
    "homepage": "https://github.com/loonghao/vx",
    "license": "MIT",
    "checkver": {"github": "loonghao/vx"},
    "architecture": {"64bit": {"url": "https://example.test/$version/a.zip", "hash": "b" * 64}},
    "autoupdate": {"architecture": {"64bit": {"url": "https://example.test/$version/a.zip"}}},
    "bin": "vx.exe",
}

SWEEP_BRANCH = "scoop-autoupdate/vx"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _sweep_args(**overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        dry_run=False,
        only="",
        push=True,
        allow_downgrade=False,
        check_hashes=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


# Identity used only to build the fixture's own baseline commit. Deliberately
# NOT written into the repo config, so the fixture mirrors a CI runner: no
# identity anywhere, which is exactly the condition that made `git commit` exit
# 128 ("Author identity unknown") on GitHub Actions.
FIXTURE_IDENTITY = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.com")

def _isolate_git_identity(monkeypatch, tmp_path: Path) -> None:
    """Point git at an empty identity world.

    Without this the developer's own ~/.gitconfig satisfies `git commit` and a
    missing-identity regression passes locally while failing on the runner.
    """
    empty_home = tmp_path / "no-identity-home"
    empty_home.mkdir(exist_ok=True)
    missing = tmp_path / "definitely-absent-gitconfig"
    for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(empty_home))
    for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(var, str(missing))
    # A committed identity would survive in the environment too.
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def bucket_repo(tmp_path, monkeypatch):
    """A throwaway repo with a local bare remote, pre-loaded with vx.json.

    The repo is intentionally left with NO configured identity (neither local
    nor global), matching a CI runner. sweep() must therefore supply its own.
    """
    _isolate_git_identity(monkeypatch, tmp_path)

    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin), cwd=tmp_path)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)

    bucket = repo / "bucket"
    bucket.mkdir()
    (bucket / "vx.json").write_text(json.dumps(SWEEP_MANIFEST, indent=4) + "\n", encoding="utf-8")
    _git("add", "bucket", cwd=repo)
    _git(*FIXTURE_IDENTITY, "commit", "-q", "-m", "initial", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)

    # sweep() runs git in the process cwd and reads manifests from BUCKET_DIR.
    monkeypatch.chdir(repo)
    monkeypatch.setattr(su, "BUCKET_DIR", bucket)
    # Only the network and `gh` are stubbed; every git call is real.
    monkeypatch.setattr(su, "latest_version", lambda manifest: "0.9.32")
    monkeypatch.setattr(su, "download_sha256", lambda url: "a" * 64)
    monkeypatch.setattr(su, "existing_pr", lambda branch: None)
    return repo


def test_sweep_writes_manifest_to_disk(bucket_repo, monkeypatch):
    """The regression that motivated these tests: sweep must persist updates.

    An earlier revision deferred the write to "when the branch is prepared" and
    never implemented it, so the sweep reported an update, wrote nothing, and
    exited 0. Assert on the file on disk, not on the returned dict.
    """
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(su, "open_pr", lambda branch, title, body: opened.append((branch, title)))

    assert su.sweep(_sweep_args(push=True)) == 0

    on_disk = json.loads((bucket_repo / "bucket" / "vx.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == "0.9.32"
    assert on_disk["architecture"]["64bit"]["url"] == "https://example.test/0.9.32/a.zip"
    assert on_disk["architecture"]["64bit"]["hash"] == "a" * 64
    assert opened == [(SWEEP_BRANCH, "chore(scoop): update vx to 0.9.32")]


def test_sweep_commits_the_rewritten_manifest(bucket_repo, monkeypatch):
    """The commit -- not just the working tree -- must carry the new version."""
    monkeypatch.setattr(su, "open_pr", lambda branch, title, body: None)

    assert su.sweep(_sweep_args(push=True)) == 0

    committed = json.loads(_git("show", f"{SWEEP_BRANCH}:bucket/vx.json", cwd=bucket_repo).stdout)
    assert committed["version"] == "0.9.32"

    # The branch was really pushed, so a PR can be opened against it.
    remote = _git("ls-remote", "--heads", "origin", f"refs/heads/{SWEEP_BRANCH}", cwd=bucket_repo)
    assert SWEEP_BRANCH in remote.stdout


def test_sweep_commits_with_its_own_identity(bucket_repo, monkeypatch):
    """sweep() must commit on a machine that has no git identity at all.

    CI runners have neither a global nor a local user.name/user.email, so a bare
    `git commit` exits 128 ("Author identity unknown") there while passing on any
    developer machine. The fixture strips every identity source, so this test
    only passes if the commit supplies its own.
    """
    monkeypatch.setattr(su, "open_pr", lambda branch, title, body: None)

    assert su.sweep(_sweep_args(push=True)) == 0

    name = _git("log", "-1", "--format=%an", cwd=bucket_repo).stdout.strip()
    email = _git("log", "-1", "--format=%ae", cwd=bucket_repo).stdout.strip()
    assert (name, email) == ("loonghao", "hal.long@outlook.com")

    # The identity must be passed per-invocation, not persisted into the repo.
    local_identity = subprocess.run(
        ["git", "config", "--local", "--get-regexp", "^user\\."],
        cwd=bucket_repo, capture_output=True, text=True, check=False,
    )
    assert local_identity.returncode != 0, "sweep must not write identity into repo config"


def test_sweep_dry_run_writes_nothing(bucket_repo, monkeypatch):
    monkeypatch.setattr(su, "open_pr", lambda branch, title, body: None)

    assert su.sweep(_sweep_args(dry_run=True, push=True)) == 0

    on_disk = json.loads((bucket_repo / "bucket" / "vx.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == "0.9.31"
    assert SWEEP_BRANCH not in _git("branch", "--list", cwd=bucket_repo).stdout


def test_sweep_without_updates_does_not_commit(bucket_repo, monkeypatch):
    monkeypatch.setattr(su, "open_pr", lambda branch, title, body: None)
    monkeypatch.setattr(su, "latest_version", lambda manifest: "0.9.31")  # already pinned

    assert su.sweep(_sweep_args(push=True)) == 0

    assert SWEEP_BRANCH not in _git("branch", "--list", cwd=bucket_repo).stdout


def test_open_pr_reports_existing_pr_without_creating_one(monkeypatch):
    monkeypatch.setattr(su, "existing_pr", lambda branch: "https://github.com/o/r/pull/1")

    calls = []

    class _Result:
        returncode = 0
        stdout = "https://github.com/o/r/pull/2"
        stderr = ""

    def _fake_run(*args, **kwargs):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(su.subprocess, "run", _fake_run)
    su.open_pr("branch", "title", "body")
    assert not calls, "gh pr create must not run when a PR already exists"


def test_open_pr_raises_when_gh_create_fails(monkeypatch):
    monkeypatch.setattr(su, "existing_pr", lambda branch: None)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "gh: not logged in"

    monkeypatch.setattr(su.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(su.SweepError, match="gh pr create failed"):
        su.open_pr("branch", "title", "body")
