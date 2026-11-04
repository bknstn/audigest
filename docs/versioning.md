# Versioning

Audigest uses SemVer for package releases. Before `1.0.0`, the CLI and Python internals may still change; `0.y.z` communicates implementation progress, not a stable API promise. A patch release fixes defects without intentionally changing accepted behavior. A minor `0.y.0` release may add features or change behavior. After `1.0.0`, use SemVer's compatibility rules.

## Single version source

The package version lives only in `src/audigest/__init__.py` as `__version__`. Hatch reads it through the dynamic version path in `pyproject.toml`; package metadata and the `audigest --version` command use that value. `uv.lock` records the resolved project metadata and should be regenerated or checked after a version bump. The canonical transcript `schema_version`, processor versions, cache identities, and prompt versions have separate compatibility lifecycles and must not be changed just to bump the package version.

## Prepare a version

1. Update `__version__` in `src/audigest/__init__.py` and add a `docs/CHANGELOG.md` entry.
2. Run `uv lock` and the test, lint, format, type, and package-build checks. Verify `audigest --version` and built wheel metadata report the same value.
3. Review the diff and publish it only after the commit lands on `main` and CI passes on supported Python versions.
4. Before creating a tag, check that the exact `vX.Y.Z` tag is absent locally and remotely. Create an annotated tag on the accepted release commit and push it. Never move or replace an existing tag.

CI does not create tags, publish packages, or perform releases automatically. Create and push release tags only when the task authorizes it and the code and CI review are complete.
