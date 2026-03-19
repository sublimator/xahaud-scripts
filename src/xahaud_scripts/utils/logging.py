import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

package_logger = logging.getLogger("xahaud_scripts")

_SCENARIO_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def setup_logging(log_level: str, logger: logging.Logger) -> None:
    """Set up logging with the specified level."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Configure the root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s [%(filename)s:%(lineno)d]",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Set our module logger level
    logger.setLevel(numeric_level)
    package_logger.setLevel(numeric_level)
    logger.info(f"Logging initialized at level {log_level.upper()}")


def make_logger(name: str) -> logging.Logger:
    """Create a logger with the specified name."""
    return logging.getLogger(name)


@contextmanager
def scenario_file_logging(
    *log_files: tuple[Path, str],
    py_log_specs: list[str] | None = None,
    logger_name: str = "xahaud_scripts.testnet",
) -> Iterator[list[logging.FileHandler]]:
    """Context manager for scenario file logging with optional py_log_specs.

    Creates file handlers, attaches them to the scenario logger, applies
    --with-py-logs specs, and cleans everything up on exit.

    Args:
        log_files: Tuples of (path, mode) for each file handler to create.
        py_log_specs: Optional list of "logger.name=LEVEL" specs to lower
            specific loggers (file only, console stays unchanged).
        logger_name: Base logger to attach handlers to.

    Yields:
        List of created FileHandler instances (same order as log_files).
    """
    scenario_logger = logging.getLogger(logger_name)
    formatter = logging.Formatter(_SCENARIO_FORMAT)

    handlers: list[logging.FileHandler] = []
    for path, mode in log_files:
        h = logging.FileHandler(path, mode=mode)
        h.setFormatter(formatter)
        h.setLevel(logging.DEBUG)
        scenario_logger.addHandler(h)
        handlers.append(h)

    lowered_loggers: list[logging.Logger] = []
    if py_log_specs:
        _logger = logging.getLogger(__name__)
        for spec in py_log_specs:
            if "=" not in spec:
                _logger.error(
                    f"Invalid --with-py-logs format: {spec} (expected name=LEVEL)"
                )
                continue
            name, level_str = spec.split("=", 1)
            level = getattr(logging, level_str.upper(), None)
            if not isinstance(level, int):
                _logger.error(f"Invalid log level: {level_str}")
                continue
            target = logging.getLogger(name)
            target.setLevel(level)
            for h in handlers:
                if h not in target.handlers:
                    target.addHandler(h)
            lowered_loggers.append(target)
            _logger.info(f"File logging: {name}={level_str.upper()}")

        # Pin console handlers so lowered levels don't flood console
        for rh in logging.root.handlers:
            if rh.level == logging.NOTSET:
                rh.setLevel(logging.root.level)

    try:
        yield handlers
    finally:
        for h in handlers:
            scenario_logger.removeHandler(h)
        for lg in lowered_loggers:
            for h in handlers:
                lg.removeHandler(h)
        for h in handlers:
            h.close()
