"""TagNTrac factory data dashboard.

    streamlit run app.py

Layout
  Left column  (sidebar)   : every control - filters, plot settings, spec limits,
                             outlier rules, and loading new files.
  Top right                : statistics + the plot, with export buttons.
  Bottom right             : units that are outliers / have issues, units picked
                             on the plot, and the full data matrix - each exportable.

Streamlit reruns this whole script from top to bottom every time someone
changes a widget, so each section simply reads the current widget values.
"""
import datetime as dt
import io

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text, update

from tnt.db import get_engine, parameters
from tnt.loader import SUPPORTED_TYPES, load_file, seed_parameters
from tnt.stats import capability

st.set_page_config(page_title="TagNTrac Factory Data", layout="wide")

# Chart colors (validated colorblind-safe palette). Meaning is never color-only:
# limit lines are labeled, and problem units are listed in a table.
BLUE = "#2a78d6"        # in-spec data
RED = "#d03b3b"         # out of spec / spec limits
INK = "#0b0b0b"         # mean line
GRAY = "#8a8984"        # +/- 3 sigma lines, fitted curve
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]   # up to 3 groups in "color by"

DEVICE_COLS = ["imei", "device_id", "lot_id", "carton_id", "inner_box_id",
               "battery_sn", "pcb_sn", "temp_humi_sn", "iccid", "sku",
               "hardware_version", "firmware_version", "test_status",
               "test_time", "scan_time"]
PLOT_TYPES = ["Histogram", "Scatter", "Box plot", "Average"]


# ---------------------------------------------------------------- helpers
@st.cache_resource
def engine():
    eng = get_engine()
    with eng.begin() as conn:
        seed_parameters(conn)
    return eng


def query(sql, **params):
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def fmt(x, digits=2):
    return "—" if x is None else f"{x:,.{digits}f}"


def to_excel(df: pd.DataFrame, sheet: str) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name=sheet[:31])
    return buf.getvalue()


def export_buttons(df: pd.DataFrame, name: str, key: str):
    """CSV + Excel download buttons for a table of units."""
    a, b, _ = st.columns([1, 1, 3])
    a.download_button("Export CSV", df.to_csv(index=False), file_name=f"{name}.csv",
                      mime="text/csv", key=f"csv_{key}", disabled=df.empty)
    b.download_button("Export Excel", to_excel(df, name), file_name=f"{name}.xlsx",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                      key=f"xlsx_{key}", disabled=df.empty)


def unit_columns(df, p, lead=()):
    """Order columns: any lead columns, IMEI, the parameter value, then the rest."""
    cols = list(lead) + ["imei", p] + DEVICE_COLS[1:]
    return df[[c for c in cols if c in df.columns]]


# ================================================================ LEFT COLUMN
# Everything the user can adjust lives in the sidebar.
sb = st.sidebar
sb.title("Controls")

with sb.expander("Load data", expanded=False):
    up = st.file_uploader("Factory shipment list", type=SUPPORTED_TYPES,
                          help="CSV or Excel (.xlsx / .xls)")
    if up and st.button("Load into database", type="primary"):
        try:
            res = load_file(up.getvalue(), up.name, engine())
            st.success(f"Loaded {res['loaded']:,} units. "
                       f"{res['total_devices']:,} units in database.")
            for w in res["warnings"][:10]:
                st.warning(w)
        except ValueError as e:
            st.error(str(e))

span = query("SELECT MIN(test_time) AS first, MAX(test_time) AS last, "
             "COUNT(*) AS n FROM devices")
if span.n[0] == 0:
    st.title("TagNTrac Factory Data")
    st.info("No data yet. Open **Load data** in the left column and load a shipment file.")
    st.stop()

# --- filters
sb.subheader("Filters")
first_day = dt.date.fromisoformat(span["first"][0][:10])
last_day = dt.date.fromisoformat(span["last"][0][:10])
all_lots = query("SELECT DISTINCT lot_id FROM devices ORDER BY lot_id").lot_id.tolist()
dates = sb.date_input("Tested between", (first_day, last_day),
                      min_value=first_day, max_value=last_day)
