# TagNTrac Factory Data Dashboard

A web dashboard for factory test data. Load the shipment list from the
factory (.csv, .xlsx or .xls), then:

- **Left column (controls):** load files, filter by test date and lot, pick
  the parameter and plot type (histogram, scatter, box plot, average), set
  spec limits (LSL/USL), and choose what counts as an outlier.
- **Top right (statistics + plot):** Cpk, Cp, mean, σ, mean ± 3σ, expected
  fallout (ppm) and yield, units out of spec, and the plot. Export the plot as
  interactive HTML, its data as CSV, or a PNG via the camera icon.
- **Bottom right (problem units):** units outside spec, beyond ±3σ, or that
  failed the factory test, each with the reason. Also the units you clicked
  on the plot, and all units. Every table exports to CSV or Excel.

## Current setup (prototype)

| Piece     | Now                          | Can switch to later                   |
|-----------|------------------------------|---------------------------------------|
| Database  | SQLite file (`data/tnt.db`)  | PostgreSQL (company server or cloud)  |
| App       | Streamlit, run on one machine| Same app on a server / cloud host     |
| Access    | Whoever can reach that machine | Company login in front of the app   |

The code doesn't care which database it uses. It reads `DATABASE_URL`, so
moving to PostgreSQL is a configuration change, not a rewrite.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Load a factory file (or use the upload box in the app's sidebar)
python -m tnt.loader path/to/shipment.xlsx

streamlit run app.py               # opens http://localhost:8501
```

Others on the same network can open `http://<this-machine's-IP>:8501`.

## Project layout

```
app.py            Streamlit dashboard
tnt/db.py         Database connection + table definitions
tnt/loader.py     Reads factory .csv/.xlsx/.xls files into the database
tnt/stats.py      Mean, sigma, Cp/Cpk, expected fallout
tests/            Run with: pytest
data/             Local database lives here (git-ignored)
```

## Data model

- **devices**: one row per unit (IMEI, serial numbers, lot, carton, test time)
- **measurements**: one row per unit per test value. New test items from the
  factory (sleep current, G-sensor, ...) become new rows; no schema change.
- **parameters**: the test items (dashboard widgets) with spec limits for Cpk
- **imports**: which file each load came from

## Notes on the factory data

- ESR values arrive as e.g. `167.0542m?`; the `?` is an Ω sign lost in the
  export. They are stored in mΩ.
- The date filter uses `Test_Time` (when the unit was tested). `Scan_Time`
  is when the carton was packed.
- Cpk needs LSL/USL for each parameter. ESR defaults to 100–200 mΩ; change
  the limits in the app and click "Save limits as default" to keep them.
  Leave one box blank for a one-sided limit.
- Expected fallout assumes a normal distribution; compare it with the
  "Actually out of spec" count.
- New test columns (temperature, voltage, sleep current...) are added in
  `MEASUREMENT_COLUMNS` in `tnt/loader.py`; they then appear in every chart.
- Factory data files are never committed. `.gitignore` excludes CSVs and
  database files.

## Moving to PostgreSQL later

1. `pip install "psycopg[binary]"`
2. Set `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/tnt`
3. Re-run the loader on the source CSVs (tables are created automatically).
