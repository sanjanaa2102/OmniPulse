"""
Phase 5 - Reading smoother

Wraps a vital-sign reading with outlier rejection. hr_algorithm.py's
"longest strong match" logic is solid on average, but it can
occasionally lock onto the wrong autocorrelation peak for a single
reading (see hr_algorithm.py's docstring for the mechanism) - the
result is a rare, brief jump to roughly the wrong multiple of the
true rate, then a return to normal. Feeding that straight into the
EWS produces a false alarm on an otherwise completely normal
reading.

This keeps a short rolling history and rejects any new reading that
jumps too far from the recent trend, holding the last good value
for that one cycle instead. Tradeoff worth knowing: a genuinely fast
real change (e.g. standing up quickly) could also get held back for
a reading or two - acceptable for a resting-monitoring context, but
worth loosening max_jump if this is ever used during active exertion.
"""


class ReadingSmoother:
    def __init__(self, history_size=5, max_jump=25):
        self.history = []
        self.history_size = history_size
        self.max_jump = max_jump

    def update(self, value, is_valid):
        """Returns (smoothed_value, is_valid) - call once per new reading."""
        if not is_valid or value is None:
            if self.history:
                return self.history[-1], True
            return None, False

        if self.history:
            sorted_hist = sorted(self.history)
            median = sorted_hist[len(sorted_hist) // 2]
            if abs(value - median) > self.max_jump:
                return self.history[-1], True  # reject the outlier

        self.history.append(value)
        if len(self.history) > self.history_size:
            self.history.pop(0)
        return value, True
