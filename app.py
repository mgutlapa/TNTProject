"""TagNTrac factory data dashboard (step 1: dates, lots, data matrix).

    streamlit run app.py
"""
import datetime as dt

import pandas as pd
import streamlit as st
from sqlalchemy import text

from tnt.db import get_engine
from tnt.loader import load_csv_text

st.set_page_config(page_title="TagNTrac Factory Data", layout="wide")


# Connect to the database once and reuse the connection on every rerun.
@st.cache_resource
def engine():
    return get_engine()


def query(sql, **params):
    """Run a SQL query and return the result as a pandas DataFrame."""
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


# ---------- Sidebar: get a CSV into the database ----------
with st.sidebar:
    st.header("Load shipment file")
    up = st.file_uploader("Factory shipment list (.csv)", type="csv")
    if up and st.button("Load into database", type="primary"):
        try:
            result = load_csv_text(up.getvalue().decode("utf-8-sig", errors="replace"),
                                   up.name, engine())
            st.success(f"Loaded {result['loaded']} units. "
                       f"{result['total_devices']} units in database.")
        except ValueError as e:
            st.error(str(e))

st.title("TagNTrac Factory Data")

# If the database is empty, stop here and ask for a file.
span = query("SELECT MIN(test_time) AS first, MAX(test_time) AS last, "
             "COUNT(*) AS n FROM devices")
if span.n[0] == 0:
    st.info("No data yet. Load a shipment CSV from the sidebar to get started.")
    st.stop()

# ---------- Filters: test date range and lot ----------
first_day = dt.date.fromisoformat(span["first"][0][:10])
last_day = dt.date.fromisoformat(span["last"][0][:10])
all_lots = query("SELECT DISTINCT lot_id FROM devices ORDER BY lot_id").lot_id.tolist()

col1, col2 = st.columns(2)
with col1:
    dates = st.date_input("Tested between", (first_day, last_day),
                          min_value=first_day, max_value=last_day)
with col2:
    lots = st.multiselect("Lots", all_lots, default=all_lots)

# The date picker returns one date while the user is mid-selection; wait for both.
if not isinstance(dates, tuple) or len(dates) != 2 or not lots:
    st.info("Choose a start date, an end date, and at least one lot.")
    st.stop()
start, end = dates

# ---------- Query: units in range, joined with their test values ----------
lot_slots = ", ".join(f":lot{i}" for i in range(len(lots)))   # ":lot0, :lot1, ..."
rows = query(f"""
    SELECT d.imei, d.device_id, d.lot_id, d.carton_id, d.inner_box_id,
           d.battery_sn, d.test_status, d.test_time,
           m.parameter, m.value
    FROM devices d
    LEFT JOIN measurements m ON m.imei = d.imei
    WHERE d.test_time BETWEEN :start AND :end
      AND d.lot_id IN ({lot_slots})""",
    start=str(start), end=f"{end} 23:59:59",
    **{f"lot{i}": lot for i, lot in enumerate(lots)})

if rows.empty:
    st.warning("No units match these filters.")
    st.stop()

# ---------- Data matrix: one row per unit, one column per test parameter ----------
id_cols = ["imei", "device_id", "lot_id", "carton_id", "inner_box_id",
           "battery_sn", "test_status", "test_time"]
matrix = (rows.pivot_table(index=id_cols, columns="parameter", values="value")
              .reset_index()
              .sort_values("test_time"))
matrix.columns.name = None

st.subheader(f"Data matrix ({len(matrix):,} units)")
st.dataframe(matrix, hide_index=True, width="stretch")
