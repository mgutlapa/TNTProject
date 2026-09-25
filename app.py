"""TagNTrac factory data dashboard.

    streamlit run app.py
"""
import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text, update

from tnt.db import get_engine, parameters
from tnt.loader import load_csv_text, seed_parameters
from tnt.stats import summarize

st.set_page_config(page_title="TagNTrac Factory Data", layout="wide")


@st.cache_resource
def engine():
    eng = get_engine()
    with eng.begin() as conn:
        seed_parameters(conn)
    return eng


def query(sql, **params):
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


# ---------- sidebar: load data ----------
with st.sidebar:
    st.header("Load shipment file")
    up = st.file_uploader("Factory shipment list (.csv)", type="csv")
    if up and st.button("Load into database", type="primary"):
        try:
            s = load_csv_text(up.getvalue().decode("utf-8-sig", errors="replace"), up.name, engine())
            st.success(f"Loaded {s['loaded']} units. {s['total_devices']} units in database.")
            for w in s["warnings"][:10]:
                st.warning(w)
        except ValueError as e:
            st.error(str(e))

    imp = query("SELECT filename, row_count, loaded_at FROM imports ORDER BY import_id DESC LIMIT 5")
    if not imp.empty:
        st.caption("Recent loads")
        st.dataframe(imp, hide_index=True, width="stretch")

st.title("TagNTrac Factory Data")

span = query("SELECT MIN(test_time) AS lo, MAX(test_time) AS hi, COUNT(*) AS n FROM devices")
if span.n[0] == 0:
    st.info("No data yet. Load a shipment CSV from the sidebar to get started.")
    st.stop()

# ---------- filters ----------
lo = dt.date.fromisoformat(span.lo[0][:10])
hi = dt.date.fromisoformat(span.hi[0][:10])
c1, c2 = st.columns([1, 1])
with c1:
    dates = st.date_input("Tested between", (lo, hi), min_value=lo, max_value=hi)
with c2:
    lots = query("SELECT DISTINCT lot_id FROM devices ORDER BY lot_id").lot_id.tolist()
    sel_lots = st.multiselect("Lots", lots, default=lots)

if not isinstance(dates, tuple) or len(dates) != 2:
    st.stop()
start, end = dates

params = query("""SELECT p.name, p.unit, p.test_stage, p.status, p.lsl, p.usl,
                         COUNT(m.value) AS n
                  FROM parameters p LEFT JOIN measurements m ON m.parameter = p.name
                  GROUP BY p.name, p.unit, p.test_stage, p.status, p.lsl, p.usl
                  ORDER BY p.name""")
available = params[params.n > 0].name.tolist()
pending = params[params.n == 0].name.tolist()

st.subheader("Parameters")
chosen = st.pills("Select parameters to display", available,
                  selection_mode="multi", default=available)
if pending:
    st.caption("Not in the data yet (factory hasn't exported these): " + ", ".join(pending))

if not chosen or not sel_lots:
    st.info("Pick at least one parameter and one lot.")
    st.stop()

# ---------- data matrix ----------
lot_ph = ", ".join(f":lot{i}" for i in range(len(sel_lots)))
par_ph = ", ".join(f":par{i}" for i in range(len(chosen)))
long = query(f"""
    SELECT d.imei, d.lot_id, d.carton_id, d.test_time, d.test_status,
           m.parameter, m.value
    FROM devices d JOIN measurements m ON m.imei = d.imei
    WHERE d.test_time BETWEEN :start AND :end
      AND d.lot_id IN ({lot_ph}) AND m.parameter IN ({par_ph})""",
    start=str(start), end=f"{end} 23:59:59",
    **{f"lot{i}": v for i, v in enumerate(sel_lots)},
    **{f"par{i}": v for i, v in enumerate(chosen)})

if long.empty:
    st.warning("No units match these filters.")
    st.stop()

ids = ["imei", "lot_id", "carton_id", "test_time", "test_status"]
matrix = long.pivot_table(index=ids, columns="parameter", values="value").reset_index()
matrix.columns.name = None
matrix = matrix.sort_values("test_time")

k1, k2, k3 = st.columns(3)
k1.metric("Units", f"{len(matrix):,}")
k2.metric("Lots", matrix.lot_id.nunique())
k3.metric("Passed", f"{(matrix.test_status == 'Good').mean():.1%}")

# ---------- capability summary ----------
st.subheader("Capability summary")
limits = {r.name: (None if pd.isna(r.lsl) else r.lsl, None if pd.isna(r.usl) else r.usl)
          for r in params.itertuples()}
summary = summarize(matrix[chosen], limits)
units = dict(zip(params.name, params.unit))
summary.insert(1, "Unit", summary.Parameter.map(units))
st.dataframe(summary, hide_index=True, width="stretch",
             column_config={c: st.column_config.NumberColumn(format="%.3f")
                            for c in ["Mean", "Std dev", "Min", "Max", "LSL", "USL", "Cpk"]})
if summary.Cpk.isna().any():
    st.caption("Cpk is blank until spec limits are entered below.")

with st.expander("Spec limits (LSL / USL)"):
    edit = st.data_editor(params[params.name.isin(chosen)][["name", "unit", "lsl", "usl"]],
                          hide_index=True, disabled=["name", "unit"], key="limits")
    if st.button("Save limits"):
        with engine().begin() as conn:
            for r in edit.itertuples():
                conn.execute(update(parameters).where(parameters.c.name == r.name).values(
                    lsl=None if pd.isna(r.lsl) else float(r.lsl),
                    usl=None if pd.isna(r.usl) else float(r.usl)))
        st.rerun()

# ---------- distributions ----------
st.subheader("Distributions")
cols = st.columns(min(len(chosen), 2))
for i, p in enumerate(chosen):
    base = alt.Chart(matrix).mark_bar().encode(
        x=alt.X(f"{p}:Q", bin=alt.Bin(maxbins=40), title=f"{p} ({units.get(p) or ''})"),
        y=alt.Y("count():Q", title="Units"))
    lsl, usl = limits.get(p, (None, None))
    rules = [alt.Chart(pd.DataFrame({"x": [v]})).mark_rule(color="red", strokeDash=[4, 3])
             .encode(x="x:Q") for v in (lsl, usl) if v is not None]
    cols[i % len(cols)].altair_chart(alt.layer(base, *rules), width="stretch")

# ---------- matrix ----------
st.subheader("Data matrix")
st.dataframe(matrix, hide_index=True, width="stretch")
st.download_button("Download as CSV", matrix.to_csv(index=False),
                   file_name=f"tnt_{start}_{end}.csv", mime="text/csv")
