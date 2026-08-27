"""
Phase 4 - Cuffless blood pressure estimation (v2: heart-rate-normalized feature)

Without a second sensor (like an ECG), there's no way to measure
true Pulse Transit Time, which is what most published cuffless BP
systems rely on for their best accuracy. With PPG alone, we instead
use pulse SHAPE - specifically, what fraction of each heartbeat is
spent on the rising (systolic) upstroke before the peak. Stiffer
arteries reflect the pressure wave back faster, shrinking that
fraction, and that shift is documented to track with blood pressure.

Why v2: v1 used raw crest time in milliseconds, which has a real
confound - crest time shrinks when the heart simply beats faster,
even with zero change in arterial stiffness, because a faster heart
rate compresses the whole cardiac cycle. Two calibration points
(rest vs. post-exercise, where heart rate was very different) fit a
line that ran backwards from known physiology as a result. Dividing
crest time by the FULL length of that same beat cancels the
heart-rate effect out, since both shrink together when only heart
rate changes - leaving something closer to a pure measure of pulse
shape.

IMPORTANT: this changes the units of the calibration feature from
milliseconds to a dimensionless ratio. Any calibration points saved
under v1 are NOT compatible - delete calibration_points.json and
recapture before using this version.
"""

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

SAMPLE_RATE_HZ = 25


def bandpass_filter(signal, fs=SAMPLE_RATE_HZ, low_hz=0.7, high_hz=8.0, order=3):
    """Wider band than the HR filter - preserves enough pulse shape detail to time it."""
    nyquist = fs / 2
    b, a = butter(order, [low_hz / nyquist, high_hz / nyquist], btype="band")
    return filtfilt(b, a, signal)


def extract_crest_time_ratio(ir_window, fs=SAMPLE_RATE_HZ):
    """
    Average, across all detectable beats, of:
        (time from a beat's foot to its systolic peak) / (full length of that beat)
    Returns a dimensionless fraction (typically ~0.15-0.45), or None if
    too few clean beats were found.
    """
    if len(ir_window) < fs * 5:
        return None

    filtered = bandpass_filter(ir_window, fs)
    filtered = filtered - np.mean(filtered)

    min_distance = int(fs * 60 / 180)  # fastest plausible beat spacing
    prominence = 0.5 * np.std(filtered)

    peaks, _ = find_peaks(filtered, distance=min_distance, prominence=prominence)
    troughs, _ = find_peaks(-filtered, distance=min_distance, prominence=prominence)

    if len(peaks) < 3 or len(troughs) < 4:
        return None

    ratios = []
    for i in range(len(troughs) - 1):
        beat_start = troughs[i]
        beat_end = troughs[i + 1]
        beat_period_samples = beat_end - beat_start
        if beat_period_samples <= 0:
            continue

        beat_peaks = peaks[(peaks > beat_start) & (peaks < beat_end)]
        if len(beat_peaks) == 0:
            continue
        peak_idx = beat_peaks[0]

        ratio = (peak_idx - beat_start) / beat_period_samples
        if 0.1 <= ratio <= 0.6:  # sanity range for a real systolic upstroke fraction
            ratios.append(ratio)

    if len(ratios) < 3:
        return None

    return float(np.mean(ratios))


class BPCalibration:
    """
    Holds calibration points and estimates BP from a crest time RATIO.
      - 0 points: no estimate possible.
      - 1 point: returns that reading as a flat baseline (can't track
        change with only one data point).
      - 2+ points: fits a line (least squares) personalized to this
        person's own measured shape-vs-BP relationship.
    """

    def __init__(self, points=None):
        # points: list of (crest_time_ratio, sys, dia)
        self.points = list(points) if points else []

    def add_point(self, ratio, sys, dia):
        self.points.append((ratio, sys, dia))

    def estimate(self, current_ratio):
        if len(self.points) == 0 or current_ratio is None:
            return None, None
        if len(self.points) == 1:
            _, sys, dia = self.points[0]
            return sys, dia

        ratios = np.array([p[0] for p in self.points])
        syss = np.array([p[1] for p in self.points])
        dias = np.array([p[2] for p in self.points])
        sys_slope, sys_intercept = np.polyfit(ratios, syss, 1)
        dia_slope, dia_intercept = np.polyfit(ratios, dias, 1)

        sys_est = sys_slope * current_ratio + sys_intercept
        dia_est = dia_slope * current_ratio + dia_intercept
        return round(sys_est), round(dia_est)
