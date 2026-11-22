"""Validate and build a deterministic, text-only Agensi skill ZIP."""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_STORED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "audigest"
DEFAULT_OUTPUT = ROOT / "dist" / "audigest-agensi-skill.zip"
MEMBERS = ("LICENSE.txt", "SKILL.md", "references/setup.md", "references/workflows.md")
MAX_FILE_BYTES = 64 * 1024
_FRONTMATTER_FIELD = re.compile(r'([a-z][a-z-]*): (".*")')
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_LINK = re.compile(r"\[[^\]\n]+\]\(\s*<?([^)>\s]+)>?[^)\n]*\)")
_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|xi-[A-Za-z0-9_-]{24,}|A(?:KIA|SIA)[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:OPENAI|ANTHROPIC|ELEVENLABS)_API_KEY\s*=\s*['\"]?[A-Za-z0-9_-]{12,}"
)


def _read_member(skill_root: Path, name: str) -> bytes:
    path = skill_root / name
    if any(part.is_symlink() for part in (skill_root, *path.parents, path)):
        raise ValueError(f"Skill member uses a symlink: {name}")
    if not path.is_file():
        raise ValueError(f"Missing or non-regular skill member: {name}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Skill member exceeds {MAX_FILE_BYTES} bytes: {name}")
    return data


def _read_frontmatter(document: str) -> tuple[dict[str, str], dict[str, str], str]:
    # This skill uses a small YAML subset with JSON-quoted strings and a flat metadata map.
    # Restricting the source format keeps the build dependent only on the standard library.
    opening, _, remaining = document.partition("\n")
    header, separator, body = remaining.partition("\n---\n")
    if opening != "---" or not separator or not body.strip():
        raise ValueError("SKILL.md requires YAML frontmatter and a non-empty body")
    fields: dict[str, str] = {}
    metadata: dict[str, str] = {}
    has_metadata = False
    for line in header.splitlines():
        if line == "metadata:" and not has_metadata:
            has_metadata = True
            continue
        nested = line.startswith("  ")
        match = _FRONTMATTER_FIELD.fullmatch(line[2:] if nested else line)
        if match is None or nested != has_metadata:
            raise ValueError("Use YAML frontmatter with quoted strings and a flat metadata map")
        name, encoded_value = match.groups()
        target = metadata if nested else fields
        if name in target:
            raise ValueError(f"Duplicate SKILL.md frontmatter field: {name}")
        value = json.loads(encoded_value)
        if not value.strip():
            raise ValueError(f"Empty SKILL.md frontmatter field: {name}")
        target[name] = value
    return fields, metadata, body


def _validate(files: dict[str, str], skill_name: str, cli_version: str) -> None:
    fields, metadata, body = _read_frontmatter(files["SKILL.md"])
    if fields.keys() != {"name", "description", "license", "compatibility"}:
        raise ValueError("SKILL.md requires name, description, license and compatibility")
    if (
        fields["name"] != skill_name
        or len(skill_name) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_name)
    ):
        raise ValueError("Skill name must match its directory and follow Agent Skills naming")
    if not 40 <= len(fields["description"]) <= 1024:
        raise ValueError("Provide a meaningful skill description of at most 1024 characters")
    if len(fields["compatibility"]) > 500:
        raise ValueError("Skill compatibility must be at most 500 characters")
    if fields["license"] != "MIT (see LICENSE.txt)":
        raise ValueError("SKILL.md must identify the bundled MIT license")
    if metadata.keys() != {"version", "audigest-version"} or not _VERSION.fullmatch(
        metadata["version"]
    ):
        raise ValueError("Provide a semantic skill version and compatible Audigest version")
    if metadata["audigest-version"] != cli_version:
        raise ValueError("SKILL.md audigest-version must match the Audigest package version")

    for name, contents in files.items():
        # All file references in the skill are relative to the package root.
        markdown = body if name == "SKILL.md" else contents
        for link in _LINK.findall(markdown):
            url = urlsplit(link)
            if url.scheme in ("https", "http") and url.hostname and url.username is None:
                continue
            link_path = unquote(url.path)
            target = PurePosixPath(link_path)
            if not link_path and not url.netloc and not url.scheme and url.fragment:
                continue
            if url.scheme or url.netloc or target.as_posix() not in files:
                raise ValueError(f"Invalid or unpackaged link in {name}: {link}")
    for name in MEMBERS:
        if name.startswith("references/") and name not in _LINK.findall(body):
            raise ValueError(f"SKILL.md must link to {name}")


def build_skill_zip(skill_root: Path, output: Path, *, cli_version: str) -> None:
    raw_files = {name: _read_member(skill_root, name) for name in MEMBERS}
    text_files = {}
    for name, raw in raw_files.items():
        try:
            document = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"Skill member must be UTF-8 text: {name}") from None
        if any(not char.isprintable() and char not in "\t\r\n" for char in document):
            raise ValueError(f"Skill member contains non-text characters: {name}")
        if _SECRET.search(document):
            raise ValueError(f"Possible credential in skill member: {name}")
        text_files[name] = document
    _validate(text_files, skill_root.name, cli_version)

    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as temp:
        temporary_path = Path(temp.name)
    try:
        with ZipFile(temporary_path, "w", compression=ZIP_STORED) as archive:
            for name, data in raw_files.items():
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, data)
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the ZIP"
    )
    args = parser.parse_args()
    try:
        package = (ROOT / "src" / "audigest" / "__init__.py").read_text(encoding="utf-8")
        version = re.search(r'^__version__ = "([0-9.]+)"$', package, re.MULTILINE)
        if version is None:
            raise ValueError("Could not read the Audigest package version")
        if _read_member(SKILL_ROOT, "LICENSE.txt") != (ROOT / "LICENSE").read_bytes():
            raise ValueError("The bundled skill license must match the repository's MIT license")
        build_skill_zip(SKILL_ROOT, args.output, cli_version=version[1])
    except (OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(args.output)


if __name__ == "__main__":
    main()
