# Hacking on `rss2email`

This document is still TODO. Please contribute to it and file issues
if you have a question (or something is not described sufficiently).
See `AGENTS.md` for the canonical build/test/release commands an
agent (or contributor) should run.

## Cutting a new release

The release flow uses [uv](https://github.com/astral-sh/uv) and the
optional `update-copyright` dev dependency. There is no Nix shell
anymore (the previous Nix expressions pinned a 2021 nixpkgs that pre-dated
`uv` and targeted Python 3.6/3.7, so they were broken and have been
removed).

- `uv sync` (sync the dev environment from the lockfile)
- `uv run --extra dev update-copyright` (config in
  `.update-copyright.conf`; this refreshes the per-file author/year
  headers in every source file)
- Prepare `CHANGELOG`
- Bump `__version__` in `rss2email/__init__.py` (the source of truth;
  `pyproject.toml` reads it dynamically via
  `tool.setuptools.dynamic.version`)
- `git commit`

- `uv build` (produces `dist/rss2email-<version>.{tar.gz,whl}`)
- `uv run twine check dist/*`
- `uv run twine upload --repository-url https://test.pypi.org/legacy/ dist/*`
  You need to register a separate account on test.pypi.org, then be
  added to the rss2email package there separately.
- Verify the test-pypi install works (install in a fresh venv, run the
  unittest suite from a checkout).

- Tag and push: `git tag v<version> && git push --tags origin master`
- `uv run twine upload dist/*`