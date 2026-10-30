"""Process capability statistics: mean, sigma, Cp/Cpk and expected fallout."""
import math

import pandas as pd


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def capability(values, lsl: float | None, usl: float | None) -> dict:
    """Summary statistics for one parameter against its spec limits.

    Cpk = min(USL - mean, mean - LSL) / (3 * sigma)
        With only one limit set, the one-sided index (Cpu or Cpl) is used.
    Expected fallout assumes the data is normally distributed: it is the
    area of the fitted bell curve that falls outside the limits, in parts
    per million (ppm). Observed fallout is the actual count outside limits.
    """
    v = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    n = len(v)
    out = dict(n=n, mean=None, sigma=None, minus_3s=None, plus_3s=None,
               min=None, max=None, cp=None, cpk=None,
               expected_ppm=None, expected_yield=None,
               observed_out=None, observed_pct=None)
    if n == 0:
        return out
    mean = float(v.mean())
    sigma = float(v.std(ddof=1)) if n > 1 else 0.0
    out.update(mean=mean, sigma=sigma, min=float(v.min()), max=float(v.max()),
               minus_3s=mean - 3 * sigma, plus_3s=mean + 3 * sigma)

    if lsl is not None or usl is not None:
        below = int((v < lsl).sum()) if lsl is not None else 0
        above = int((v > usl).sum()) if usl is not None else 0
        out.update(observed_out=below + above, observed_pct=(below + above) / n * 100)

    if sigma > 0 and (lsl is not None or usl is not None):
        sides, p_out = [], 0.0
        if usl is not None:
            sides.append((usl - mean) / (3 * sigma))
            p_out += 1 - _normal_cdf((usl - mean) / sigma)
        if lsl is not None:
            sides.append((mean - lsl) / (3 * sigma))
            p_out += _normal_cdf((lsl - mean) / sigma)
        out["cpk"] = min(sides)
        if lsl is not None and usl is not None:
            out["cp"] = (usl - lsl) / (6 * sigma)
        out["expected_ppm"] = p_out * 1e6
        out["expected_yield"] = (1 - p_out) * 100
    return out


def cpk(values, lsl, usl):
    """Convenience wrapper returning just Cpk."""
    return capability(values, lsl, usl)["cpk"]
