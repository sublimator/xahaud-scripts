from __future__ import annotations

import pytest

from xahaud_scripts.utils.quoting import applescript_string, shell_export, shell_quote


def test_shell_quote_quotes_one_shell_token() -> None:
    assert shell_quote("dir name; touch nope") == "'dir name; touch nope'"


def test_applescript_string_escapes_applescript_literal_chars() -> None:
    assert applescript_string('a"b\\c\n') == '"a\\"b\\\\c\\n"'


def test_shell_export_rejects_invalid_env_names() -> None:
    assert (
        shell_export("GOOD_NAME_1", "value with space")
        == "export GOOD_NAME_1='value with space'"
    )

    with pytest.raises(ValueError, match="shell identifiers"):
        shell_export("BAD-NAME", "value")