lots = sb.multiselect("Lots", all_lots, default=all_lots)
if not isinstance(dates, tuple) or len(dates) != 2 or not lots:
    st.title("TagNTrac Factory Data")
    st.info("Choose a start date, an end date, and at least one lot in the left column.")
    st.stop()
start, end = dates

# --- load the units that match the filters (the data matrix)
lot_slots = ", ".join(f":lot{i}" for i in range(len(lots)))
where = f"d.test_time BETWEEN :start AND :end AND d.lot_id IN ({lot_slots})"
args = dict(start=str(start), end=f"{end} 23:59:59",
            **{f"lot{i}": lot for i, lot in enumerate(lots)})
units = query(f"SELECT {', '.join('d.' + c for c in DEVICE_COLS)} "
              f"FROM devices d WHERE {where}", **args)
if units.empty:
    st.title("TagNTrac Factory Data")
    st.warning("No units match these filters.")
    st.stop()
values = query(f"""SELECT m.imei, m.parameter, m.value FROM measurements m
                   JOIN devices d ON d.imei = m.imei WHERE {where}""", **args)
wide = values.pivot_table(index="imei", columns="parameter", values="value")
matrix = units.merge(wide, left_on="imei", right_index=True, how="left")
matrix["test_time"] = pd.to_datetime(matrix["test_time"])
matrix = matrix.sort_values("test_time").reset_index(drop=True)

params = query("SELECT name, unit, lsl, usl FROM parameters").set_index("name")
measured = [p for p in params.index if p in matrix.columns]
not_yet = [p for p in params.index if p not in matrix.columns]
if not measured:
    st.warning("These units have no numeric test values yet.")
    st.stop()


def unit_of(p):
    u = params.unit.get(p)
    return u if isinstance(u, str) else ""


def saved_limit(p, col):
    v = params[col].get(p)
    return None if v is None or pd.isna(v) else float(v)


# --- plot settings
sb.subheader("Plot")
p = sb.selectbox("Parameter", measured, key="param",
                 help="Parameters without data yet: " + (", ".join(not_yet) or "none"))
plot_type = sb.radio("Plot type", PLOT_TYPES, key="plot_type", horizontal=True)
if plot_type == "Histogram":
    bins = sb.slider("Bins", 10, 80, 35)
else:
    x_options = ["test_time", "scan_time", "lot_id", "carton_id", "inner_box_id",
                 "test_status", "firmware_version", "hardware_version"] + \
                [m for m in measured if m != p]
    x = sb.selectbox("X axis", x_options, key="x_axis")
    group_opts = ["(none)"] + [c for c in ["lot_id", "test_status", "firmware_version",
                                           "hardware_version"]
                               if matrix[c].nunique() <= len(SERIES)]
    color_by = sb.selectbox("Color by", group_opts, key="color_by",
                            help="Only fields with 3 or fewer values, so colors stay "
                                 "distinguishable.")

# --- spec limits (start from saved defaults; blank = no limit on that side)
sb.subheader(f"Spec limits ({unit_of(p) or 'units'})")
st.session_state.setdefault(f"lsl_{p}", saved_limit(p, "lsl"))
st.session_state.setdefault(f"usl_{p}", saved_limit(p, "usl"))
l1, l2 = sb.columns(2)
lsl = l1.number_input("LSL", value=None, key=f"lsl_{p}", placeholder="none",
                      help="Lower spec limit. Leave blank for none.")
usl = l2.number_input("USL", value=None, key=f"usl_{p}", placeholder="none",
                      help="Upper spec limit. Leave blank for none.")
if sb.button("Save limits as default", width="stretch"):
    with engine().begin() as conn:
        conn.execute(update(parameters).where(parameters.c.name == p)
                     .values(lsl=lsl, usl=usl))
    st.toast(f"Saved {p} limits: LSL {fmt(lsl)}, USL {fmt(usl)}")
if lsl is not None and usl is not None and lsl >= usl:
    st.error("LSL must be lower than USL.")
    st.stop()

# --- what counts as an outlier / issue
sb.subheader("Outliers & issues")
use_spec = sb.checkbox("Outside spec limits", value=True)
use_sigma = sb.checkbox("Beyond mean ± 3σ", value=True,
                        help="Statistical outliers, even if still inside spec.")
use_status = sb.checkbox("Failed factory test", value=True,
                         help="Test_Status is anything other than 'Good'.")

# ================================================================ TOP RIGHT
st.title("TagNTrac Factory Data")
st.caption(f"{len(matrix):,} units · {matrix.lot_id.nunique()} lot(s) · "
           f"tested {start:%b %d} – {end:%b %d, %Y} · "
           f"factory test pass {(matrix.test_status == 'Good').mean():.1%}")

data = matrix.dropna(subset=[p]).reset_index(drop=True)
vals = data[p].to_numpy()
s = capability(vals, lsl, usl)

# --- statistics
r1 = st.columns(5)
r1[0].metric("Cpk", fmt(s["cpk"]), border=True,
             help="min(USL − mean, mean − LSL) / 3σ. ≥ 1.33 is a common target.")
r1[1].metric("Cp", fmt(s["cp"]), border=True,
             help="(USL − LSL) / 6σ: spread only, ignores centering.")
r1[2].metric(f"Mean ({unit_of(p)})", fmt(s["mean"]), border=True)
r1[3].metric("σ (std dev)", fmt(s["sigma"]), border=True)
r1[4].metric("Units", f"{s['n']:,}", border=True,
             help=f"Min {fmt(s['min'])}, max {fmt(s['max'])} {unit_of(p)}")
r2 = st.columns(5)
r2[0].metric("Mean − 3σ", fmt(s["minus_3s"]), border=True)
r2[1].metric("Mean + 3σ", fmt(s["plus_3s"]), border=True)
r2[2].metric("Expected fallout", "—" if s["expected_ppm"] is None
             else f"{s['expected_ppm']:,.0f} ppm", border=True,
             help="Predicted from a normal (bell-curve) fit: units expected outside "
                  "the limits, per million.")
r2[3].metric("Expected yield", "—" if s["expected_yield"] is None
             else f"{s['expected_yield']:.2f}%", border=True)
r2[4].metric("Out of spec", "—" if s["observed_out"] is None
             else f"{s['observed_out']:,}", border=True,
             help="Units actually outside the limits"
                  + ("" if s["observed_pct"] is None else f" ({s['observed_pct']:.2f}%)."))
if lsl is None and usl is None:
    st.caption("Enter an LSL and/or USL in the left column to calculate Cpk and fallout.")

# --- the plot
fig = go.Figure()
picked_units = pd.DataFrame()

if plot_type == "Histogram":
    edges = np.histogram_bin_edges(vals, bins=bins)
    counts, _ = np.histogram(vals, bins=edges)
    centers = (edges[:-1] + edges[1:]) / 2
    width = edges[1] - edges[0]
    out_bin = np.zeros(len(counts), dtype=bool)
    if lsl is not None:
        out_bin |= edges[:-1] < lsl
    if usl is not None:
        out_bin |= edges[1:] > usl
    fig.add_bar(
        x=centers, y=counts, width=width * 0.94,
        marker_color=np.where(out_bin, RED, BLUE),
        customdata=np.stack([edges[:-1], edges[1:]], axis=1),
        hovertemplate=(f"{p}: %{{customdata[0]:.1f}} – %{{customdata[1]:.1f}} {unit_of(p)}"
                       "<br>%{y} units<br><i>click to list these units</i><extra></extra>"),
        name=p)
    if s["sigma"]:
        lo = min(vals.min(), lsl if lsl is not None else vals.min())
        hi = max(vals.max(), usl if usl is not None else vals.max())
        xs = np.linspace(lo, hi, 300)
        ys = (len(vals) * width / (s["sigma"] * np.sqrt(2 * np.pi))
              * np.exp(-0.5 * ((xs - s["mean"]) / s["sigma"]) ** 2))
        fig.add_scatter(x=xs, y=ys, mode="lines", line=dict(color=GRAY, width=2),
                        hoverinfo="skip", name="Normal fit")

    def vline(xv, label, color, dash, where="top"):
        fig.add_vline(x=xv, line=dict(color=color, width=2, dash=dash),
                      annotation_text=label, annotation_position=where,
                      annotation_font_color=INK)

    vline(s["mean"], f"Mean {s['mean']:.1f}", INK, "solid")
    if s["sigma"]:
        vline(s["minus_3s"], "−3σ", GRAY, "dot", "bottom left")
        vline(s["plus_3s"], "+3σ", GRAY, "dot", "bottom right")
    if lsl is not None:
        vline(lsl, f"LSL {lsl:g}", RED, "dash", "top left")
        fig.add_vrect(x0=min(lsl, vals.min()) - width, x1=lsl,
                      fillcolor=RED, opacity=0.06, line_width=0)
    if usl is not None:
        vline(usl, f"USL {usl:g}", RED, "dash", "top right")
        fig.add_vrect(x0=usl, x1=max(usl, vals.max()) + width,
                      fillcolor=RED, opacity=0.06, line_width=0)
    fig.update_layout(xaxis_title=f"{p} ({unit_of(p)})", yaxis_title="Units",
                      showlegend=False, bargap=0)
    hint = ("Click a bar (shift-click to add more), or use the box-select tool "
            "(dashed square, top-right of the chart) to list those units below.")
    select_modes = ("points", "box")
else:
    d = data.copy()
    if x == "scan_time":
        d[x] = pd.to_datetime(d[x])
    time_x = x in ("test_time", "scan_time")
    numeric_x = time_x or x in measured
    groups = [(None, d)] if color_by == "(none)" else list(d.groupby(color_by))
    for gi, (gname, gd) in enumerate(groups):
        color = SERIES[gi % len(SERIES)]
        name = str(gname) if gname is not None else p
        if plot_type == "Scatter":
            fig.add_scattergl(
                x=gd[x], y=gd[p], mode="markers", name=name,
                marker=dict(color=color, size=8, opacity=0.6,
                            line=dict(color="white", width=1)),
                customdata=gd[["imei"]],
                hovertemplate=f"IMEI %{{customdata[0]}}<br>{x}: %{{x}}<br>{p}: %{{y:.2f}}"
                              "<extra></extra>")
        elif plot_type == "Box plot":
            bx = gd[x].dt.date.astype(str) if time_x else gd[x].astype(str)
            fig.add_box(x=bx, y=gd[p], name=name, marker_color=color, boxmean=True)
        else:  # Average
            key = gd[x].dt.date if time_x else gd[x]
            agg = gd.groupby(key)[p].agg(["mean", "count"]).reset_index()
            agg.columns = ["x", "mean", "count"]
            hover = (f"{x}: %{{x}}<br>average {p}: %{{y:.2f}}<br>%{{customdata}} units"
                     "<extra></extra>")
            if numeric_x:
                fig.add_scatter(x=agg.x, y=agg["mean"], mode="lines+markers", name=name,
                                line=dict(color=color, width=2), marker=dict(size=8),
                                customdata=agg["count"], hovertemplate=hover)
            else:
                fig.add_bar(x=agg.x.astype(str), y=agg["mean"], name=name,
                            marker_color=color, customdata=agg["count"],
                            hovertemplate=hover)
    for v, lab in ((lsl, "LSL"), (usl, "USL")):
        if v is not None:
            fig.add_hline(y=v, line=dict(color=RED, dash="dash", width=2),
                          annotation_text=f"{lab} {v:g}", annotation_position="top left",
                          annotation_font_color=INK)
    fig.update_layout(xaxis_title=x, yaxis_title=f"{p} ({unit_of(p)})",
                      showlegend=len(groups) > 1, boxmode="group")
    hint = ("Click points, or use the box/lasso select tools (top-right of the chart), "
            "to list those units below.") if plot_type == "Scatter" else \
           ("Hover to see how many units each point averages." if plot_type == "Average"
            else "")
    select_modes = ("points", "box", "lasso") if plot_type == "Scatter" else None

plot_title = f"{p} {plot_type.lower()}" + ("" if plot_type == "Histogram" else f" vs {x}")
fig.update_layout(
    title=dict(text=f"{plot_title} · {start:%b %d} – {end:%b %d, %Y}", x=0, font_size=15),
    height=460, margin=dict(t=50, b=40, l=10, r=10), plot_bgcolor="white",
    paper_bgcolor="white",
    xaxis=dict(gridcolor="#eeeeec"), yaxis=dict(gridcolor="#eeeeec"))

