#!/usr/bin/env python3
"""Central Scoop autoupdate sweep for this bucket.

For every manifest under ``bucket/`` that declares ``checkver`` and
``autoupdate``, query the upstream GitHub repository for its latest release and,
when the version differs from the pinned one, rewrite ``version`` plus the
per-architecture ``url``/``hash`` pairs.

Why a polling sweep instead of a push on release
------------------------------------------------
``release: published`` is not emitted when a release is created by
release-please or made public via ``gh release edit --draft=false``. A sweep
that polls the releases API on a schedule never depends on that event, needs no
cross-repository PAT, and keeps every bucket update reviewable as a pull
request.

Stdlib only -- no third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BUCKET_DIR = Path(__file__).resolve().parent.parent / "bucket"
API_ROOT = "https://api.github.com"
CHUNK = 1 << 20

# Accepts "owner/repo", a github.com URL, or an SSH remote.
_REPO_RE = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)?"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class SweepError(RuntimeError):
    """Fatal error for a single manifest."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _request(url: str) -> bytes:
    headers = {
        "User-Agent": "loonghao-scoop-bucket-autoupdate",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        raise SweepError(f"GET {url} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        # HTTPError is a URLError subclass and is handled above; this covers DNS
        # failures, refused/reset connections and timeouts, which would otherwise
        # escape sweep() and abort the whole run mid-way.
        raise SweepError(f"GET {url} -> {type(exc).__name__}: {exc}") from exc


def parse_repo(spec: str) -> tuple[str, str]:
    """Extract ``(owner, repo)`` from a repo reference or GitHub URL."""
    match = _REPO_RE.match(spec.strip())
    if not match:
        raise SweepError(f"cannot parse GitHub repository from {spec!r}")
    return match.group("owner"), match.group("repo")


def substitute(template: str, version: str, asset_url: str | None = None) -> str:
    """Expand ``$version`` (and ``$url`` when *asset_url* is given)."""
    out = template.replace("$version", version)
    if asset_url is not None:
        out = out.replace("$url", asset_url)
    return out


def checkver_repo(manifest: dict[str, Any]) -> tuple[str, str]:
    """Resolve the upstream repository from ``checkver`` or ``homepage``."""
    checkver = manifest.get("checkver")
    source = None
    if isinstance(checkver, dict) and checkver.get("github"):
        source = checkver["github"]
    elif isinstance(checkver, str) and checkver != "github":
        source = checkver
    elif manifest.get("homepage"):
        source = manifest["homepage"]
    if not source:
        raise SweepError("no checkver source and no homepage to derive one from")
    return parse_repo(str(source))


def extract_version(tag: str, regex: str | None) -> str:
    """Pull the manifest version out of a release tag.

    Mirrors Scoop's own semantics: a named ``version`` group wins, otherwise the
    first capture group is used. Without a regex only a leading ``v`` is stripped.
    """
    if regex:
        match = re.search(regex, tag)
        if not match:
            raise SweepError(f"checkver regex {regex!r} does not match tag {tag!r}")
        groups = match.groupdict()
        return groups.get("version") or match.group(1)
    return tag[1:] if tag.startswith("v") else tag


def version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", version):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def latest_version(manifest: dict[str, Any]) -> str:
    owner, repo = checkver_repo(manifest)
    payload = json.loads(_request(f"{API_ROOT}/repos/{owner}/{repo}/releases/latest"))
    tag = payload.get("tag_name")
    if not tag:
        raise SweepError(f"{owner}/{repo} has no latest release")
    regex = manifest.get("checkver", {}).get("regex") if isinstance(manifest.get("checkver"), dict) else None
    return extract_version(str(tag), regex)


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #
def download_sha256(url: str) -> str:
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "loonghao-scoop-bucket-autoupdate"}),
            timeout=600,
        ) as resp:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                digest.update(block)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise SweepError(f"download {url} -> {type(exc).__name__}: {exc}") from exc
    return digest.hexdigest()


