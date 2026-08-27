"""
Phase 5 - Test harness
Run this directly (python main_test.py) to check HR, RR, SpO2, BP,
and the combined AI Early Warning Score together, live. Press
Ctrl+C to stop.

RR takes ~25-30s to warm up - be patient. BP requires
calibration_points.json to exist (run calibrate_bp.py at least
twice first) - without it, BP and EWS will just skip that input.
"""

import json
import os
import time
from serial_reader import PMSSerialReader
from hr_algorithm import compute_heart_rate
from rr_algorithm import compute_respiratory_rate
from bp_algorithm import extract_crest_time_ratio, BPCalibration
from ews_algorithm import compute_ews
from smoothing import ReadingSmoother

# --- UPDATE THIS to match your board's port ---
SERIAL_PORT = "COM3"
CALIBRATION_FILE = "calibration_points.json"

bp_cal = BPCalibration()
if os.path.exists(CALIBRATION_FILE):
    with open(CALIBRATION_FILE) as f:
        saved_points = json.load(f)
    for p in saved_points:
        bp_cal.add_point(p["crest_time_ratio"], p["sys"], p["dia"])
    print(f"Loaded {len(bp_cal.points)} BP calibration point(s).")
else:
    print("No BP calibration found yet - run calibrate_bp.py first for BP estimates.")

hr_smoother = ReadingSmoother(history_size=5, max_jump=25)

reader = PMSSerialReader(port=SERIAL_PORT)

print("Reading from sensor... place a finger and hold still.")
print("(RR takes ~25-30s to warm up - be patient)")
last_print = time.time()

try:
    while True:
        reader.read_line()

        if time.time() - last_print >= 1.0:
            last_print = time.time()
            if reader.finger_present:
                hr_raw, hr_valid_raw = compute_heart_rate(reader.get_ir_window(seconds=10))
                hr, hr_valid = hr_smoother.update(hr_raw, hr_valid_raw)
                rr, rr_valid = compute_respiratory_rate(reader.get_ir_window())
                ct_ratio = extract_crest_time_ratio(reader.get_ir_window(seconds=10))
                bp_sys, bp_dia = bp_cal.estimate(ct_ratio) if ct_ratio else (None, None)
                spo2 = reader.latest_device_spo2 if reader.latest_device_spo2_valid else None

                risk, score, breakdown = compute_ews(
                    hr=hr if hr_valid else None,
                    rr=rr if rr_valid else None,
                    spo2=spo2,
                    sys_bp=bp_sys,
                )

                hr_str = f"{hr} bpm" if hr_valid else "calculating..."
                rr_str = f"{rr} br/min" if rr_valid else "calculating..."
                spo2_str = f"{spo2}%" if spo2 is not None else "calculating..."
                bp_str = f"{bp_sys}/{bp_dia}" if bp_sys is not None else (
                    "no calibration" if len(bp_cal.points) == 0 else "calculating..."
                )

                print(
                    f"HR: {hr_str:14s} | RR: {rr_str:14s} | SpO2: {spo2_str:16s} "
                    f"| BP: {bp_str:16s} | EWS: {risk} ({score})"
                )
            else:
                print("Waiting for finger...")
except KeyboardInterrupt:
    print("\nStopping.")
    reader.close()
