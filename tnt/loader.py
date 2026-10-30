"""Load a factory shipment list (.csv, .xlsx or .xls) into the database.

    python -m tnt.loader path/to/shipment.xlsx

Safe to re-run: units are keyed on IMEI, so re-loading a file updates rows
instead of duplicating them.
"""
import io
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import devices, get_engine, imports, measurements, parameters

SUPPORTED_TYPES = ["csv", "xlsx", "xls"]

REQUIRED = ["SKU", "Carton_ID", "Inner_Box_ID", "IMEI", "Device_ID", "ICCID",
            "Temp_Humi_SN", "PCB_SN", "Battery_SN", "ESR", "Scan_Time", "Lot_ID",
            "Firmware_Version", "Hardware_Version", "Test_Status", "Tester_FW", "Test_Time"]

DEVICE_COLUMNS = {  # file column -> database column
    "IMEI": "imei", "Device_ID": "device_id", "ICCID": "iccid",
    "Temp_Humi_SN": "temp_humi_sn", "PCB_SN": "pcb_sn", "Battery_SN": "battery_sn",
    "SKU": "sku", "Hardware_Version": "hardware_version",
    "Firmware_Version": "firmware_version", "Lot_ID": "lot_id",
    "Carton_ID": "carton_id", "Inner_Box_ID": "inner_box_id",
    "Test_Status": "test_status", "Tester_FW": "tester_fw",
    "Test_Time": "test_time", "Scan_Time": "scan_time",
}
DATE_COLUMNS = ["Test_Time", "Scan_Time"]

# Numeric test columns in the file -> parameter name.
# When the factory starts exporting more test items (temperature, sleep current,
# voltage...), add the column here and it shows up in every chart automatically.
MEASUREMENT_COLUMNS = {
    "ESR": "ESR",
    "Battery_Voltage": "BATTERY VOLTAGE",
    "Sleep_Current": "SLEEP CURRENT",
    "Temperature": "Temphumi",
}

# All test items from the factory's "shipping list status" sheet.
# name, unit, test stage, export status, default LSL, default USL
KNOWN_PARAMETERS = [
    ("ESR", "mΩ", "Battery test", "Imported", 100.0, 200.0),
    ("BATTERY VOLTAGE", "V", "Final test", "Imported", None, None),
    ("G-sensor", None, "Final test", "Imported", None, None),
    ("SLEEP CURRENT", None, "PCBA test", "To be developed", None, None),
    ("BT SCAN", None, "PCBA test", "To be developed", None, None),
    ("WIFI SCAN", None, "Final test", "To be developed", None, None),
    ("WDT", None, "Final test", "To be developed", None, None),
    ("Light", None, "Final test", "To be developed", None, None),
    ("Temphumi", None, "Final test", "To be developed", None, None),
    ("Bar", None, "Final test", "To be developed", None, None),
    ("INACTIVATION RESULT", None, "Final test", "To be developed", None, None),
    ("LED", None, None, "Not yet tested", None, None),
]


