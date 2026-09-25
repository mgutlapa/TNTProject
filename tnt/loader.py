"""Load a factory shipment-list CSV into the database.

    python -m tnt.loader path/to/shipment.csv

Safe to re-run: units are keyed on IMEI, so re-loading a file updates rows
instead of duplicating them.
"""
import csv
import io
import re
import sys
from datetime import datetime

from sqlalchemy import insert, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import devices, get_engine, imports, measurements, parameters

REQUIRED = ["SKU", "Carton_ID", "Inner_Box_ID", "IMEI", "Device_ID", "ICCID",
            "Temp_Humi_SN", "PCB_SN", "Battery_SN", "ESR", "Scan_Time", "Lot_ID",
            "Firmware_Version", "Hardware_Version", "Test_Status", "Tester_FW", "Test_Time"]

DEVICE_COLUMNS = {  # CSV column -> database column
    "IMEI": "imei", "Device_ID": "device_id", "ICCID": "iccid",
    "Temp_Humi_SN": "temp_humi_sn", "PCB_SN": "pcb_sn", "Battery_SN": "battery_sn",
    "SKU": "sku", "Hardware_Version": "hardware_version",
    "Firmware_Version": "firmware_version", "Lot_ID": "lot_id",
    "Carton_ID": "carton_id", "Inner_Box_ID": "inner_box_id",
    "Test_Status": "test_status", "Tester_FW": "tester_fw",
    "Test_Time": "test_time", "Scan_Time": "scan_time",
}

# Numeric test columns in the CSV -> parameter name.
# When the factory starts exporting more test items, add them here.
MEASUREMENT_COLUMNS = {
    "ESR": "ESR",
    "Battery_Voltage": "BATTERY VOLTAGE",
    "Sleep_Current": "SLEEP CURRENT",
}

# All test items from the factory's "shipping list status" sheet.
KNOWN_PARAMETERS = [
    # name, unit, test stage, export status
    ("ESR", "mΩ", "Battery test", "Imported"),
    ("BATTERY VOLTAGE", "V", "Final test", "Imported"),
    ("G-sensor", None, "Final test", "Imported"),
    ("SLEEP CURRENT", None, "PCBA test", "To be developed"),
    ("BT SCAN", None, "PCBA test", "To be developed"),
    ("WIFI SCAN", None, "Final test", "To be developed"),
    ("WDT", None, "Final test", "To be developed"),
    ("Light", None, "Final test", "To be developed"),
    ("Temphumi", None, "Final test", "To be developed"),
    ("Bar", None, "Final test", "To be developed"),
    ("INACTIVATION RESULT", None, "Final test", "To be developed"),
    ("LED", None, None, "Not yet tested"),
]


def parse_esr(raw: str) -> float | None:
    """'167.0542m?' -> 167.0542 mΩ. (The '?' is an Ω sign lost in the export.)"""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([mu]?)", raw or "")
    if not m:
        return None
    value, prefix = float(m.group(1)), m.group(2)
    return {"m": value, "u": value / 1000}.get(prefix, value * 1000)


def parse_number(raw: str) -> float | None:
    m = re.match(r"^\s*(-?[0-9]*\.?[0-9]+)", raw or "")
    return float(m.group(1)) if m else None


def _upsert(conn, table, rows, keys):
    if not rows:
        return
    ins = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    stmt = ins(table)
    update = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys}
    conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=update), rows)


def seed_parameters(conn):
    """Add any missing test items; never overwrite spec limits someone entered."""
    existing = {r[0] for r in conn.execute(select(parameters.c.name))}
    new = [dict(name=n, unit=u, test_stage=s, status=st)
           for n, u, s, st in KNOWN_PARAMETERS if n not in existing]
    if new:
        conn.execute(insert(parameters), new)


def load_csv_text(text: str, filename: str, engine=None) -> dict:
    """Load CSV content. Returns a summary dict (rows, devices, warnings)."""
    engine = engine or get_engine()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"File is missing columns: {', '.join(missing)}")
    rows = list(reader)

    warnings, dev_rows, meas_rows = [], [], []
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        r = {k: (v or "").strip() for k, v in r.items() if k}
        if not r["IMEI"]:
            warnings.append(f"line {i}: blank IMEI, skipped")
            continue
        dev_rows.append({db: r[csv_col] for csv_col, db in DEVICE_COLUMNS.items()})
        for col, param in MEASUREMENT_COLUMNS.items():
            if col not in r:
                continue
            value = parse_esr(r[col]) if col == "ESR" else parse_number(r[col])
            if value is None:
                warnings.append(f"line {i}: could not read {col}={r[col]!r}")
            meas_rows.append(dict(imei=r["IMEI"], parameter=param,
                                  value=value, raw_value=r[col]))

    with engine.begin() as conn:
        seed_parameters(conn)
        import_id = conn.execute(insert(imports).values(
            filename=filename, row_count=len(rows),
            loaded_at=datetime.now().isoformat(timespec="seconds"))).inserted_primary_key[0]
        for d in dev_rows:
            d["import_id"] = import_id
        _upsert(conn, devices, dev_rows, ["imei"])
        _upsert(conn, measurements, meas_rows, ["imei", "parameter"])
        total = conn.execute(select(func.count()).select_from(devices)).scalar()

    return {"rows": len(rows), "loaded": len(dev_rows),
            "total_devices": total, "warnings": warnings}


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tnt.loader path/to/shipment.csv")
    path = sys.argv[1]
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        summary = load_csv_text(f.read(), path.split("/")[-1])
    print(f"Loaded {summary['loaded']} units from {path} "
          f"({summary['total_devices']} units now in the database)")
    for w in summary["warnings"][:20]:
        print("  WARNING", w)


if __name__ == "__main__":
    main()
