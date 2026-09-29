"""Resolve one source file without importing or executing submitted code."""

import fnmatch
import os
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath

from .models import CodeFile, GradingInputError, ResolvedSource, TargetKind


class FileResolver:
    """Resolve AD-12 identifiers against a submission directory.

    FILE uses a relative POSIX glob, with ``**`` matching zero or more path
    segments. Java METHOD uses ``package.Class#method`` and CLASS uses
    ``package.Class``; ``Outer$Inner`` and ``Outer.Inner`` are supported.
    Dotted Java names use the conventional lowercase package / uppercase class
    naming rule: the first uppercase-start segment names the outermost class.
    This ensures ``pkg.Outer.Inner`` never silently resolves ``Inner.java``.
    An unqualified name or an explicit ``$`` boundary also identifies a class.
    Use FILE for uppercase package names or lowercase dotted class names;
    Java's syntax alone cannot disambiguate those nonconventional layouts.

    Python METHOD/CLASS use the local convention ``relative/module.py:qualname``.
    Dotted module conventions are not inferred; use FILE for other layouts.
    Qualnames select a file, not an extracted function or class body.

    Only the selected file's contents are read. Other regular files are listed
    by relative path. Directory symlinks are not traversed, source symlinks are
    rejected, and any encountered symlink escaping the submission is rejected.
    """

    def resolve(
        self,
        identifier: str,
        kind: TargetKind | str,
        language: str,
        submission_dir: str | Path,
    ) -> ResolvedSource:
        if not isinstance(identifier, str) or not identifier.strip():
            raise GradingInputError("target identifier must not be blank")
        try:
            target_kind = TargetKind(kind)
        except (TypeError, ValueError) as exc:
            raise GradingInputError(f"Unsupported target kind: {kind!r}.") from exc

        # Validate configuration before traversing any submission files.
        source_names = None
        if target_kind is TargetKind.FILE:
            pattern = self._relative_pattern(identifier)
        elif isinstance(language, str) and language.strip().casefold() == "java":
            source_names = self._java_source_names(identifier, target_kind)
            pattern = None
        elif isinstance(language, str) and language.strip().casefold() == "python":
            pattern = self._python_source_path(identifier)
        else:
            raise GradingInputError(
                f"Cannot resolve {target_kind.value} target {identifier!r} for language "
                f"{language!r}; use a FILE identifier or configure a supported language."
            )

        try:
            root = Path(submission_dir).resolve(strict=True)
            if not root.is_dir():
                raise GradingInputError("submission_dir must be an existing directory")
            files, symlinks = self._list_files(root)
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, GradingInputError):
                raise
            raise GradingInputError(
                f"Cannot inspect the submission directory for target {identifier!r}."
            ) from exc

        candidates = sorted(
            name for name in files + symlinks
            if (PurePosixPath(name).name in source_names if source_names is not None
                else self._matches(name, pattern))
        )
        if not candidates:
            raise GradingInputError(
                f"No source file matches target {identifier!r}; check the configured "
                "identifier and submission layout."
            )
        if len(candidates) != 1:
            raise GradingInputError(
                f"Multiple source files match target {identifier!r}: "
                f"{', '.join(candidates)}. Check the configured identifier and submission layout."
            )

        selected = candidates[0]
        source = root / selected
        try:
            if source.is_symlink():
                raise GradingInputError(
                    f"Source file {selected!r} for target {identifier!r} is a symbolic link; "
                    "provide a regular source file."
                )
            # Recheck containment immediately before reading the selected source.
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise GradingInputError(f"Source file {selected!r} escapes the submission directory.")
            content = resolved.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise GradingInputError(
                f"Source file {selected!r} for target {identifier!r} must be UTF-8 text."
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise GradingInputError(
                f"Cannot read source file {selected!r} for target {identifier!r}; "
                "check the file and its permissions."
            ) from exc

        return ResolvedSource(
            source_file=CodeFile(path=selected, content=content),
            other_file_paths=tuple(name for name in files if name != selected),
        )

    @staticmethod
    def _relative_pattern(identifier: str) -> str:
        path = PurePosixPath(identifier)
        if (not identifier or "\x00" in identifier or "\\" in identifier
                or path.is_absolute() or PureWindowsPath(identifier).drive
                or ".." in path.parts):
            raise GradingInputError(
                "Source paths must be relative POSIX paths without '..' or absolute paths."
            )
        return path.as_posix()

    @classmethod
    def _python_source_path(cls, identifier: str) -> str:
        parts = identifier.split(":")
        if (len(parts) != 2 or not parts[0].endswith(".py")
                or not all(segment.isidentifier() for segment in parts[1].split("."))
                or any(char in parts[0] for char in "*?[]")):
            raise GradingInputError(
                "Python METHOD/CLASS identifiers must use 'relative/module.py:qualname'; "
                "use FILE for other layouts."
            )
        return cls._relative_pattern(parts[0])

    @staticmethod
    def _java_source_names(identifier: str, kind: TargetKind) -> set[str]:
        parts = identifier.split("#")
        if kind is TargetKind.METHOD:
            if len(parts) != 2 or not parts[1].isidentifier():
                raise GradingInputError("Java METHOD identifiers must use 'package.Class#method'.")
        elif len(parts) != 1:
            raise GradingInputError("Java CLASS identifiers must use 'package.Class'.")

        class_name = parts[0]
        if not all(segment.isidentifier() for segment in class_name.replace("$", ".").split(".")):
            raise GradingInputError(f"Invalid Java class identifier: {class_name!r}.")
        segments = class_name.split("$", 1)[0].split(".")
        outer = next((segment for segment in segments if segment[0].isupper()), None)
        if outer is None:
            if len(segments) == 1 or "$" in class_name:
                outer = segments[-1]
            else:
                raise GradingInputError(
                    "Dotted Java identifiers require lowercase package names and an "
                    "uppercase outer-class name; use FILE for other naming conventions."
                )
        return {f"{outer}.java"}

    @staticmethod
    def _matches(name: str, pattern: str) -> bool:
        names = PurePosixPath(name).parts
        patterns = PurePosixPath(pattern).parts

        @lru_cache(maxsize=None)
        def match(name_index: int, pattern_index: int) -> bool:
            if pattern_index == len(patterns):
                return name_index == len(names)
            if patterns[pattern_index] == "**":
                return (match(name_index, pattern_index + 1)
                        or (name_index < len(names) and match(name_index + 1, pattern_index)))
            return (name_index < len(names)
                    and fnmatch.fnmatchcase(names[name_index], patterns[pattern_index])
                    and match(name_index + 1, pattern_index + 1))

        return match(0, 0)

    @staticmethod
    def _list_files(root: Path) -> tuple[list[str], list[str]]:
        files = []
        symlinks = []

        def on_error(error: OSError) -> None:
            raise error

        for directory, directories, filenames in os.walk(root, followlinks=False, onerror=on_error):
            for name in sorted(directories + filenames):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    if not path.resolve(strict=True).is_relative_to(root):
                        raise GradingInputError(
                            f"Symbolic link {relative!r} escapes the submission directory."
                        )
                    if name in filenames:
                        symlinks.append(relative)
                elif path.is_file():
                    files.append(relative)
            directories[:] = sorted(name for name in directories
                                    if not (Path(directory) / name).is_symlink())
        return sorted(files), sorted(symlinks)
