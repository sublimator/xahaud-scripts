"""CLI command handlers.

This package contains implementations for CLI commands that are
complex enough to warrant their own modules.
"""

from xahaud_scripts.testnet.cli_handlers.logs_search import logs_search_handler

__all__ = ["logs_search_handler"]
