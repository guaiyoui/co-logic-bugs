"""Target DBMS runners. Each runner executes SQL and returns normalized results."""

from targets.postgres_runner import PostgresRunner

__all__ = ["PostgresRunner"]