file_stub = f"{p}_{plot_type}_{start}_{end}".replace(" ", "_").lower()
plot_config = {"toImageButtonOptions": {"format": "png", "filename": file_stub, "scale": 2}}
if select_modes:
    event = st.plotly_chart(fig, on_select="rerun", selection_mode=select_modes,
                            key=f"plot_{p}_{plot_type}", width="stretch", config=plot_config)
else:
    event = None
    st.plotly_chart(fig, width="stretch", config=plot_config)
if hint:
    st.caption(hint)

# --- plot export
e1, e2, _ = st.columns([1, 1, 3])
e1.download_button("Export plot (HTML)", fig.to_html(include_plotlyjs=True, full_html=True),
                   file_name=f"{file_stub}.html", mime="text/html",
                   help="Interactive plot that opens in any browser, no install needed.")
e2.download_button("Export plot data (CSV)",
                   unit_columns(data, p).to_csv(index=False),
                   file_name=f"{file_stub}_data.csv", mime="text/csv",
                   help="The unit values behind this plot.")
st.caption("For a PNG image, click the camera icon at the top-right of the plot.")

# --- which units were picked on the plot
if event:
    sel = event.selection
    if plot_type == "Histogram":
        picked = {int(pt.get("point_index", pt.get("point_number")))
                  for pt in sel.get("points", [])
                  if pt.get("curve_number", 0) == 0
                  and pt.get("point_index", pt.get("point_number")) is not None}
        if picked:
            mask = np.zeros(len(data), dtype=bool)
            for b in picked:
                last = b == len(counts) - 1
                mask |= (vals >= edges[b]) & ((vals <= edges[b + 1]) if last
                                              else (vals < edges[b + 1]))
            picked_units = data[mask]
    else:
        imeis = {pt["customdata"][0] for pt in sel.get("points", []) if pt.get("customdata")}
        if imeis:
            picked_units = data[data.imei.isin(imeis)]

# ================================================================ BOTTOM RIGHT
# Every unit gets a list of reasons it's flagged; units with any reason are outliers.
reasons = pd.Series([[] for _ in range(len(data))], index=data.index)


def flag(mask, label):
    for i in data.index[mask]:
        reasons[i].append(label)


if use_spec:
    if lsl is not None:
        flag(vals < lsl, "Below LSL")
    if usl is not None:
        flag(vals > usl, "Above USL")
if use_sigma and s["sigma"]:
    flag(vals < s["minus_3s"], "Below −3σ")
    flag(vals > s["plus_3s"], "Above +3σ")
if use_status:
    flag((data.test_status != "Good").to_numpy(), "Failed factory test")

flagged = data[reasons.str.len() > 0].copy()
flagged.insert(0, "issue", reasons[flagged.index].str.join(", "))
flagged = unit_columns(flagged, p, lead=["issue"])

st.divider()
tab_out, tab_sel, tab_all = st.tabs([
    f"Outliers & issues ({len(flagged):,})",
    f"Selected on plot ({len(picked_units):,})",
    f"All units ({len(data):,})"])

with tab_out:
    if not (use_spec or use_sigma or use_status):
        st.info("Turn on at least one rule under **Outliers & issues** in the left column.")
    elif flagged.empty:
        st.success("No units are flagged by the rules selected in the left column.")
    else:
        counts_by = flagged["issue"].str.split(", ").explode().value_counts()
        st.caption(" · ".join(f"{k}: {v:,}" for k, v in counts_by.items()))
        st.dataframe(flagged, hide_index=True, width="stretch")
    export_buttons(flagged, f"{p}_outliers_{start}_{end}".lower(), "out")

with tab_sel:
    if picked_units.empty:
        st.info("Nothing selected. " + (hint or "Switch to a Histogram or Scatter plot "
                                               "to select units."))
    else:
        st.dataframe(unit_columns(picked_units, p), hide_index=True, width="stretch")
        export_buttons(unit_columns(picked_units, p), f"{p}_selected_{start}_{end}".lower(),
                       "sel")

with tab_all:
    cols = DEVICE_COLS[:8] + measured + DEVICE_COLS[8:]
    st.dataframe(matrix[cols], hide_index=True, width="stretch")
    export_buttons(matrix[cols], f"tnt_units_{start}_{end}", "all")
