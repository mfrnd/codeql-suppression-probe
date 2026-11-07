# uv-probe

Whether Dependabot's uv updater, under `versioning-strategy: lockfile-only` and a seven-day
cooldown, moves the packages of a dependency group that nothing installs, and the package named
`uv` among them.

`pyproject.toml` and `uv.lock` are KeePassGate's at `fa77fbc8`, with a `build` group added and
locked at hatchling 1.27.0 and uv 0.13.0. `Directory.Build.props` and the two files under `src/`
are what hatchling reads to make the project's metadata. The configuration is the `uv` entry in
`main`'s `.github/dependabot.yml`, the same as KeePassGate's apart from `target-branch: uv-probe`.

Expected on 2026-11-07, if the cooldown takes the newest release a week old: hatchling 1.33.0,
uv 0.13.6 rather than 0.13.7, and ruff, in the default `dev` group, 0.17.3 rather than 0.17.4.
