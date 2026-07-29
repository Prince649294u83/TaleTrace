"""Central logging configuration."""

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure consistent application logging."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )