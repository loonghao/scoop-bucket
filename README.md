# Scoop Bucket for loonghao's Tools

This is a [Scoop](https://scoop.sh/) bucket containing applications and tools developed by loonghao.

## Installation

First, add this bucket to your Scoop installation:

```powershell
scoop bucket add loonghao https://github.com/loonghao/scoop-bucket
```

Then install any of the available applications:

```powershell
scoop install vx
```

## Available Applications

| Application | Description |
|-------------|-------------|
| [bazaardb-cli](https://github.com/loonghao/bazaardb-cli) | Unofficial local CLI for The Bazaar card snapshots and ten-win combinations |
| [mcpcall](https://github.com/loonghao/mcpcall) | CLI for listing and calling MCP server tools over stdio or Streamable HTTP |
| [msvc-kit](https://github.com/loonghao/msvc-kit) | Portable MSVC Build Tools installer and manager for Rust development |
| [noti](https://github.com/loonghao/noti) | Unified multi-channel notification CLI for AI agents |
| [py2pyd](https://github.com/loonghao/py2pyd) | Rust-based tool to compile Python modules to pyd files |
| [rez-next](https://github.com/loonghao/rez-next) | Experimental Rust rewrite of Rez package manager core components |
| [rez-tools](https://github.com/loonghao/rez-tools) | Command line suite for the Rez package manager |
| [shimexe](https://github.com/loonghao/shimexe) | Modern executable shim manager with HTTP download support |
| [turbo-cdn](https://github.com/loonghao/turbo-cdn) | Intelligent download accelerator with geographic detection and CDN quality assessment |
| [vx](https://github.com/loonghao/vx) | Universal development tool manager |

## Automatic Updates

This bucket is automatically maintained by [`.github/workflows/scoop-autoupdate.yml`](.github/workflows/scoop-autoupdate.yml).

The workflow runs daily and, for every manifest under `bucket/`, polls the upstream
repository's latest GitHub release. When a new version is found it rewrites
`version` plus each architecture's `url`/`hash` and opens a pull request for
review instead of pushing to `main` directly.

Polling is used rather than listening for `release: published` because releases
created by release-please, or published via `gh release edit --draft=false`, do
not emit that event.

To add a new application, add a manifest under `bucket/` with a `checkver` block
and an `autoupdate.architecture` URL template using `$version` as the placeholder.
Manifests without those keys are skipped by the sweep.

Manual runs are available via **Actions → Scoop Autoupdate → Run workflow**, which
supports a dry-run mode and restricting the sweep to specific manifests.

## Contributing

Pull requests are welcome. Manifests must:

- pin a real SHA-256 `hash` per architecture;
- declare `checkver` and a matching `autoupdate.architecture` template;
- keep `version` in sync with the upstream release tag.

Run the checks locally with:

```powershell
uv run --with pytest python -m pytest tests/ -q
uv run python scripts/scoop_update.py --dry-run
```

## Support

For issues with specific applications, please visit their respective repositories
linked in the table above.

For issues with this bucket, please [open an issue](https://github.com/loonghao/scoop-bucket/issues).