def resolve_hash(asset_url: str, version: str, hash_spec: Any) -> str:
    """Compute the SHA-256 for *asset_url*.

    ``hash_spec`` may be a dict with a ``url`` (typically ``$url.sha256``) and an
    optional ``regex``; in that case the checksum is parsed out of that file.
    Anything unparseable falls back to downloading the asset itself, which is
    also the default when no ``hash`` is declared (Scoop's own default).
    """
    if isinstance(hash_spec, dict) and hash_spec.get("url"):
        checksum_url = substitute(hash_spec["url"], version, asset_url)
        try:
            text = _request(checksum_url).decode("utf-8", "replace")
        except SweepError:
            text = ""
        if text:
            regex = hash_spec.get("regex")
            if regex:
                match = re.search(regex, text)
                if match:
                    groups = match.groupdict()
                    candidate = groups.get("sha256") or groups.get("version") or match.group(1)
                else:
                    candidate = ""
            else:
                candidate = text.split()[0] if text.split() else ""
            candidate = str(candidate).strip().lower()
            if HEX64_RE.match(candidate):
                return candidate
            print(f"    ! checksum file {checksum_url} did not yield a sha256, downloading asset")
    return download_sha256(asset_url)


# --------------------------------------------------------------------------- #
# manifest update
# --------------------------------------------------------------------------- #
def apply_autoupdate(manifest: dict[str, Any], version: str) -> tuple[dict[str, Any], list[str]]:
    """Return an updated copy of *manifest* pinned to *version*, plus changes."""
    autoupdate = manifest.get("autoupdate") or {}
    arch_templates = autoupdate.get("architecture")
    if not arch_templates:
        raise SweepError("manifest has no autoupdate.architecture block")

    updated = json.loads(json.dumps(manifest))  # deep copy, preserves key order
    changes: list[str] = []

    for arch, spec in arch_templates.items():
        template = spec.get("url") if isinstance(spec, dict) else None
        if not template:
            continue
        url = substitute(template, version)
        digest = resolve_hash(url, version, spec.get("hash"))
        target = updated.setdefault("architecture", {}).setdefault(arch, {})
        if target.get("url") != url:
            changes.append(f"{arch}: url -> {url}")
            target["url"] = url
        if target.get("hash") != digest:
            changes.append(f"{arch}: hash -> {digest}")
            target["hash"] = digest

    if updated.get("version") != version:
        changes.append(f"version: {updated.get('version')} -> {version}")
        updated["version"] = version

    return updated, changes


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# git / PR plumbing
# --------------------------------------------------------------------------- #
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def commit_changes(branch: str, message: str) -> bool:
    git("checkout", "-B", branch)
    git("add", "bucket")
    # `git diff --cached --quiet` exits 0 = nothing staged, 1 = staged changes,
    # so this probe must not raise on a non-zero status.
    staged = git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("  nothing staged; no commit created")
        return False
    git("commit", "-m", message)
    return True


def existing_pr(branch: str) -> str | None:
    """Return the URL of the open PR for *branch*, or None.

    Queried through the API rather than from local git state, so it works on a
    fresh checkout and can be called before anything is written.
    """
    found = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip() or None


