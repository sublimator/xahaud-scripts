#!/usr/bin/env python3
"""Thin shim: delegates to ``hookz build-test-hooks``.

If no INPUT_FILE is given and we're inside a xahaud worktree,
defaults to SetHook_test.cpp (hookz itself requires an explicit file).
"""

import os
import sys

import click

from xahaud_scripts.utils.paths import get_xahaud_root


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def main(ctx: click.Context, args: tuple[str, ...]) -> None:
    """Build test hooks — delegates to hookz build-test-hooks.

    All options are forwarded directly. If no INPUT_FILE is given,
    defaults to {xahaud_root}/src/test/app/SetHook_test.cpp.
    """
    cmd = ["hookz", "build-test-hooks"]
    args_list = list(args)

    # Check if any positional arg (non-option) was given as INPUT_FILE
    has_input_file = any(not a.startswith("-") and os.path.exists(a) for a in args_list)

    if not has_input_file:
        try:
            xahaud_root = get_xahaud_root()
            default_file = os.path.join(
                xahaud_root, "src", "test", "app", "SetHook_test.cpp"
            )
            if os.path.exists(default_file):
                args_list.insert(0, default_file)
            else:
                click.echo(f"Default file not found: {default_file}", err=True)
                sys.exit(1)
        except Exception:
            click.echo(
                "No INPUT_FILE given and not inside a xahaud worktree. "
                "Pass a file explicitly or set XAHAUD_ROOT.",
                err=True,
            )
            sys.exit(1)

    cmd.extend(args_list)
    os.execvp("hookz", cmd)


if __name__ == "__main__":
    main()
