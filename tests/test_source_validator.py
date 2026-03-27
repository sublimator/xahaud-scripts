"""Tests for SourceValidator in hooks/compiler.py."""

import pytest

from xahaud_scripts.hooks.compiler import SourceValidator


@pytest.fixture
def validator() -> SourceValidator:
    return SourceValidator()


class TestExtractDeclarations:
    """Test declaration and call extraction."""

    def test_simple_extern(self, validator: SourceValidator) -> None:
        source = "extern int32_t accept(uint32_t a, uint32_t b, int64_t c);"
        declared, used = validator.extract_declarations(source)
        assert "accept" in declared

    def test_function_name_with_digits(self, validator: SourceValidator) -> None:
        """Functions like util_sha512h should be recognized as one name."""
        source = (
            "extern int64_t util_sha512h("
            "uint32_t w, uint32_t wl, uint32_t r, uint32_t rl);\n"
            "int64_t hook(uint32_t r) { util_sha512h(a, b, c, d); return 0; }"
        )
        declared, used = validator.extract_declarations(source)
        assert "util_sha512h" in declared
        assert "util_sha512h" in used
        # 'h' should NOT appear as a separate function call
        assert "h" not in used

    def test_multiple_digit_functions(self, validator: SourceValidator) -> None:
        source = (
            "extern int64_t float_sto_set(uint32_t a, uint32_t b);\n"
            "extern int64_t etxn_fee_base(uint32_t a, uint32_t b);\n"
            "extern int64_t sha512h(uint32_t a, uint32_t b);\n"
            "int64_t hook(uint32_t r) {\n"
            "  float_sto_set(1, 2);\n"
            "  etxn_fee_base(3, 4);\n"
            "  sha512h(5, 6);\n"
            "  return 0;\n"
            "}"
        )
        declared, used = validator.extract_declarations(source)
        assert "float_sto_set" in declared
        assert "etxn_fee_base" in declared
        assert "sha512h" in declared
        assert "float_sto_set" in used
        assert "etxn_fee_base" in used
        assert "sha512h" in used

    def test_g_guard_function(self, validator: SourceValidator) -> None:
        """_g() is a common guard function in hooks."""
        source = (
            "extern int32_t _g(uint32_t id, uint32_t maxiter);\n"
            "int64_t hook(uint32_t r) { _g(1, 1); return 0; }"
        )
        declared, used = validator.extract_declarations(source)
        assert "_g" in declared
        assert "_g" in used

    def test_sizeof_excluded(self, validator: SourceValidator) -> None:
        source = "int x = sizeof(int);"
        declared, used = validator.extract_declarations(source)
        assert "sizeof" not in declared
        assert "sizeof" not in used

    def test_hook_cbak_excluded_from_used(self, validator: SourceValidator) -> None:
        """hook() and cbak() are entry points, not external calls."""
        source = "int64_t hook(uint32_t r) { return 0; }"
        _, used = validator.extract_declarations(source)
        assert "hook" not in used

    def test_define_macro(self, validator: SourceValidator) -> None:
        source = "#define SBUF(x) x, sizeof(x)"
        declared, _ = validator.extract_declarations(source)
        # SBUF is uppercase, won't match [a-z0-9_-]+
        assert "SBUF" not in declared


class TestValidate:
    """Test the validation logic."""

    def test_all_declared_passes(self, validator: SourceValidator) -> None:
        source = (
            "extern int32_t _g(uint32_t id, uint32_t maxiter);\n"
            "extern int64_t accept(uint32_t a, uint32_t b, int64_t c);\n"
            "int64_t hook(uint32_t r) { _g(1, 1); accept(0, 0, 0); return 0; }"
        )
        # Should not raise
        validator.validate(source)

    def test_undeclared_raises(self, validator: SourceValidator) -> None:
        source = (
            "extern int32_t _g(uint32_t id, uint32_t maxiter);\n"
            "int64_t hook(uint32_t r) { _g(1, 1); state_set(0, 0, 0, 0); return 0; }"
        )
        with pytest.raises(ValueError, match="state_set"):
            validator.validate(source)

    def test_digit_function_not_false_positive(
        self, validator: SourceValidator
    ) -> None:
        """util_sha512h declared and used should not report 'h' undeclared."""
        source = (
            "extern int32_t _g(uint32_t id, uint32_t maxiter);\n"
            "extern int64_t util_sha512h("
            "uint32_t w, uint32_t wl, uint32_t r, uint32_t rl);\n"
            "extern int64_t accept(uint32_t a, uint32_t b, int64_t c);\n"
            "extern int64_t state_set("
            "uint32_t a, uint32_t b, uint32_t c, uint32_t d);\n"
            "extern int64_t otxn_field(uint32_t a, uint32_t b, uint32_t c);\n"
            "int64_t hook(uint32_t r) {\n"
            "  _g(1, 1);\n"
            "  util_sha512h(0, 0, 0, 0);\n"
            "  otxn_field(0, 0, 0);\n"
            "  state_set(0, 0, 0, 0);\n"
            "  return accept(0, 0, 0);\n"
            "}"
        )
        # Should not raise — previously would fail with "Undeclared functions: h"
        validator.validate(source)
