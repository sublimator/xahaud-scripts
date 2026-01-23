"""WASM hook compilation library.

Provides tools for compiling C and WAT source code to WASM bytecode.

Example:
    from xahaud_scripts.hooks import WasmCompiler

    compiler = WasmCompiler()
    bytecode = compiler.compile(source_code)
"""

from xahaud_scripts.hooks.compiler import (
    BinaryChecker,
    CompilationCache,
    SourceValidator,
    WasmCompiler,
)

__all__ = [
    "BinaryChecker",
    "CompilationCache",
    "SourceValidator",
    "WasmCompiler",
]
