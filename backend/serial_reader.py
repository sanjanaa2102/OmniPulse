"""
Phase 3 - Serial Reader
Connects to the ESP8266 over USB serial, parses the R/V/E protocol
from the firmware, and keeps rolling buffers of raw IR/RED samples
plus the latest on-device vitals. Every other module (HR, RR, BP,
EWS) reads from an instance of this class instead of touching the
serial port directly.

Buffer length increased from 10s to 40s in Phase 3: HR only needs
a recent slice (6-10s) of it, but RR needs to see several full
breathing cycles (3-5s each), which needs a much longer window.
"""

import serial
from collections import deque
import numpy as np

BAUD_RATE = 115200
SAMPLE_RATE_HZ = 25          # matches firmware config: 100 Hz ADC / 4x averaging
BUFFER_SECONDS = 40          # long enough for RR; HR just uses a trailing slice
BUFFER_SIZE = SAMPLE_RATE_HZ * BUFFER_SECONDS


class PMSSerialReader:
    def __init__(self, port, baud=BAUD_RATE):
        # IMPORTANT: close the Arduino IDE's Serial Monitor before running this.
        # Only one program can hold the COM port open at a time.
        self.ser = serial.Serial(port, baud, timeout=1)
        self.ir_buffer = deque(maxlen=BUFFER_SIZE)
        self.red_buffer = deque(maxlen=BUFFER_SIZE)

        self.latest_device_hr = None
        self.latest_device_hr_valid = False
        self.latest_device_spo2 = None
        self.latest_device_spo2_valid = False

        self.finger_present = False
        self.status = "CONNECTING"

    def read_line(self):
        """Read and parse one line from the ESP8266. Call this repeatedly in a loop."""
        try:
            raw = self.ser.readline().decode("utf-8", errors="ignore").strip()
        except Exception:
            return
        if not raw:
            return

        parts = raw.split(",")
        tag = parts[0]

        if tag == "R" and len(parts) == 3:
            ir, red = int(parts[1]), int(parts[2])
            self.ir_buffer.append(ir)
            self.red_buffer.append(red)
            self.finger_present = True

        elif tag == "V" and len(parts) == 5:
            self.latest_device_hr = int(parts[1])
            self.latest_device_hr_valid = bool(int(parts[2]))
            self.latest_device_spo2 = int(parts[3])
            self.latest_device_spo2_valid = bool(int(parts[4]))

        elif tag == "E":
            self.status = parts[1] if len(parts) > 1 else ""
            if self.status == "NO_CONTACT":
                self.finger_present = False
                self.ir_buffer.clear()
                self.red_buffer.clear()

    def get_ir_window(self, seconds=None):
        """Returns the current rolling IR buffer as a numpy array.
        Pass `seconds` to get only the most recent slice (e.g. for HR);
        omit it to get the full buffer (e.g. for RR)."""
        arr = np.array(self.ir_buffer)
        if seconds is not None:
            n = int(seconds * SAMPLE_RATE_HZ)
            arr = arr[-n:]
        return arr

    def close(self):
        self.ser.close()