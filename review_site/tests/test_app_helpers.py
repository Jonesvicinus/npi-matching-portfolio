import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import _group_decisions_by_record, _effective_decision


# -- _group_decisions_by_record -----------------------------------------------

class TestGroupDecisionsByRecord:
    def test_single_row(self):
        decisions = {("c1", "center", "111"): {"hhl_id": "c1", "hhl_type": "center", "nppes_npi": "111", "decision": "APPROVED", "decided_at": "2026-01-01"}}
        grouped = _group_decisions_by_record(decisions)
        assert ("c1", "center") in grouped
        assert len(grouped[("c1", "center")]) == 1

    def test_two_approved_npis_same_center(self):
        decisions = {
            ("c1", "center", "111"): {"hhl_id": "c1", "hhl_type": "center", "nppes_npi": "111", "decision": "APPROVED", "decided_at": "2026-01-01"},
            ("c1", "center", "222"): {"hhl_id": "c1", "hhl_type": "center", "nppes_npi": "222", "decision": "APPROVED", "decided_at": "2026-01-02"},
        }
        grouped = _group_decisions_by_record(decisions)
        assert len(grouped[("c1", "center")]) == 2

    def test_different_records_not_mixed(self):
        decisions = {
            ("c1", "center", "111"): {"hhl_id": "c1", "hhl_type": "center", "nppes_npi": "111", "decision": "APPROVED", "decided_at": "2026-01-01"},
            ("c2", "center", "222"): {"hhl_id": "c2", "hhl_type": "center", "nppes_npi": "222", "decision": "APPROVED", "decided_at": "2026-01-02"},
        }
        grouped = _group_decisions_by_record(decisions)
        assert len(grouped[("c1", "center")]) == 1
        assert len(grouped[("c2", "center")]) == 1

    def test_empty(self):
        assert _group_decisions_by_record({}) == {}


# -- _effective_decision -------------------------------------------------------

def _row(npi, decision, decided_at="2026-01-01"):
    return {"hhl_id": "x", "hhl_type": "center", "nppes_npi": npi, "decision": decision, "decided_at": decided_at}

class TestEffectiveDecision:
    def test_no_rows_returns_pending(self):
        d, status = _effective_decision([], "x", set(), has_sibling=False)
        assert status == "PENDING"
        assert d is None

    def test_non_sibling_approved(self):
        rows = [_row("111", "APPROVED")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=False)
        assert status == "APPROVED"
        assert d["nppes_npi"] == "111"

    def test_non_sibling_rejected(self):
        rows = [_row("111", "REJECTED")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=False)
        assert status == "REJECTED"

    def test_sibling_with_approved_npis_but_not_complete_is_pending(self):
        rows = [_row("111", "APPROVED"), _row("222", "APPROVED", "2026-01-02")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=True)
        assert status == "PENDING"
        assert d is None

    def test_sibling_complete(self):
        rows = [_row("111", "APPROVED"), _row("222", "APPROVED", "2026-01-02")]
        d, status = _effective_decision(rows, "x", {"x"}, has_sibling=True)
        assert status == "APPROVED"

    def test_sibling_whole_record_flagged_overrides_approved_npis(self):
        rows = [_row("111", "APPROVED"), _row("", "FLAGGED", "2026-01-03")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=True)
        assert status == "FLAGGED"
        assert d["nppes_npi"] == ""

    def test_sibling_whole_record_rejected(self):
        rows = [_row("", "REJECTED")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=True)
        assert status == "REJECTED"

    def test_non_sibling_uses_latest_row(self):
        rows = [
            _row("111", "APPROVED", "2026-01-01"),
            _row("222", "APPROVED", "2026-01-03"),
        ]
        d, status = _effective_decision(rows, "x", set(), has_sibling=False)
        assert d["nppes_npi"] == "222"

    def test_whole_record_row_beats_approved_npi_row(self):
        rows = [_row("111", "APPROVED", "2026-01-01"), _row("", "FLAGGED", "2026-01-02")]
        d, status = _effective_decision(rows, "x", set(), has_sibling=False)
        assert status == "FLAGGED"
