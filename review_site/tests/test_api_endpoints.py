import pytest
import app as flask_app


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _first_center_id():
    centers = flask_app.get_centers()
    assert centers, "no centers loaded — ensure data/medical_center_matches.csv exists"
    return next(iter(centers))


def test_record_context_returns_methodology_and_candidates(client):
    cid = _first_center_id()
    res = client.get(f"/api/record-context/center/{cid}")
    assert res.status_code == 200
    body = res.get_json()
    assert "methodology" in body and "0.85" in body["methodology"]
    assert body["hhl_record"]["hhl_id"] == cid
    assert isinstance(body["candidates"], list)
    if body["candidates"]:
        assert "name_score" in body["candidates"][0]
        assert "nppes_npi" in body["candidates"][0]


def test_record_context_unknown_id_returns_404(client):
    res = client.get("/api/record-context/center/__nope__")
    assert res.status_code == 404
    assert res.get_json()["error"] == "record not found"


def test_record_context_bad_type_returns_404(client):
    res = client.get("/api/record-context/banana/123")
    assert res.status_code == 404


def test_hhl_records_search_by_substring(client):
    # Grab a real name fragment from a loaded center.
    centers = flask_app.get_centers()
    sample_name = next(iter(centers.values()))["meta"]["hhl_name"]
    frag = sample_name.split()[0][:3].lower()

    res = client.get(f"/api/hhl-records?q={frag}")
    assert res.status_code == 200
    rows = res.get_json()
    assert isinstance(rows, list)
    assert all({"id", "name", "type", "state"} <= set(r) for r in rows)
    assert any(frag in r["name"].lower() for r in rows)


def test_hhl_records_type_filter(client):
    res = client.get("/api/hhl-records?q=a&type=center")
    assert res.status_code == 200
    assert all(r["type"] == "center" for r in res.get_json())


def test_hhl_records_empty_query_returns_empty(client):
    res = client.get("/api/hhl-records?q=")
    assert res.status_code == 200
    assert res.get_json() == []
