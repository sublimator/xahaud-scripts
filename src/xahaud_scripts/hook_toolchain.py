"""Preflight the external compilers used for xahaud Hook test fixtures."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

_QUICKJS_INLINE_MARKERS = ("[test.tshook]", "[test.jshook]")
_QUICKJS_FILE_REF = re.compile(r"file:[^\"'\s]+\.(?:js|ts)\b")


def uses_quickjs_hooks(test_file: Path) -> bool:
    """Return whether a C++ test file references JavaScript/TypeScript Hooks."""
    source = test_file.read_text(encoding="utf-8")
    return any(marker in source for marker in _QUICKJS_INLINE_MARKERS) or bool(
        _QUICKJS_FILE_REF.search(source)
    )


def require_test_hook_toolchain(test_file: Path) -> None:
    """Fail early when hookz or the selected jshookz compiler is unavailable."""
    if shutil.which("hookz") is None:
        raise RuntimeError(
            "hookz was not found on PATH; it owns test-Hook extraction and "
            "C/WAT compilation"
        )

    if not uses_quickjs_hooks(test_file):
        return

    configured = os.environ.get("JSHOOKZ_HOOK_COMPILER")
    if configured is None:
        configured = os.environ.get("QJS_HOOK_COMPILER", "jshookz compile-hook")
    command = shlex.split(configured)
    if not command:
        raise RuntimeError("JSHOOKZ_HOOK_COMPILER resolved to an empty command")
    if shutil.which(command[0]) is None:
        raise RuntimeError(
            f"{command[0]} was not found on PATH; jshookz owns JavaScript/"
            "TypeScript Hook compilation"
        )
