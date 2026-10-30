import io

import openpyxl
import pandas as pd
import pytest

from tnt.db import get_engine
from tnt.loader import load_file, parse_esr, read_table
from tnt.stats import capability, cpk


def test_parse_esr():
    assert parse_esr("167.0542m?") == pytest.approx(167.0542)
    assert parse_esr("167.0542mΩ") == pytest.approx(167.0542)
    assert parse_esr("0.15") == pytest.approx(150.0)   # plain ohms -> mOhm
    assert parse_esr("garbage") is None


def test_cpk_and_fallout():
    v = pd.Series([9, 10, 11, 10, 10])
    sigma = v.std(ddof=1)
    assert cpk(v, 7, 13) == pytest.approx(3 / (3 * sigma))
    assert cpk(v, None, 13) == pytest.approx(3 / (3 * sigma))
    assert cpk(v, None, None) is None
    # Cpk = 1 (limits at mean +/- 3 sigma) -> about 2700 ppm outside
    c = capability(v, 10 - 3 * sigma, 10 + 3 * sigma)
    assert c["cpk"] == pytest.approx(1.0)
    assert c["expected_ppm"] == pytest.approx(2699.8, rel=1e-3)


HEADER = ("SKU,Carton_ID,Inner_Box_ID,IMEI,Device_ID,ICCID,Temp_Humi_SN,PCB_SN,"
          "Battery_SN,ESR,Scan_Time,Lot_ID,Firmware_Version,Hardware_Version,"
          "Test_Status,Tester_FW,Test_Time\n")
ROW = ("SKU1,C1,B1,{imei},{did},ICC,TH,PCB,BAT,{esr},2026-07-20 13:21:46,LOT1,FW,HW,"
       "Good,TFW,2026-07-13 16:46:25\n")
CSV = HEADER + ROW.format(imei="1", did="a", esr="150m?") + ROW.format(imei="2", did="b", esr="160m?")


def test_reload_is_idempotent(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    assert load_file(CSV.encode(), "a.csv", eng)["total_devices"] == 2
    assert load_file(CSV.encode(), "a.csv", eng)["total_devices"] == 2


def test_xlsx_matches_csv():
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    rows = [line.split(",") for line in CSV.strip().split("\n")]
    for r in rows:
        ws.append(r)
    ws["D2"] = 863598076986636                      # IMEI typed as a number
    ws["Q2"] = datetime(2026, 7, 13, 16, 46, 25)    # Test_Time typed as a date
    buf = io.BytesIO()
    wb.save(buf)
    df = read_table(buf.getvalue(), "a.xlsx")
    assert df.loc[0, "IMEI"] == "863598076986636"
    assert df.loc[0, "Test_Time"] == "2026-07-13 16:46:25"
    assert df.loc[1, "ESR"] == "160m?"
