"""Unit tests for database image detection."""

from __future__ import annotations

from backuplint.coverage import CoverageStatus, evaluate_coverage
from backuplint.database import is_database_image
from backuplint.models import Mount, MountType, ServiceMounts


def test_postgres_mysql_mariadb_mongo_detected() -> None:
    assert is_database_image("postgres:16-alpine")
    assert is_database_image("library/postgres")
    assert is_database_image("mysql:8")
    assert is_database_image("mariadb:11")
    assert is_database_image("mongo:7")
    assert is_database_image("mongodb")
    assert is_database_image("public.ecr.aws/docker/library/postgres:16")


def test_unrelated_and_similar_names_not_detected() -> None:
    assert not is_database_image("alpine:3.20")
    assert not is_database_image("postgres-exporter:latest")
    assert not is_database_image("mypostgres")
    assert not is_database_image("notmysql")
    assert not is_database_image(None)
    assert not is_database_image("")


def test_evaluate_coverage_adds_database_warning() -> None:
    services = [
        ServiceMounts(
            name="postgres",
            image="postgres:16-alpine",
            mounts=(
                Mount(
                    service="postgres",
                    type=MountType.BIND,
                    source="/srv/pg",
                    target="/var/lib/postgresql/data",
                ),
            ),
        )
    ]
    findings = evaluate_coverage(services, ("/srv",))
    assert any(f.status is CoverageStatus.PROTECTED for f in findings)
    warning = [f for f in findings if f.host_path == "database detected"]
    assert len(warning) == 1
    assert warning[0].status is CoverageStatus.UNSUPPORTED
    assert "consistent database backup" in warning[0].detail