def push_branch(branch: str) -> None:
    """Push *branch*, refreshing it when it already exists on the remote.

    An autoupdate branch is owned by this workflow, so replacing its content is
    safe. --force-with-lease is used instead of a bare force so a concurrent
    push by anyone else is still refused.
    """
    remote_has_branch = bool(git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", check=False).stdout.strip())
    if remote_has_branch:
        git("fetch", "origin", branch, check=False)
        pushed = git("push", "--force-with-lease", "origin", branch, check=False)
    else:
        pushed = git("push", "-u", "origin", branch, check=False)
    if pushed.returncode != 0:
        raise SweepError(f"push failed: {pushed.stderr.strip()}")


def open_pr(branch: str, title: str, body: str) -> None:
    already = existing_pr(branch)
    if already:
        print(f"  existing pull request refreshed: {already}")
        return
    result = subprocess.run(
        ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title, "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Surface the failure: a pushed branch with no PR is invisible, and a
        # green job here would hide that.
        raise SweepError(f"gh pr create failed: {result.stderr.strip()}")
    print(f"  opened pull request: {result.stdout.strip()}")


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #
def check_hashes(args: argparse.Namespace) -> int:
    """Re-download every pinned asset and confirm the pinned hash still matches.

    The normal sweep only computes hashes when a version actually changes, so a
    hand-written manifest with a wrong hash would otherwise pass every gate and
    only fail at `scoop install` time.
    """
    manifests = sorted(BUCKET_DIR.glob("*.json"))
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        manifests = [p for p in manifests if p.stem in wanted]

    failed = 0
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for arch, spec in sorted((manifest.get("architecture") or {}).items()):
            url, pinned = spec.get("url"), spec.get("hash")
            if not url or not pinned:
                continue
            actual = download_sha256(url)
            status = "ok" if actual == pinned else "MISMATCH"
            if actual != pinned:
                failed += 1
            print(f"[{path.stem}/{arch}] {status}  {url}\n    pinned={pinned}\n    actual={actual}")

    print(f"\nhash check: {len(manifests)} manifests, {failed} mismatch(es)")
    return 1 if failed else 0


def sweep(args: argparse.Namespace) -> int:
    manifests = sorted(BUCKET_DIR.glob("*.json"))
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        manifests = [path for path in manifests if path.stem in wanted]
        missing = wanted - {path.stem for path in manifests}
        if missing:
            print(f"warning: no manifest for {', '.join(sorted(missing))}")
    if not manifests:
        print("no manifests selected")
        return 0

    pending: list[tuple[Path, dict[str, Any], list[str]]] = []

    for path in manifests:
        print(f"[{path.stem}]")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  SKIP invalid JSON: {exc}")
            continue
        if not manifest.get("checkver") or not (manifest.get("autoupdate") or {}).get("architecture"):
            print("  SKIP no checkver/autoupdate block")
            continue

        try:
            version = latest_version(manifest)
        except SweepError as exc:
            print(f"  ERROR {exc}")
            continue

        current = manifest.get("version")
        if version == current:
            print(f"  up to date ({version})")
            continue
        if not args.allow_downgrade and version_tuple(version) < version_tuple(str(current)):
            print(f"  SKIP upstream {version} is older than pinned {current} (use --allow-downgrade)")
            continue

        print(f"  update available: {current} -> {version}")
        try:
            updated, changes = apply_autoupdate(manifest, version)
        except SweepError as exc:
            print(f"  ERROR {exc}")
            continue
        if not changes:
            print("  no field changes after substitution")
            continue
        for change in changes:
            print(f"    - {change}")

        if args.dry_run:
            print("  dry-run: manifest not written")
            continue

        # Writing is deferred until the branch is prepared, so that switching
        # branches cannot clash with metadata already modified on disk.
        pending.append((path, updated, changes))

    if args.dry_run or not pending:
        print("\nsweep complete (no files changed)")
        return 0

    names = sorted(path.stem for path, _, _ in pending)
    versions = ", ".join(
        f"{path.stem} {manifest['version']}" for path, manifest, _ in pending
    )
    branch = f"scoop-autoupdate/{'-'.join(names)}"[:120]
    title = f"chore(scoop): update {', '.join(names)} to {pending[0][1]['version']}" if len(names) == 1 else (
        f"chore(scoop): update {len(names)} manifests"
    )
    body = "\n".join(
        [
            "Automated Scoop manifest update.",
            "",
            f"Apps: {versions}",
            "",
            "## Changes",
            *[f"- **{path.stem}**: " + "; ".join(changes) for path, _, changes in pending],
            "",
            "Generated by `.github/workflows/scoop-autoupdate.yml`.",
        ]
    )

    # Checked before writing so a sweep for an already-open PR refreshes that
    # PR instead of failing to push or silently doing nothing.
    open_pr_url = existing_pr(branch)

    if not commit_changes(branch, f"chore(scoop): update {versions}"):
        return 0

    if args.push:
        try:
            push_branch(branch)
            open_pr(branch, title, body)
        except SweepError as exc:
            print(f"  ERROR {exc}")
            return 1
    else:
        print(f"  committed on branch {branch} (pass --push to publish)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report updates without writing files")
    parser.add_argument("--only", default="", help="comma-separated manifest names to sweep")
    parser.add_argument("--push", action="store_true", help="push the update branch and open a PR")
    parser.add_argument("--allow-downgrade", action="store_true", help="allow moving to an older upstream version")
    parser.add_argument(
        "--check-hashes",
        action="store_true",
        help="re-download every pinned asset and verify the recorded hash",
    )
    args = parser.parse_args()
    return check_hashes(args) if args.check_hashes else sweep(args)


if __name__ == "__main__":
    sys.exit(main())
