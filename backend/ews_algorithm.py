"""
Phase 5 - AI Early Warning Score (EWS) - v4: age-banded + patient messaging

Combines HR, RR, SpO2, and BP into a single live risk level.

Why v4: age was being collected in the GUI but never actually used -
the whole point of asking for it. Added age-banded HR/RR thresholds,
sourced from PALS 2015 pediatric guidelines and cross-checked against
a second independent pediatric vitals reference. Only two tiers turned
out to be evidence-backed: "child" and "adult" - by roughly age 12,
published pediatric ranges have already converged to the same numbers
used for adults, so a separate adolescent tier wasn't a real distinction.

SCOPE NOTE: the "child" band is a single blended range covering
roughly ages 5-11 (school-age reference values, since that's the
practical population for a fingertip PPG clip - an infant or toddler
generally can't hold still for one). This is NOT validated or intended
for infants/toddlers; that population needs different hardware
entirely, not just different thresholds.

SpO2 and systolic BP are NOT age-banded: healthy SpO2 range is
consistent across ages, and BP already gets personalized per-patient
through the calibration system - a blanket age adjustment on top of
an already-personal calibration would work against it, not with it.

Also added generate_patient_message() - a plain-language explanation
for the person being monitored, separate from the technical score a
clinician would read.
"""

RISK_STABLE = "STABLE"
RISK_WATCH = "WATCH"
RISK_CRITICAL = "CRITICAL"

AGE_BANDS = {
    "child": {   # ~ages 5-11, school-age reference values (PALS 2015)
        "hr": {"baseline": (70, 120), "flag": (60, 140)},
        "rr": {"baseline": (18, 25), "flag": (14, 30)},
    },
    "adult": {   # ~age 12+ - pediatric ranges converge to adult by here
        "hr": {"baseline": (60, 100), "flag": (50, 110)},
        "rr": {"baseline": (12, 20), "flag": (9, 24)},
    },
}


def get_age_band(age):
    if age is None:
        return "adult"
    return "child" if age < 12 else "adult"


def _score_from_bands(value, baseline, flag):
    if value is None:
        return 0
    value = round(value)
    lo_b, hi_b = baseline
    lo_f, hi_f = flag
    if value < lo_f or value > hi_f:
        return 3
    if value < lo_b or value > hi_b:
        return 1
    return 0


def _score_hr(hr, age_band="adult"):
    bands = AGE_BANDS[age_band]["hr"]
    return _score_from_bands(hr, bands["baseline"], bands["flag"])


def _score_rr(rr, age_band="adult"):
    bands = AGE_BANDS[age_band]["rr"]
    return _score_from_bands(rr, bands["baseline"], bands["flag"])


def _score_spo2(spo2):
    """Healthy baseline 95-100%; flag below 92%. Not age-banded - see
    module docstring."""
    if spo2 is None:
        return 0
    spo2 = round(spo2)
    if spo2 < 92:
        return 3
    if spo2 < 95:
        return 1
    return 0


def _score_sys_bp(sys_bp):
    """Healthy baseline 90-120 mmHg; flag <90 or >140 mmHg. Not
    age-banded - see module docstring."""
    if sys_bp is None:
        return 0
    sys_bp = round(sys_bp)
    if sys_bp < 90 or sys_bp > 140:
        return 3
    if sys_bp > 120:
        return 1
    return 0


def compute_ews(hr=None, rr=None, spo2=None, sys_bp=None, age=None, athletic=False):
    """
    Returns (risk_level, total_score, breakdown_dict).
    `age` (years) selects the HR/RR reference band - omit or pass
    None to use adult ranges. `athletic=True` widens the adult HR
    baseline downward (40-100 instead of 60-100) - well-trained
    individuals commonly have a genuinely healthy resting HR in the
    40s-50s, which would otherwise score points here. Not applied to
    the child band (pediatric HR norms aren't adjusted for fitness
    the same way). Any vital can be None if not yet available - it's
    excluded (scored 0) rather than treated as automatically dangerous.
    """
    age_band = get_age_band(age)
    hr_bands = AGE_BANDS[age_band]["hr"]
    if athletic and age_band == "adult":
        hr_bands = {"baseline": (40, 100), "flag": (35, 110)}

    scores = {
        "hr": _score_from_bands(hr, hr_bands["baseline"], hr_bands["flag"]),
        "rr": _score_rr(rr, age_band),
        "spo2": _score_spo2(spo2),
        "sys_bp": _score_sys_bp(sys_bp),
    }
    total = sum(scores.values())
    has_red_flag = 3 in scores.values()

    if total >= 7:
        risk = RISK_CRITICAL
    elif total >= 5 or has_red_flag:
        risk = RISK_WATCH
    else:
        risk = RISK_STABLE

    return risk, total, scores


def generate_patient_message(risk, breakdown):
    """Plain-language guidance for the person being monitored - not the
    clinical jargon a nurse would read. Names the single most relevant
    reason rather than listing everything that scored."""
    if risk == RISK_STABLE:
        return "Your vitals look normal right now."

    worst = max(breakdown, key=lambda k: breakdown[k])
    reasons = {
        "hr": "your heart rate is outside its usual range",
        "rr": "your breathing rate is outside its usual range",
        "spo2": "your oxygen level is a little lower than usual",
        "sys_bp": "your blood pressure is outside its usual range",
    }
    reason = reasons.get(worst, "one of your vitals is outside its usual range")

    if risk == RISK_WATCH:
        return f"Heads up \u2014 {reason}. Sit still, breathe normally, and let a nurse know if it doesn't settle."
    return f"Your vitals have changed significantly \u2014 {reason}. Staff have been notified. Please stay seated and try to remain calm."
