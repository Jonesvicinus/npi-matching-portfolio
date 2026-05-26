UNIQUE_BONUS          = 0.05
UNIQUE_BONUS_NO_CITY  = 0.10
HIGH_THRESHOLD        = 0.80
MEDIUM_THRESHOLD      = 0.60
CITY_MISSING_CEILING  = 0.75


def professional_composite(
    name_score: float,
    city_val: float,
    cred_match: bool,
    tax_match: bool,
    city_missing: bool,
    is_unique: bool = False,
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
        composite = name_score * 0.70 + cred_val * 0.15 + tax_val * 0.15
        signals.append("city_missing")
    else:
        composite = name_score * 0.50 + city_val * 0.20 + cred_val * 0.15 + tax_val * 0.15
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

    return round(composite, 3), signals


def center_composite(
    name_score: float,
    city_match: bool,
    zip_match: bool,
    zip_missing: bool,
    is_unique: bool = False,
) -> tuple:
    """
    Returns (composite_score, signals_list).

    zip_missing: True when HHL has no ZIP data. Redistributes ZIP weight to name.
    City is always present for centers (in HHL CSV), so no ceiling is applied.
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
        signals.append("zip")

    if zip_missing:
        composite = name_score * 0.80 + city_val * 0.20
    else:
        composite = name_score * 0.60 + city_val * 0.20 + zip_val * 0.20

    if is_unique:
        composite = min(1.0, composite + UNIQUE_BONUS)
        signals.append("unique")

    return round(composite, 3), signals


def confidence_from_score(composite: float) -> str:
    if composite >= HIGH_THRESHOLD:
        return "HIGH"
    if composite >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"
