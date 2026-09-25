"""Process capability statistics."""
import math

import pandas as pd


def cpk(values: pd.Series, lsl: float | None, usl: float | None) -> float | None:
    """Cpk = min(USL - mean, mean - LSL) / (3 * sigma), using sample std dev.
    With only one limit, the one-sided index (Cpu or Cpl) is returned."""
    v = pd.to_numeric(values, errors="coerce").dropna()
    if len(v) < 2 or (lsl is None and usl is None):
        return None
    mean, sigma = v.mean(), v.std(ddof=1)
    if sigma == 0 or math.isnan(sigma):
        return None
    sides = []
    if usl is not None:
        sides.append((usl - mean) / (3 * sigma))
    if lsl is not None:
        sides.append((mean - lsl) / (3 * sigma))
    return min(sides)


def summarize(matrix: pd.DataFrame, limits: dict) -> pd.DataFrame:
    """One row per parameter: n, mean, std, min, max, LSL, USL, Cpk."""
    out = []
    for p in matrix.columns:
        v = pd.to_numeric(matrix[p], errors="coerce").dropna()
        lsl, usl = limits.get(p, (None, None))
        out.append({
            "Parameter": p, "n": len(v),
            "Mean": v.mean(), "Std dev": v.std(ddof=1),
            "Min": v.min(), "Max": v.max(),
            "LSL": lsl, "USL": usl, "Cpk": cpk(v, lsl, usl),
        })
    return pd.DataFrame(out)
