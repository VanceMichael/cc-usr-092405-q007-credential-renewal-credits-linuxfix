"""Alembic 迁移产物与 SQLAlchemy metadata 必须结构一致。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

pytest.importorskip("alembic")

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_migration_schema_matches_metadata(tmp_path):
    db_path = tmp_path / "migrated.sqlite3"
    env = dict(os.environ, DATABASE_PATH=str(db_path))
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True,
    )

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    migrated = {t: {c["name"] for c in inspector.get_columns(t)}
                for t in inspector.get_table_names()
                if t != "alembic_version"}

    from skill_engine.models import metadata
    model_tables = set(metadata.tables)
    assert set(migrated) == model_tables
    for table_name in model_tables:
        expected = {c.name for c in metadata.tables[table_name].columns}
        assert migrated[table_name] == expected, f"表 {table_name} 列与迁移不一致"
