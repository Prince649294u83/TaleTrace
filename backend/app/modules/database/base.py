"""SQLAlchemy declarative base for TaleTrace models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata registry for all database tables."""
