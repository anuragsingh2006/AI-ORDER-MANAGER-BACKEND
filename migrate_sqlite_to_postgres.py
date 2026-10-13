import argparse
import sqlite3
from pathlib import Path

from database import (
    DATABASE_URL_CONFIGURED,
    IS_POSTGRES,
    get_connection,
    get_table_columns,
    initialize_database,
)


TABLES = ("shopkeepers", "products", "orders")


def migrate(source_path: Path) -> None:
    if not DATABASE_URL_CONFIGURED or not IS_POSTGRES:
        raise RuntimeError(
            "Set DATABASE_URL to the target PostgreSQL database before migrating."
        )
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    initialize_database(seed_default_products=False)
    source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    source.row_factory = sqlite3.Row
    target = get_connection()
    migrated_counts = {}

    try:
        source_tables = {
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = set(TABLES) - source_tables
        if missing_tables:
            raise RuntimeError(
                "SQLite database is missing required tables: "
                + ", ".join(sorted(missing_tables))
            )

        target.execute(
            "LOCK TABLE shopkeepers, products, orders IN ACCESS EXCLUSIVE MODE"
        )
        for table in TABLES:
            count = target.execute(
                f"SELECT COUNT(*) AS row_count FROM {table}"
            ).fetchone()["row_count"]
            if count:
                raise RuntimeError(
                    f"Target table {table} is not empty; migration stopped "
                    "without changing its records."
                )

        for table in TABLES:
            source_columns = {
                row["name"]
                for row in source.execute(f"PRAGMA table_info({table})")
            }
            target_columns = get_table_columns(target, table)
            columns = sorted(source_columns & target_columns)
            if "id" not in columns:
                raise RuntimeError(
                    f"SQLite table {table} does not have the required id column."
                )

            select_columns = ", ".join(columns)
            insert_columns = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            rows = source.execute(
                f"SELECT {select_columns} FROM {table} ORDER BY id"
            )
            insert_statement = (
                f"INSERT INTO {table} ({insert_columns}) "
                f"VALUES ({placeholders})"
            )
            migrated_counts[table] = 0
            while batch := rows.fetchmany(500):
                target.executemany(
                    insert_statement,
                    [tuple(row[column] for column in columns) for row in batch],
                )
                migrated_counts[table] += len(batch)

        for table in TABLES:
            target.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence(?, 'id'),
                    COALESCE(MAX(id), 1),
                    COUNT(*) > 0
                )
                FROM
                """
                + table,
                (table,),
            )
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()

    initialize_database()
    print("SQLite records copied to PostgreSQL:")
    for table, count in migrated_counts.items():
        print(f"  {table}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy an existing SQLite order-manager database to PostgreSQL."
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=Path(__file__).resolve().parent / "order_manager.db",
        help="Path to the source SQLite database (default: backend/order_manager.db)",
    )
    args = parser.parse_args()
    migrate(args.sqlite_path)


if __name__ == "__main__":
    main()
