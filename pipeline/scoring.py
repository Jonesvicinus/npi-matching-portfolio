from typing import Optional

UNIQUE_BONUS          = 0.05
UNIQUE_BONUS_NO_CITY  = 0.10
PHONE_MATCH_BONUS     = 0.10
HIGH_THRESHOLD        = 0.85
MEDIUM_THRESHOLD      = 0.65
CITY_MISSING_CEILING  = 0.92   # raised: allow perfect name matches to reach HIGH


def zip_compare(hhl_zip, nppes_zip):
    """
    Compare ZIPs at the most specific level available.
    Returns (match: bool, level: "zip9" | "zip5" | "none").
    Rejects values with < 5 or > 9 digits after cleaning (blocks phone numbers/URLs).
    """
    def digits(z):
        raw = str(z or "").strip().lower()
        if "http://" in raw or "https://" in raw or "www." in raw:
            return ""
        return "".join(ch for ch in raw if ch.isdigit())

    h, n = digits(hhl_zip), digits(nppes_zip)

    if len(h) < 5 or len(h) > 9 or len(n) < 5 or len(n) > 9:
        return False, "none"

    if len(h) >= 9 and len(n) >= 9 and h[:9] == n[:9]:
        return True, "zip9"

    if h[:5] == n[:5]:
        return True, "zip5"

    return False, "none"


def assess_candidate_strength(match_score):
    """STRONG / MODERATE / WEAK based on final composite. No margin or conflict gate."""
    if match_score >= HIGH_THRESHOLD:
        return "STRONG"
    if match_score >= MEDIUM_THRESHOLD:
        return "MODERATE"
    return "WEAK"


def assess_selection_confidence(match_score, city_conflict, margin, name_score=1.0):
    """
    Returns (selection_confidence: str, confidence_flags: list).

    Option B: city_conflict and margin_too_close are surfaced at any score tier.
    city_conflict is always shown when present.
    margin_too_close is only shown when match_score >= MEDIUM_THRESHOLD (LOW candidate
    being close to another LOW candidate is not meaningful reviewer context).
    HIGH is only returned when score >= HIGH_THRESHOLD and no flags fired.

    name_score: the raw name similarity (0–1). A HIGH composite driven by non-name
    signals (credential/taxonomy/uniqueness) with a weak name is misleading — require
    name_score >= 0.85 as a prerequisite for HIGH. Defaults to 1.0 for backward compat.
    """
    flags = []

    if city_conflict:
        flags.append("city_conflict")

    # Don't flag margin_too_close when the name is near-perfect — a clear name
    # match is trusted even if composite scores are close (credential/taxonomy
    # differences can close the gap without the name actually being ambiguous).
    if margin is not None and margin < 0.08 and match_score >= MEDIUM_THRESHOLD and name_score < 0.95:
        flags.append("margin_too_close")

    if match_score >= HIGH_THRESHOLD and name_score < 0.90:
        flags.append("name_below_threshold")

    if match_score >= HIGH_THRESHOLD and not flags:
        tier = "HIGH"
    elif match_score >= MEDIUM_THRESHOLD:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    if name_score == 1.0:
        flags.append("perfect_name")

    return tier, flags


def professional_composite(
    name_score: float,
    city_val: float,
    cred_match: bool,
    tax_match: bool,
    city_missing: bool,
    is_unique: bool = False,
    phone_match: bool = False,
) -> tuple:
    """
    Returns (composite_score, signals_list).

    city_val:     pass 1.0 for a confirmed city match, 0.0 otherwise.
                  For anchor_approved, always pass 1.0.
    city_missing: True when HHL has no city data at all (not just unmatched).
                  Redistributes city weight to name and applies 0.75 ceiling
                  unless is_unique overrides it.
    """
    signals = []
    if name_score >= 0.85:
        signals.append("name")
    elif name_score >= 0.80:
        signals.append("name_good")

    cred_val = 1.0 if cred_match else 0.0
    tax_val  = 1.0 if tax_match  else 0.0

    if city_missing:
        composite = name_score * 0.80 + cred_val * 0.10 + tax_val * 0.10
        signals.append("city_missing")
    else:
        composite = name_score * 0.55 + city_val * 0.25 + cred_val * 0.10 + tax_val * 0.10
        if city_val > 0:
            signals.append("city")

    if cred_match:
        signals.append("credential")
    if tax_match:
        signals.append("taxonomy")

    if city_missing:
        composite = min(composite, CITY_MISSING_CEILING)

    if is_unique:
        bonus = UNIQUE_BONUS_NO_CITY if city_missing else UNIQUE_BONUS
        composite = min(1.0, composite + bonus)
        signals.append("unique")

    if phone_match:
        composite = min(1.0, composite + PHONE_MATCH_BONUS)
        signals.append("phone")

    field_scores = {
        "name":       name_score,
        "city":       None if city_missing else city_val,
        "credential": cred_val,
        "taxonomy":   tax_val,
    }
    return round(composite, 3), signals, field_scores


def center_composite(
    name_score: float,
    city_match: bool,
    zip_match: bool,
    zip_missing: bool,
    is_unique: bool = False,
    zip_match_level=None,
    city_missing: bool = False,
) -> tuple:
    """
    Returns (composite_score, signals_list, field_scores).

    zip_missing: True when HHL has no ZIP data. Redistributes ZIP weight to name.
    zip_match_level: "zip9" or "zip5" from zip_compare(). Controls signal label.
                     Falls back to "zip" when None (backward compat).
    city_missing: True when HHL has no city data. field_scores["city"] will be None.
    """
    signals = []
    if name_score >= 0.85:
        signals.append("name")
    elif name_score >= 0.80:
        signals.append("name_good")

    city_val = 1.0 if city_match else 0.0
    zip_val  = 1.0 if zip_match  else 0.0

    if city_match:
        signals.append("city")
    if zip_match:
        signals.append(zip_match_level or "zip")

    if zip_missing:
        composite = name_score * 0.80 + city_val * 0.20
    else:
        composite = name_score * 0.60 + city_val * 0.20 + zip_val * 0.20

    if is_unique:
        composite = min(1.0, composite + UNIQUE_BONUS)
        signals.append("unique")

    field_scores = {
        "name": name_score,
        "city": None if city_missing else city_val,
        "zip":  None if zip_missing else zip_val,
    }

    return round(composite, 3), signals, field_scores
