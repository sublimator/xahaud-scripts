"""Tests for suite ``network:`` block validators and raw-argument quoting."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo
from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.testnet.launcher.iterm_panes import ITermPanesLauncher
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.suite import (
    _validate_network_config,
    _validated_desktop,
    _validated_extra_args,
    _validated_genesis_file,
)


def _node(tmp_path: Path) -> NodeInfo:
    return NodeInfo(
        id=0,
        public_key="pk0",
        token="tok0",
        config_path=tmp_path / "n0" / "xahaud.cfg",
        port_peer=21235,
        port_rpc=5005,
        port_ws=6005,
    )


def _launch_config(tmp_path: Path, extra_args: list[str]) -> LaunchConfig:
    return LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
        extra_args=extra_args,
    )


# --- extra_args quoting oracle ---------------------------------------------

# A spaced value must stay ONE argument, and a substitution must stay literal.
HOSTILE_ARGS = ["--label=a b", "$(printf injected)", "semi;colon", "quote'd"]


@pytest.mark.parametrize(
    "launcher",
    [TmuxLauncher(), ITermLauncher(), ITermPanesLauncher()],
    ids=["tmux", "iterm", "iterm_panes"],
)
def test_extra_args_survive_shell_parsing_intact(launcher, tmp_path: Path):
    """Every launcher joins args into a string a shell later parses.

    Unquoted, `--label=a b` became two argv entries and `$(...)` was executed
    by the terminal. The contract the suite validator advertises is that one
    YAML list entry is one argument, so shell-splitting the built command must
    recover the list exactly.
    """
    flags = launcher._build_startup_flags(  # noqa: SLF001
        _node(tmp_path), _launch_config(tmp_path, HOSTILE_ARGS)
    )

    parsed = shlex.split(flags)
    # Drop the --ledgerfile pair the builder always emits first.
    assert parsed[0] == "--ledgerfile"
    assert parsed[2:] == HOSTILE_ARGS


# --- _validated_extra_args --------------------------------------------------


def test_extra_args_accepts_strings_and_numbers():
    assert _validated_extra_args(None) == []
    assert _validated_extra_args(["--a", 5, 1.5]) == ["--a", "5", "1.5"]


def test_extra_args_rejects_bare_string():
    """`extra_args: "--a --b"` would otherwise silently become one argument."""
    with pytest.raises(ValueError, match="not a bare string"):
        _validated_extra_args("--a --b")


def test_extra_args_rejects_bool_because_bool_is_an_int():
    with pytest.raises(ValueError, match="string or number"):
        _validated_extra_args([True])


# --- _validated_desktop -----------------------------------------------------


@pytest.mark.parametrize("value", [1, 9])
def test_desktop_accepts_boundaries(value: int):
    assert _validated_desktop(value) == value


@pytest.mark.parametrize("value", [0, 10, 99])
def test_desktop_rejects_out_of_range(value: int):
    with pytest.raises(ValueError, match="between 1 and 9"):
        _validated_desktop(value)


@pytest.mark.parametrize("value", [True, "3", 1.0])
def test_desktop_rejects_wrong_types(value: object):
    with pytest.raises(ValueError, match="must be an integer"):
        _validated_desktop(value)


# --- _validated_genesis_file ------------------------------------------------


def test_genesis_file_resolves_relative_to_root(tmp_path: Path):
    genesis = tmp_path / "custom.json"
    genesis.write_text("{}")
    assert _validated_genesis_file("custom.json", xahaud_root=tmp_path) == genesis


def test_genesis_file_rejects_missing_path(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        _validated_genesis_file("nope.json", xahaud_root=tmp_path)


def test_genesis_file_none_means_bundled(tmp_path: Path):
    assert _validated_genesis_file(None, xahaud_root=tmp_path) is None


# --- the single validation entry point --------------------------------------


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"genesis_file": "missing.json"}, "does not exist"),
        ({"extra_args": "--bare"}, "not a bare string"),
        ({"desktop": 99}, "between 1 and 9"),
    ],
    ids=["genesis_file", "extra_args", "desktop"],
)
def test_validate_network_config_rejects_each_field(
    config: dict, match: str, tmp_path: Path
):
    """These three were previously only reached inside _build_launch_config.

    That meant --dry-run skipped them entirely, and a real run did not reject
    them until after it had torn down the previous network.
    """
    with pytest.raises(ValueError, match=match):
        _validate_network_config(config, xahaud_root=tmp_path)


def test_extra_args_rejects_nul_byte():
    """NUL is the one class quoting cannot rescue — argv cannot hold it.

    YAML "\\0" decodes to "\\x00" and would pass every other check, then fail
    deep in subprocess, which is exactly what up-front validation is for.
    """
    with pytest.raises(ValueError, match="NUL byte"):
        _validated_extra_args(["--label=a\x00b"])


def test_validate_network_config_rejects_nul_in_extra_args(tmp_path: Path):
    with pytest.raises(ValueError, match="NUL byte"):
        _validate_network_config({"extra_args": ["\x00"]}, xahaud_root=tmp_path)
