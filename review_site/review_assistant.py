"""Builds the plain-English methodology reference handed to the review
assistant. Numeric values are read live from the pipeline's scoring module so
the text can never drift from the real pipeline."""

import scoring


def build_methodology():
    """Return a plain-English description of how the matching pipeline scores
    and ranks candidates, with all numeric thresholds pulled from source."""
    s = scoring
    return f"""\
HOW THE MATCHING PIPELINE SCORES CANDIDATES

Each HHL record is matched against NPPES provider records. Every candidate gets
a composite match_score from 0 to 1, built from weighted signals.

Centers (composite weights):
- With ZIP present: name x0.60 + city x0.20 + zip x0.20.
- ZIP missing: weight shifts to name x0.80 + city x0.20.
- A "unique" sole-candidate bonus of +{s.UNIQUE_BONUS} may apply.

Professionals (composite weights):
- With city present: name x0.55 + city x0.25 + credential x0.10 + taxonomy x0.10.
- City missing: name x0.80 + credential x0.10 + taxonomy x0.10, capped at
  {s.CITY_MISSING_CEILING} (the city_missing ceiling) unless a uniqueness bonus
  ({s.UNIQUE_BONUS_NO_CITY} when city is missing) overrides.
- A phone match adds +{s.PHONE_MATCH_BONUS}.

Confidence tiers (selection confidence shown to the reviewer):
- HIGH: composite >= {s.HIGH_THRESHOLD} AND name_score >= 0.90 AND no warning
  flags fired.
- MEDIUM: composite >= {s.MEDIUM_THRESHOLD}.
- LOW: below {s.MEDIUM_THRESHOLD}.
When only the name signal is available (no address/city/zip/phone in the HHL
record), MEDIUM is effectively the ceiling, because no other signal can fire.

Candidate strength (a separate label from confidence, based purely on composite):
- STRONG: composite >= {s.HIGH_THRESHOLD}.
- MODERATE: composite >= {s.MEDIUM_THRESHOLD}.
- WEAK: below {s.MEDIUM_THRESHOLD}.

Confidence flags (warnings attached to a candidate):
- perfect_name: name_score == 1.0 (an exact name match, including NPPES
  alternate/"other" names).
- margin_too_close: the gap to the next candidate is < 0.08 and the score is at
  least MEDIUM and name_score < 0.95 — the top two candidates are hard to
  separate.
- name_below_threshold: composite reached HIGH but name_score < 0.90, so the
  high score is driven by non-name signals.
- city_conflict: the HHL and NPPES cities disagree.

Sibling-location detection (centers only):
- Fires only when at least 2 candidates have name_score >= {s.SIBLING_NAME_THRESHOLD}
  in different NPPES cities. When it fires, every candidate with name_score >=
  {s.SIBLING_FLAG_THRESHOLD} is flagged sibling_location, and sibling_count is set.
- IMPORTANT LIMITATION: because the trigger requires name_score >=
  {s.SIBLING_NAME_THRESHOLD}, an organization that registered under a longer
  formal/parent name (scoring below that on the HHL short name) will NOT trigger
  detection even when real sibling locations exist in the candidate list. When
  asked about siblings, inspect the raw candidate addresses/cities yourself and
  report what you find, clearly separating it from what the pipeline flagged.
"""
