"""
Phase 4 - BP calibration capture (v2: waits for a stable signal first)

v1 captured blindly for exactly 30 seconds starting the instant the
script connected - but opening the serial port resets the ESP8266
(a common quirk of NodeMCU-style boards), so some of those seconds
were actually spent on the board rebooting, not capturing usable
data. This version waits for the board to come back up AND for
steady finger contact before it starts the real 30-second count, and
prints status along the way instead of going silent.
"""

import json
import os
import time
from serial_reader import PMSSerialReader
from bp_algorithm import extract_crest_time_ratio

# --- UPDATE THIS to match your board's port ---
SERIAL_PORT = "COM3"

CALIBRATION_FILE = "calibration_points.json"
CAPTURE_SECONDS = 30

reader = PMSSerialReader(port=SERIAL_PORT)

print("Connecting... (the board will restart itself - this is normal, just wait)")
time.sleep(3)

print("Place your finger on the sensor now and hold still.")
print("Waiting for a steady signal before starting the 30s capture...")

settled = False
settle_start = None
while not settled:
    reader.read_line()
    if reader.finger_present:
        if settle_start is None:
            settle_start = time.time()
        elif time.time() - settle_start >= 3:
            settled = True
    else:
        settle_start = None

print(f"Signal looks steady. Capturing for {CAPTURE_SECONDS}s -")
print("take your cuff BP reading now, during this window.")

start = time.time()
last_status = time.time()
while time.time() - start < CAPTURE_SECONDS:
    reader.read_line()
    if time.time() - last_status >= 5:
        last_status = time.time()
        elapsed = int(time.time() - start)
        contact = "contact OK" if reader.finger_present else "NO CONTACT - keep your finger still!"
        print(f"  {elapsed}s / {CAPTURE_SECONDS}s - {len(reader.ir_buffer)} samples buffered - {contact}")

ir_window = reader.get_ir_window()
reader.close()

ct_ratio = extract_crest_time_ratio(ir_window)
if ct_ratio is None:
    print(f"Couldn't get a clean reading ({len(ir_window)} samples were buffered).")
    print("Make sure your finger stayed on the sensor, fairly still, for the whole 30s, then try again.")
    raise SystemExit

print(f"Captured crest time ratio: {ct_ratio:.3f}")
sys_val = int(input("Enter the SYS (systolic) number your cuff monitor showed: "))
dia_val = int(input("Enter the DIA (diastolic) number your cuff monitor showed: "))

points = []
if os.path.exists(CALIBRATION_FILE):
    with open(CALIBRATION_FILE) as f:
        points = json.load(f)

points.append({"crest_time_ratio": ct_ratio, "sys": sys_val, "dia": dia_val})

with open(CALIBRATION_FILE, "w") as f:
    json.dump(points, f, indent=2)

print(f"Saved. You now have {len(points)} calibration point(s).")
if len(points) < 2:
    print("Get at least one more reading (ideally after mild activity)")
    print("before BP estimates will be personalized instead of flat.")
