"""Quoting helpers for nested command launchers."""

from __future__ import annotations

import re
import shlex

_SHELL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def shell_quote(value: object) -> str:
    """Quote one zsh/sh token."""
    return shlex.quote(str(value))


def validate_shell_identifier(name: str) -> str:
    """Validate one shell variable/function identifier."""
    if not _SHELL_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            "environment variable names must be shell identifiers "
            "(letters, digits, and underscores; not starting with a digit)"
        )
    return name


def shell_export(name: str, value: object) -> str:
    """Return a safe shell export command for one environment variable."""
    return f"export {validate_shell_identifier(name)}={shell_quote(value)}"


def applescript_string(value: object) -> str:
    """Return an AppleScript string literal for ``value``.

    iTerm launchers send shell commands through AppleScript, so both layers
    matter: shell_quote protects the command after it reaches zsh; this helper
    protects the AppleScript source that transports the command.
    """
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\r", "\\r")
    text = text.replace("\n", "\\n")
    return f'"{text}"'
