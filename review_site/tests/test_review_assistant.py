from review_assistant import build_methodology


def test_methodology_includes_live_scoring_constants():
    text = build_methodology()
    # Confidence thresholds (from scoring.py)
    assert "0.85" in text
    assert "0.65" in text
    # Sibling detection threshold (from match_medical_centers.py)
    assert "0.97" in text


def test_methodology_explains_key_concepts():
    text = build_methodology().lower()
    for term in ["confidence", "sibling", "name", "perfect_name", "margin_too_close"]:
        assert term in text


def test_methodology_is_nonempty_string():
    text = build_methodology()
    assert isinstance(text, str) and len(text) > 200
