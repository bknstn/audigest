"""Offline checks of the Agensi upload package."""

from __future__ import annotations

import runpy
import shutil
import stat
from pathlib import Path
from zipfile import ZipFile

import pytest

from audigest import __version__

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "build_agensi_skill.py"
_BUILD = runpy.run_path(str(_SCRIPT))["build_skill_zip"]
_SKILL = _ROOT / "skills" / "audigest"
_EXPECTED = ["LICENSE.txt", "SKILL.md", "references/setup.md", "references/workflows.md"]


@pytest.fixture
def skill_copy(tmp_path: Path) -> Path:
    return shutil.copytree(_SKILL, tmp_path / "audigest")


def test_package_reproducible_and_extractable(skill_copy: Path, tmp_path: Path) -> None:
    for name in (".env", ".DS_Store", "private-transcript.md", "unlisted.txt"):
        (skill_copy / name).write_text("synthetic and excluded", encoding="utf-8")
    (skill_copy / "references" / "extra.md").symlink_to(skill_copy / ".env")
    (skill_copy / "site-packages").mkdir()
    (skill_copy / "site-packages" / "module.py").touch()

    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    _BUILD(skill_copy, first, cli_version=__version__)
    (skill_copy / "SKILL.md").touch()
    _BUILD(skill_copy, second, cli_version=__version__)
    assert first.read_bytes() == second.read_bytes()

    clean = tmp_path / "extracted" / "audigest"
    with ZipFile(first) as archive:
        assert archive.namelist() == _EXPECTED
        assert archive.testzip() is None
        assert all(stat.S_ISREG(member.external_attr >> 16) for member in archive.infolist())
        archive.extractall(clean)
    for name in _EXPECTED:
        assert (clean / name).read_bytes() == (_SKILL / name).read_bytes()
    assert list(tmp_path.glob(".*.zip.*")) == []


@pytest.mark.parametrize("member", ("SKILL.md", "references", "references/setup.md"))
def test_package_rejects_symlinked_members(skill_copy: Path, tmp_path: Path, member: str) -> None:
    member_path = skill_copy / member
    moved = tmp_path / member_path.name
    member_path.rename(moved)
    member_path.symlink_to(moved, target_is_directory=moved.is_dir())
    output = tmp_path / "bad.zip"
    with pytest.raises(ValueError, match="uses a symlink"):
        _BUILD(skill_copy, output, cli_version=__version__)
    assert not output.exists()


@pytest.mark.parametrize(
    ("original", "replacement", "error"),
    [
        ('name: "audigest"', 'name: "different-skill"', "must match"),
        ('name: "audigest"', "name: audigest", "YAML frontmatter"),
        ('name: "audigest"', 'name: "audigest"\nname: "audigest"', "Duplicate"),
        ('description: "Use for', 'about: "Use for', "requires name, description"),
        (f'audigest-version: "{__version__}"', 'audigest-version: "99.0.0"', "must match"),
        ("references/setup.md", "../setup.md", "Invalid or unpackaged link"),
        ("references/setup.md", "references/%2e%2e/setup.md", "Invalid or unpackaged link"),
        ("references/setup.md", "references/missing.md", "Invalid or unpackaged link"),
    ],
)
def test_package_rejects_invalid_metadata_or_links(
    skill_copy: Path, tmp_path: Path, original: str, replacement: str, error: str
) -> None:
    skill = skill_copy / "SKILL.md"
    document = skill.read_text(encoding="utf-8")
    assert original in document
    skill.write_text(document.replace(original, replacement), encoding="utf-8")
    output = tmp_path / "existing.zip"
    output.write_bytes(b"existing output")
    with pytest.raises(ValueError, match=error):
        _BUILD(skill_copy, output, cli_version=__version__)
    assert output.read_bytes() == b"existing output"


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        (b"\x00binary", "non-text"),
        (b"\xff", "UTF-8"),
        (b"x" * (64 * 1024 + 1), "exceeds"),
        (b"OPENAI_API_KEY=" + b"synthetic0123456789", "Possible credential"),
    ],
)
def test_package_rejects_invalid_text_or_secret(
    skill_copy: Path, tmp_path: Path, contents: bytes, error: str
) -> None:
    (skill_copy / "references" / "setup.md").write_bytes(contents)
    with pytest.raises(ValueError, match=error):
        _BUILD(skill_copy, tmp_path / "bad.zip", cli_version=__version__)
