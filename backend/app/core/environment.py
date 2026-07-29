"""Environment file loading utilities."""

from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    """Load the repository .env file, if present, before settings are built."""

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)