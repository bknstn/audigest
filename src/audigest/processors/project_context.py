"""Bounded, read-only loading of explicitly selected project context files."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from audigest.errors import ConfigurationError, SourceResolutionError

MAX_CONTEXT_FILES = 16
MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_PATH_CHARS = 1024
DEFAULT_CONTEXT_PATHS = ("README.md",)


@dataclass(frozen=True, slots=True)
class ProjectContextFile:
    """One selected file's in-memory text and safe, non-absolute identity."""

    relative_path: str
    text: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """A resolved project directory and the selected files read from it."""

    root: Path
    name: str
    files: tuple[ProjectContextFile, ...]

    @property
    def identity_sha256(self) -> str:
        """Hash the resolved project path without persisting or exposing it."""

        return hashlib.sha256(os.fsencode(str(self.root))).hexdigest()


def resolve_project_root(project: str | Path) -> Path:
    """Resolve an explicitly supplied existing project directory."""

    if not str(project).strip():
        raise ConfigurationError("Project directory must be a non-empty path.")
    try:
        root = Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError(
            "Project directory does not exist or cannot be resolved."
        ) from None
    if not root.is_dir():
        raise ConfigurationError("Project path must be an existing directory.")
    return root


def ensure_cache_outside_project(cache_root: str | Path, project_root: Path) -> None:
    """Prevent processor output from modifying the selected project directory."""

    try:
        resolved_cache = Path(cache_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("Configured cache directory cannot be resolved safely.") from None
    if resolved_cache == project_root or resolved_cache.is_relative_to(project_root):
        raise ConfigurationError(
            "The configured cache directory is inside the project; set it outside the project "
            "before running apply."
        )


def normalize_context_paths(paths: Sequence[str] | None) -> tuple[str, ...]:
    """Validate, deduplicate, and sort relative context names deterministically."""

    requested = DEFAULT_CONTEXT_PATHS if paths is None else tuple(paths)
    if not requested:
        raise ConfigurationError("Select at least one project context file.")

    normalized: set[str] = set()
    for raw_path in requested:
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ConfigurationError("Context paths must be non-empty relative filenames.")
        if "\\" in raw_path:
            raise ConfigurationError("Context paths must use relative paths with '/' separators.")
        windows_path = PureWindowsPath(raw_path)
        if Path(raw_path).is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ConfigurationError("Absolute project context paths are not allowed.")

        components = raw_path.split("/")
        if any(component == ".." for component in components):
            raise ConfigurationError("Project context paths must not traverse parent directories.")
        if any(not component or component == "." for component in components):
            raise ConfigurationError(
                "Project context paths must not contain empty or dot components."
            )
        if any(component.startswith(".") for component in components):
            raise ConfigurationError("Hidden project context path components are not allowed.")
        if any(any(ord(character) < 32 for character in component) for component in components):
            raise ConfigurationError("Control characters are not allowed in context paths.")

        relative_path = PurePosixPath(*components).as_posix()
        if len(relative_path) > MAX_CONTEXT_PATH_CHARS:
            raise ConfigurationError(
                f"Project context paths must be at most {MAX_CONTEXT_PATH_CHARS} characters."
            )
        normalized.add(relative_path)

    selected = tuple(sorted(normalized))
    if len(selected) > MAX_CONTEXT_FILES:
        raise ConfigurationError(f"Select no more than {MAX_CONTEXT_FILES} project context files.")
    return selected


def read_project_context(
    project: str | Path,
    paths: Sequence[str] | None = None,
) -> ProjectContext:
    """Read only the selected bounded UTF-8 files using no-follow directory handles."""

    root = resolve_project_root(project)
    selected_paths = normalize_context_paths(paths)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ConfigurationError("This platform cannot safely open project context files.")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(root, directory_flags)
    except OSError:
        raise ConfigurationError("Project directory cannot be opened safely.") from None

    files: list[ProjectContextFile] = []
    total_bytes = 0
    try:
        for relative_path in selected_paths:
            raw = _read_relative_file(root_fd, relative_path, MAX_CONTEXT_BYTES - total_bytes)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ConfigurationError(
                    f"Project context file '{relative_path}' is not valid UTF-8."
                ) from None
            if not content.strip():
                raise ConfigurationError(
                    f"Project context file '{relative_path}' is empty or whitespace-only."
                )
            total_bytes += len(raw)
            files.append(
                ProjectContextFile(
                    relative_path=relative_path,
                    text=content,
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                )
            )
    finally:
        try:
            os.close(root_fd)
        except OSError:
            raise ConfigurationError(
                "Project directory handle could not be closed safely."
            ) from None

    return ProjectContext(root=root, name=root.name or "project", files=tuple(files))


def _read_relative_file(root_fd: int, relative_path: str, byte_limit: int) -> bytes:
    """Open one path component at a time and reject link, type, and size races."""

    components = PurePosixPath(relative_path).parts
    current_fd = root_fd
    owned_directory_fd: int | None = None
    try:
        for component in components[:-1]:
            info = _stat_component(current_fd, component, relative_path)
            if stat.S_ISLNK(info.st_mode):
                raise ConfigurationError(
                    f"Project context path '{relative_path}' contains a symbolic link."
                )
            if not stat.S_ISDIR(info.st_mode):
                raise ConfigurationError(
                    f"Project context path '{relative_path}' has a non-directory component."
                )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            next_fd: int | None = None
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
                opened_info = os.fstat(next_fd)
            except OSError:
                if next_fd is not None:
                    try:
                        os.close(next_fd)
                    except OSError:
                        pass
                raise ConfigurationError(
                    f"Project context path '{relative_path}' changed or cannot be opened safely."
                ) from None
            if not stat.S_ISDIR(opened_info.st_mode) or (
                opened_info.st_dev,
                opened_info.st_ino,
            ) != (info.st_dev, info.st_ino):
                _close_descriptor(next_fd, relative_path)
                raise ConfigurationError(
                    f"Project context path '{relative_path}' changed while being opened."
                )
            if owned_directory_fd is not None:
                os.close(owned_directory_fd)
            current_fd = next_fd
            owned_directory_fd = next_fd

        filename = components[-1]
        info = _stat_component(current_fd, filename, relative_path)
        if stat.S_ISLNK(info.st_mode):
            raise ConfigurationError(f"Project context file '{relative_path}' is a symbolic link.")
        if not stat.S_ISREG(info.st_mode):
            raise ConfigurationError(
                f"Project context file '{relative_path}' is not a regular file."
            )
        if info.st_size > byte_limit:
            raise ConfigurationError(
                f"Project context exceeds the {MAX_CONTEXT_BYTES}-byte total limit."
            )

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(filename, flags, dir_fd=current_fd)
        except OSError:
            raise ConfigurationError(
                f"Project context file '{relative_path}' changed or cannot be opened safely."
            ) from None
        try:
            try:
                opened_info = os.fstat(file_fd)
            except OSError:
                raise ConfigurationError(
                    f"Project context file '{relative_path}' cannot be inspected safely."
                ) from None
            if not stat.S_ISREG(opened_info.st_mode) or (
                opened_info.st_dev,
                opened_info.st_ino,
            ) != (info.st_dev, info.st_ino):
                raise ConfigurationError(
                    f"Project context file '{relative_path}' changed or is not a regular file."
                )
            return _read_at_most(file_fd, relative_path, byte_limit)
        finally:
            _close_descriptor(file_fd, relative_path)
    finally:
        if owned_directory_fd is not None:
            _close_descriptor(owned_directory_fd, relative_path)


def _stat_component(directory_fd: int, component: str, relative_path: str) -> os.stat_result:
    try:
        return os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SourceResolutionError(
            f"Selected project context file '{relative_path}' was not found."
        ) from None
    except OSError:
        raise ConfigurationError(
            f"Selected project context file '{relative_path}' cannot be inspected safely."
        ) from None


def _read_at_most(file_fd: int, relative_path: str, byte_limit: int) -> bytes:
    data = bytearray()
    while len(data) <= byte_limit:
        try:
            chunk = os.read(file_fd, min(64 * 1024, byte_limit + 1 - len(data)))
        except OSError:
            raise ConfigurationError(
                f"Project context file '{relative_path}' cannot be read safely."
            ) from None
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > byte_limit:
            raise ConfigurationError(
                f"Project context exceeds the {MAX_CONTEXT_BYTES}-byte total limit."
            )
    if not data:
        raise ConfigurationError(f"Project context file '{relative_path}' is empty.")
    return bytes(data)


def _close_descriptor(file_descriptor: int, relative_path: str) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        raise ConfigurationError(
            f"Project context handle for '{relative_path}' could not be closed safely."
        ) from None


__all__ = [
    "DEFAULT_CONTEXT_PATHS",
    "MAX_CONTEXT_BYTES",
    "MAX_CONTEXT_FILES",
    "MAX_CONTEXT_PATH_CHARS",
    "ProjectContext",
    "ProjectContextFile",
    "ensure_cache_outside_project",
    "normalize_context_paths",
    "read_project_context",
    "resolve_project_root",
]