# ---------- parsing single values ----------
def parse_esr(raw: str) -> float | None:
    """'167.0542m?' or '167.0542mΩ' -> 167.0542 (mΩ). Plain ohms are converted."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([mu]?)", raw or "")
    if not m:
        return None
    value, prefix = float(m.group(1)), m.group(2)
    return {"m": value, "u": value / 1000}.get(prefix, value * 1000)


def parse_number(raw: str) -> float | None:
    m = re.match(r"^\s*(-?[0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)", raw or "")
    return float(m.group(1)) if m else None


# ---------- reading files ----------
def read_table(data: bytes, filename: str) -> pd.DataFrame:
    """Read a .csv / .xlsx / .xls file into a DataFrame of strings."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext == "csv":
        try:
            df = pd.read_csv(io.BytesIO(data), dtype=str, encoding="utf-8-sig",
                             keep_default_na=False)
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(data), dtype=str, encoding="latin-1",
                             keep_default_na=False)
    elif ext in ("xlsx", "xls"):
        engine = "openpyxl" if ext == "xlsx" else "xlrd"
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=object,
                               engine=engine)
        # Use the first sheet that has the shipment columns.
        df = next((s for s in sheets.values()
                   if set(REQUIRED) <= {str(c).strip() for c in s.columns}),
                  next(iter(sheets.values())))
        df = df.apply(lambda col: col.map(_excel_cell_to_text))
    else:
        raise ValueError(f"Unsupported file type '.{ext}'. Use .csv, .xlsx or .xls.")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _excel_cell_to_text(v) -> str:
    """Excel stores numbers and dates as typed values; turn them back into text."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))            # IMEIs etc. stored as numbers
    return str(v)


def _iso_datetime(raw: str) -> str:
    """Normalise any date format to 'YYYY-MM-DD HH:MM:SS' so date filters work."""
    if not raw:
        return ""
    ts = pd.to_datetime(raw, errors="coerce")
    return raw if pd.isna(ts) else ts.strftime("%Y-%m-%d %H:%M:%S")


# ---------- writing to the database ----------
def _upsert(conn, table, rows, keys):
    if not rows:
        return
    ins = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    for i in range(0, len(rows), 500):              # batches keep SQLite happy
        stmt = ins(table)
        update_cols = {c.name: stmt.excluded[c.name]
                       for c in table.columns if c.name not in keys}
        conn.execute(stmt.on_conflict_do_update(index_elements=keys, set_=update_cols),
                     rows[i:i + 500])


def seed_parameters(conn):
    """Add missing test items, and fill default spec limits where none are set.
    Limits someone has already entered are never overwritten."""
    existing = {r.name: r for r in conn.execute(select(parameters))}
    for name, unit, stage, status, lsl, usl in KNOWN_PARAMETERS:
        if name not in existing:
            conn.execute(insert(parameters).values(
                name=name, unit=unit, test_stage=stage, status=status, lsl=lsl, usl=usl))
        elif existing[name].lsl is None and existing[name].usl is None and (lsl or usl):
            conn.execute(update(parameters).where(and_(parameters.c.name == name))
                         .values(lsl=lsl, usl=usl))


def load_dataframe(df: pd.DataFrame, filename: str, engine=None) -> dict:
    """Validate and load a shipment table. Returns a summary dict."""
    engine = engine or get_engine()
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"File is missing columns: {', '.join(missing)}")

    warnings, dev_rows, meas_rows = [], [], []
    for i, r in enumerate(df.fillna("").astype(str).to_dict("records"), start=2):
        r = {k: v.strip() for k, v in r.items()}
        if not r["IMEI"]:
            warnings.append(f"row {i}: blank IMEI, skipped")
            continue
        for col in DATE_COLUMNS:
            r[col] = _iso_datetime(r[col])
        dev_rows.append({db: r[col] for col, db in DEVICE_COLUMNS.items()})
        for col, param in MEASUREMENT_COLUMNS.items():
            if col not in r:
                continue
            value = parse_esr(r[col]) if col == "ESR" else parse_number(r[col])
            if value is None:
                warnings.append(f"row {i}: could not read {col}={r[col]!r}")
            meas_rows.append(dict(imei=r["IMEI"], parameter=param,
                                  value=value, raw_value=r[col]))

    with engine.begin() as conn:
        seed_parameters(conn)
        import_id = conn.execute(insert(imports).values(
            filename=filename, row_count=len(df),
            loaded_at=datetime.now().isoformat(timespec="seconds"))).inserted_primary_key[0]
        for d in dev_rows:
            d["import_id"] = import_id
        _upsert(conn, devices, dev_rows, ["imei"])
        _upsert(conn, measurements, meas_rows, ["imei", "parameter"])
        total = conn.execute(select(func.count()).select_from(devices)).scalar()

    return {"rows": len(df), "loaded": len(dev_rows),
            "total_devices": total, "warnings": warnings}


def load_file(data: bytes, filename: str, engine=None) -> dict:
    return load_dataframe(read_table(data, filename), filename, engine)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tnt.loader path/to/shipment.(csv|xlsx|xls)")
    path = Path(sys.argv[1])
    summary = load_file(path.read_bytes(), path.name)
    print(f"Loaded {summary['loaded']} units from {path.name} "
          f"({summary['total_devices']} units now in the database)")
    for w in summary["warnings"][:20]:
        print("  WARNING", w)


if __name__ == "__main__":
    main()
