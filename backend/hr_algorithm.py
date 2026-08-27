"""
Phase 2 - Custom heart rate extraction (v3: longest strong autocorrelation match)

Why v3: plain autocorrelation (v2) still landed wrong - 166.7 BPM
against a real 69 BPM, almost exactly 2.4x too high. The dicrotic
notch (or a filter edge artifact right near our search boundary)
creates a SECOND point where the waveform matches a delayed copy of
itself, at a shorter lag (faster, wrong BPM) than the true heartbeat
- and that decoy match was often stronger than the real one, so
picking the single strongest match (argmax) picked the decoy.

The fix relies on one fact that always holds: a decoy match from a
notch or filter ringing shows up at a SHORTER lag (faster BPM) than
the real heartbeat - never longer. So instead of trusting whichever
match is strongest anywhere in the search range, we find every match
that's still clearly strong, and trust the LONGEST lag among them.
"""

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

SAMPLE_RATE_HZ = 25
MIN_PLAUSIBLE_BPM = 40
MAX_PLAUSIBLE_BPM = 180
MIN_WINDOW_SECONDS = 6   # need several full cycles for autocorrelation to be reliable


def bandpass_filter(signal, fs=SAMPLE_RATE_HZ, low_hz=0.7, high_hz=3.5, order=3):
    """Keep only the 42-210 BPM range, where a real pulse lives."""
    nyquist = fs / 2
    b, a = butter(order, [low_hz / nyquist, high_hz / nyquist], btype="band")
    return filtfilt(b, a, signal)


def compute_heart_rate(ir_window, fs=SAMPLE_RATE_HZ):
    """
    ir_window: 1D array of raw IR samples (ideally 6-10 seconds worth).
    Returns (heart_rate_bpm, is_valid).
    """
    if len(ir_window) < fs * MIN_WINDOW_SECONDS:
        return None, False  # not enough data buffered yet

    filtered = bandpass_filter(ir_window, fs)
    filtered = filtered - np.mean(filtered)

    corr = np.correlate(filtered, filtered, mode="full")
    corr = corr[len(corr) // 2:]
    if corr[0] == 0:
        return None, False
    corr = corr / corr[0]

    min_lag = int(fs * 60 / MAX_PLAUSIBLE_BPM)
    max_lag = min(int(fs * 60 / MIN_PLAUSIBLE_BPM), len(corr) - 1)
    if max_lag <= min_lag:
        return None, False

    search_region = corr[min_lag:max_lag]
    region_max = np.max(search_region)
    if region_max <= 0:
        return None, False

    # Every lag whose match is still clearly strong is a candidate -
    # among those, trust the longest one (see module docstring).
    threshold = 0.5 * region_max
    peak_indices, _ = find_peaks(search_region, height=threshold, distance=3)

    if len(peak_indices) == 0:
        best_lag = int(np.argmax(search_region)) + min_lag
    else:
        best_lag = int(peak_indices[-1]) + min_lag  # longest lag among strong matches

    bpm = 60.0 * fs / best_lag
    is_valid = MIN_PLAUSIBLE_BPM <= bpm <= MAX_PLAUSIBLE_BPM
    return round(bpm, 1), is_valid
