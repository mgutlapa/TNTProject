"""Database connection and table definitions.

The app talks to whatever DATABASE_URL points at:
  - default:   sqlite:///data/tnt.db              (a local file, no setup)
  - later:     postgresql+psycopg://user:pass@host/tnt   (company server / cloud)
Switching is a config change, not a code change.
"""
import os
from pathlib import Path

from sqlalchemy import (Column, Float, ForeignKey, Integer, MetaData, String,
                        Table, create_engine, event)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = f"sqlite:///{ROOT / 'data' / 'tnt.db'}"

metadata = MetaData()

imports = Table(
    "imports", metadata,
    Column("import_id", Integer, primary_key=True, autoincrement=True),
    Column("filename", String),
    Column("row_count", Integer),
    Column("loaded_at", String),
)

# One row per physical unit: identity and traceability.
devices = Table(
    "devices", metadata,
    Column("imei", String, primary_key=True),
    Column("device_id", String, unique=True, nullable=False),
    Column("iccid", String),
    Column("temp_humi_sn", String),
    Column("pcb_sn", String),
    Column("battery_sn", String),
    Column("sku", String),
    Column("hardware_version", String),
    Column("firmware_version", String),
    Column("lot_id", String, nullable=False, index=True),
    Column("carton_id", String),
    Column("inner_box_id", String),
    Column("test_status", String),
    Column("tester_fw", String),
    Column("test_time", String, index=True),   # ISO text: 'YYYY-MM-DD HH:MM:SS'
    Column("scan_time", String),
    Column("import_id", Integer, ForeignKey("imports.import_id")),
)

# Test items = dashboard widgets. LSL/USL feed the Cpk calculation.
parameters = Table(
    "parameters", metadata,
    Column("name", String, primary_key=True),
    Column("unit", String),
    Column("test_stage", String),
    Column("status", String),
    Column("lsl", Float),
    Column("usl", Float),
)

# Tall format: one row per device per parameter. New test items need no schema change.
measurements = Table(
    "measurements", metadata,
    Column("imei", String, ForeignKey("devices.imei"), primary_key=True),
    Column("parameter", String, ForeignKey("parameters.name"), primary_key=True, index=True),
    Column("value", Float),
    Column("raw_value", String),
)


def get_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_con, _):
            dbapi_con.execute("PRAGMA foreign_keys = ON")
    metadata.create_all(engine)
    return engine
