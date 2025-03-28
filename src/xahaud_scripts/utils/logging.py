import logging

package_logger = logging.getLogger("xahaud_scripts")


def setup_logging(log_level: str, logger: logging.Logger) -> None:
    """Set up logging with the specified level."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    # Configure the root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Set our module logger level
    logger.setLevel(numeric_level)
    package_logger.setLevel(numeric_level)
    logger.info(f"Logging initialized at level {log_level.upper()}")


def make_logger(name: str) -> logging.Logger:
    """Create a logger with the specified name."""
    return logging.getLogger(name)
