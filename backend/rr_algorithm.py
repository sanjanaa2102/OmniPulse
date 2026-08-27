"""
Phase 3 - Respiratory rate extraction (v3: excluding the Mayer wave band)

The same raw IR waveform used for HR also carries a much slower
signal: breathing changes blood volume and vessel tone in the
finger just enough to slowly shift the PPG baseline up and down,
once per breath. This is called Respiratory-Induced Intensity
Variation (RIIV) - it's literally the slow drift the HR bandpass
filter throws away as noise.

Why v3: v2 (plain strongest match, no directional bias) still read
~7.8-9 breaths/min against a real 12. Removing the bias barely
changed the number, which means the issue wasn't WHICH peak we
picked - something in that frequency range genuinely correlates
more strongly than the real breathing signal does. The likely cause:
a real, separate physiological rhythm in peripheral blood flow
(often called a Mayer wave) sits around 0.1 Hz (~6 breaths/min
equivalent), caused by blood vessel tone cycling rather than
breathing - and in a short, still recording it can easily be
stronger than the actual breathing signal. Our filter's lower edge
(6 breaths/min) was letting it straight through.

Fix: raise the filter's lower edge to exclude that band, and narrow
the plausible search range to match - a healthy resting adult only
rarely genuinely breathes below ~8/min anyway.

Needs a much longer window than HR: a breath takes 3-5 seconds, so
we need at least 25-30 seconds of data to see several full cycles.
"""

import numpy as np
from scipy.signal import butter, filtfilt

SAMPLE_RATE_HZ = 25
MIN_PLAUSIBLE_RR = 8      # breaths/min - excludes the Mayer wave band below this
MAX_PLAUSIBLE_RR = 40     # breaths/min
MIN_WINDOW_SECONDS = 25   # need several full breathing cycles


def lowpass_breathing_filter(signal, fs=SAMPLE_RATE_HZ, low_hz=0.15, high_hz=0.6, order=3):
    """Keep only ~9-36 breaths/min - breathing range, with the Mayer wave band filtered out."""
    nyquist = fs / 2
    b, a = butter(order, [low_hz / nyquist, high_hz / nyquist], btype="band")
    return filtfilt(b, a, signal)


def compute_respiratory_rate(ir_window, fs=SAMPLE_RATE_HZ):
    """
    ir_window: 1D array of raw IR samples, ideally 25-40 seconds worth.
    Returns (breaths_per_minute, is_valid).
    """
    if len(ir_window) < fs * MIN_WINDOW_SECONDS:
        return None, False

    filtered = lowpass_breathing_filter(ir_window, fs)
    filtered = filtered - np.mean(filtered)

    corr = np.correlate(filtered, filtered, mode="full")
    corr = corr[len(corr) // 2:]
    if corr[0] == 0:
        return None, False
    corr = corr / corr[0]

    min_lag = int(fs * 60 / MAX_PLAUSIBLE_RR)
    max_lag = min(int(fs * 60 / MIN_PLAUSIBLE_RR), len(corr) - 1)
    if max_lag <= min_lag:
        return None, False

    search_region = corr[min_lag:max_lag]
    best_lag = int(np.argmax(search_region)) + min_lag

    rr = 60.0 * fs / best_lag
    is_valid = MIN_PLAUSIBLE_RR <= rr <= MAX_PLAUSIBLE_RR
    return round(rr, 1), is_valid
