from pathlib import Path

import pytest

from xahaud_scripts import hook_toolchain


def test_c_hook_only_requires_hookz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_file = tmp_path / "C_test.cpp"
    test_file.write_text(
        'R"[test.hook](int64_t hook(uint32_t r) { return 0; })[test.hook]"'
    )
    monkeypatch.setattr(hook_toolchain.shutil, "which", lambda name: f"/bin/{name}")

    hook_toolchain.require_test_hook_toolchain(test_file)


def test_typescript_hook_requires_jshookz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    test_file = tmp_path / "JS_test.cpp"
    test_file.write_text(
        'R"[test.tshook](export function hook(_r: number) {})[test.tshook]"'
    )
    monkeypatch.delenv("JSHOOKZ_HOOK_COMPILER", raising=False)
    monkeypatch.delenv("QJS_HOOK_COMPILER", raising=False)
    monkeypatch.setattr(
        hook_toolchain.shutil,
        "which",
        lambda name: "/bin/hookz" if name == "hookz" else None,
    )

    with pytest.raises(RuntimeError, match="jshookz owns"):
        hook_toolchain.require_test_hook_toolchain(test_file)


def test_typescript_hook_accepts_configured_compiler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    test_file = tmp_path / "JS_test.cpp"
    test_file.write_text('auto hook = "file:fixtures/hook.ts";')
    monkeypatch.setenv("JSHOOKZ_HOOK_COMPILER", "custom-jshookz compile-hook")
    monkeypatch.setattr(
        hook_toolchain.shutil,
        "which",
        lambda name: f"/bin/{name}" if name in {"hookz", "custom-jshookz"} else None,
    )

    hook_toolchain.require_test_hook_toolchain(test_file)
