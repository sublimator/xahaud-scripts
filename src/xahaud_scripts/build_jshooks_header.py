#!/usr/bin/env python3

import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import click

from xahaud_scripts.utils.paths import get_xahaud_root


def resolve_qjsc_path(cli_option, working_dir):
    """Resolve the qjsc binary path from CLI option, environment variable, or default.

    Priority: CLI option > QJSC_BINARY env var > ./qjsc default

    Args:
        cli_option: Path provided via --qjsc-binary CLI option
        working_dir: Working directory to resolve relative paths from

    Returns:
        Absolute path to the qjsc binary

    Raises:
        FileNotFoundError: If the binary doesn't exist
        PermissionError: If the binary is not executable
    """
    # Determine source and path with priority
    if cli_option:
        qjsc_path = cli_option
        source = "CLI option"
    elif os.environ.get("QJSC_BINARY"):
        qjsc_path = os.environ.get("QJSC_BINARY")
        source = "QJSC_BINARY environment variable"
    else:
        qjsc_path = "./qjsc"
        source = "default"

    # Resolve relative paths from working directory
    if not os.path.isabs(qjsc_path):
        qjsc_path = os.path.join(working_dir, qjsc_path)

    # Normalize the path
    qjsc_path = os.path.abspath(qjsc_path)

    # Check if binary exists
    if not os.path.exists(qjsc_path):
        raise FileNotFoundError(f"qjsc binary not found at {qjsc_path} (from {source})")

    # Check if binary is executable
    if not os.access(qjsc_path, os.X_OK):
        raise PermissionError(
            f"qjsc binary at {qjsc_path} is not executable (from {source})"
        )

    logging.info(f"Using qjsc binary from {source}: {qjsc_path}")
    return qjsc_path


