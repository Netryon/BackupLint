"""Detect common database container images for backup warnings."""

from __future__ import annotations

_DATABASE_NAMES = frozenset(
    {
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
        "mongo",
        "mongodb",
    }
)


def image_basename(image: str) -> str:
    """Return the image repository name without registry path or tag/digest."""
    name = image.strip()
    if not name:
        return ""
    name = name.split("@", 1)[0]
    name = name.rsplit(":", 1)[0]
    return name.rsplit("/", 1)[-1]


def is_database_image(image: str | None) -> bool:
    """Return True for common PostgreSQL/MySQL/MariaDB/MongoDB images."""
    if not image:
        return False
    return image_basename(image).lower() in _DATABASE_NAMES


def database_warning_message(image: str) -> str:
    return (
        "Database workload detected "
        f"({image_basename(image)}). "
        "Verify that your backup method provides a consistent database backup."
    )
