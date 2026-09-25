import pandas as pd
import pytest

from tnt.db import get_engine
from tnt.loader import load_csv_text, parse_esr
from tnt.stats import cpk


def test_parse_esr():
    assert parse_esr("167.0542m?") == pytest.approx(167.0542)
    assert parse_esr("0.15") == pytest.approx(150.0)   # plain ohms -> mOhm
    assert parse_esr("garbage") is None


def test_cpk():
    v = pd.Series([9, 10, 11, 10, 10])
    sigma = v.std(ddof=1)
    assert cpk(v, 7, 13) == pytest.approx(3 / (3 * sigma))
    assert cpk(v, None, 13) == pytest.approx(3 / (3 * sigma))
    assert cpk(v, None, None) is None


HEADER = ("SKU,Carton_ID,Inner_Box_ID,IMEI,Device_ID,ICCID,Temp_Humi_SN,PCB_SN,"
          "Battery_SN,ESR,Scan_Time,Lot_ID,Firmware_Version,Hardware_Version,"
          "Test_Status,Tester_FW,Test_Time\n")
ROW = ("SKU1,C1,B1,{imei},{did},ICC,TH,PCB,BAT,{esr},2026-07-20 13:21:46,LOT1,FW,HW,"
       "Good,TFW,2026-07-13 16:46:25\n")


def test_reload_is_idempotent(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 't.db'}")
    csv = HEADER + ROW.format(imei="1", did="a", esr="150m?") + ROW.format(imei="2", did="b", esr="160m?")
    assert load_csv_text(csv, "a.csv", eng)["total_devices"] == 2
    assert load_csv_text(csv, "a.csv", eng)["total_devices"] == 2