def get_qjsc_hash(qjsc_path):
    """Generate a hash of the qjsc binary to include in the cache key."""
    try:
        with open(qjsc_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[
                :16
            ]  # Use first 16 chars of hash for brevity
    except Exception as e:
        logging.warning(f"Could not hash qjsc binary: {e}")
        return "unknown"


def convert_js_to_carray(js_file, js_content, qjsc_path):
    """
    Convert a JavaScript file to a C array using qjsc.
    Extracts just the hex bytes from the qjsc output.

    Args:
        js_file: Path to the JavaScript file to compile
        js_content: Content of the JavaScript file (for error reporting)
        qjsc_path: Path to the qjsc binary
    """
    try:
        # Run qjsc to compile the JavaScript file to C code
        result = subprocess.run(
            [qjsc_path, "-c", "-o", "/dev/stdout", js_file],
            capture_output=True,
            check=True,
        )

        # Check if we have any output
        if not result.stdout:
            logging.error(f"Error: qjsc produced no output for {js_file}")
            sys.exit(1)

        # Convert to text and extract just the hex values
        output_text = result.stdout.decode("utf-8", errors="replace")

        # Extract hexadecimal values from the array definition
        # Looking for patterns like 0x43, 0x0c, etc.
        hex_values = re.findall(r"0x[0-9A-Fa-f]{2}", output_text)

        # Format them as 0xXXU for the C array
        c_array = ", ".join([f"{hex_val}U" for hex_val in hex_values])

        return c_array

    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing qjsc: {e}, content: {js_content}")
        if e.stderr:
            logging.error(f"stderr: {e.stderr.decode('utf-8', errors='replace')}")
        sys.exit(1)


@click.command()
@click.option(
    "--canonical/--no-canonical",
    is_flag=True,
    default=True,
    help="Use canonical mode: output .sh in header and disable cache",
)
@click.option(
    "--log-level",
    default="error",
    type=click.Choice(
        ["debug", "info", "warning", "error", "critical"], case_sensitive=False
    ),
    help="Set logging level (default: error)",
)
@click.option(
    "--qjsc-binary",
    default=None,
    help="Path to qjsc binary (overrides QJSC_BINARY env var)",
)
def main(canonical, log_level, qjsc_binary):
    # Configure logging
    numeric_level = getattr(logging, log_level.upper(), None)
    logging.basicConfig(level=numeric_level, format="%(levelname)s: %(message)s")

    # Get the script directory and set working directory
    working_dir = os.path.join(get_xahaud_root(), "src/test/app")
    os.chdir(working_dir)

    # Resolve qjsc binary path
    try:
        qjsc_path = resolve_qjsc_path(qjsc_binary, working_dir)
    except (FileNotFoundError, PermissionError) as e:
        logging.error(str(e))
        sys.exit(1)

    # Set up paths
    wasmjs_dir = "generated/qjsb"
    input_file = "SetJSHook_test.cpp"
    output_file = "SetJSHook_wasm.h"

    # Determine header comment based on canonical mode
    header_comment = "build_test_jshooks.sh" if canonical else "build_test_jshooks.py"

    # Get hash of qjsc binary for cache key
    qjsc_hash = get_qjsc_hash(qjsc_path) if not canonical else None

    # Create cache directory if not in canonical mode
    cache_dir = None
    if not canonical:
        cache_dir = os.path.expanduser("~/.cache/jshooks-header")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

    # Create output directory if it doesn't exist
    Path(wasmjs_dir).mkdir(parents=True, exist_ok=True)

    logging.info(f"Processing {input_file} to generate {output_file}...")

    # Write header of output file
    with open(output_file, "w") as f:
        f.write(
            f"""
//This file is generated by {header_comment}
#ifndef SETHOOK_JSWASM_INCLUDED
#define SETHOOK_JSWASM_INCLUDED
#include <map>
#include <stdint.h>
#include <string>
#include <vector>
namespace ripple {{
namespace test {{
std::map<std::string, std::vector<uint8_t>> jswasm = {{"""
        )

    # Check if input file exists
    if not os.path.exists(input_file):
        logging.error(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)

    # Read input file content and convert newlines to form feeds as in the original script
    with open(input_file, encoding="utf-8") as f:
        content = f.read()

    content_with_form_feeds = content.replace("\n", "\f")

    # Get hooks using regex that matches the original bash script's grep command
    pattern = r'R"\[test\.hook\](.*?)\[test\.hook\]"'
    raw_matches = re.findall(pattern, content_with_form_feeds, re.DOTALL)

    if not raw_matches:
        logging.warning("Warning: No test hooks found in the input file.")
        sys.exit(0)

    # Process the matches similar to the original bash script's sed commands
    processed_matches = []
    for match in raw_matches:
        # Remove the opening tag
        processed = re.sub(r"^\(", "", match)
        # Remove the closing tag and any trailing whitespace/form feeds
        processed = re.sub(r"\)[\f \t]*$", "/*end*/", processed)
        processed_matches.append(processed)

    counter = 0
    for hook_content in processed_matches:
        logging.info(f"Processing hook {counter}...")

        with open(output_file, "a") as f:
            f.write(f"\n/* ==== WASM: {counter} ==== */\n")
            f.write('{ R"[test.hook](')

            # Remove the /*end*/ marker and convert form feeds back to newlines
            clean_content = (
                hook_content[:-7] if hook_content.endswith("/*end*/") else hook_content
            )
            clean_content = clean_content.replace("\f", "\n")
            f.write(clean_content)

            f.write(')[test.hook]",\n{')

        # Check if this is a WebAssembly module
        wat_count = len(re.findall(r"\(module", hook_content))
        if wat_count > 0:
            logging.error(f"Error: WebAssembly text format detected in hook {counter}")
            sys.exit(1)

        # Generate hash of the content for caching
        cache_file = None
        if not canonical and cache_dir:
            # Include qjsc hash in the cache key
            content_hash = hashlib.sha256(
                f"{qjsc_hash}:{hook_content}".encode()
            ).hexdigest()
            cache_file = os.path.join(cache_dir, f"hook-{content_hash}.c_array")

        # Check if we have a cached version and not in canonical mode
        if not canonical and cache_file and os.path.exists(cache_file):
            logging.info(f"Using cached version for hook {counter}")
            with open(cache_file) as cache:
                c_array = cache.read()
                with open(output_file, "a") as f:
                    f.write(c_array)
        else:
            logging.info(f"Compiling hook {counter}...")
            # Generate JS file with form feeds converted back to newlines
            # Add an extra newline at the end, as in paste.txt
            js_content = hook_content.replace("\f", "\n") + "\n"
            js_file = os.path.join(wasmjs_dir, f"test-{counter}-gen.js")
            with open(js_file, "w", encoding="utf-8") as f:
                f.write(js_content)

            try:
                # Use our internal function instead of the external script
                c_array_output = convert_js_to_carray(js_file, js_content, qjsc_path)

                # Cache the result if not in canonical mode
                if not canonical and cache_file:
                    with open(cache_file, "w") as cache:
                        cache.write(c_array_output)

                # Write to output file
                with open(output_file, "a") as f:
                    f.write(c_array_output)
            except Exception as e:
                logging.error(f"Compilation error for hook {counter}: {e}")
                sys.exit(1)

        with open(output_file, "a") as f:
            f.write("}},\n\n")

        counter += 1

    # Write footer of output file
    with open(output_file, "a") as f:
        f.write(
            """};
}
}
#endif\n"""
        )

    # Format the output file using clang-format
    logging.info("Formatting output file...")
    try:
        subprocess.run(["clang-format", "-i", output_file], check=True)
        logging.info(f"Successfully generated {output_file} with {counter} hooks")
    except subprocess.CalledProcessError:
        logging.warning(
            "Warning: clang-format failed, output might not be properly formatted"
        )
    except FileNotFoundError:
        logging.warning("Warning: clang-format not found, output will not be formatted")


if __name__ == "__main__":
    main()
