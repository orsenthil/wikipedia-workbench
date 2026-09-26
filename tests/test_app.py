import httpx
import respx
from fastapi.testclient import TestClient

from editor.app import app
from editor.wikipedia import API_URL


def _contrib(revid, pageid, title, ts):
    return {"revid": revid, "pageid": pageid, "ns": 0, "title": title,
            "timestamp": ts, "comment": "", "size": 100}


@respx.mock
def test_articles_aggregates_and_follows_continuation():
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.Response(200, json={
            "continue": {"uccontinue": "x", "continue": "-||"},
            "query": {"usercontribs": [
                _contrib(3, 1, "Python", "2026-09-01T00:00:00Z"),
                _contrib(2, 2, "Chennai", "2026-08-01T00:00:00Z"),
            ]},
        }),
        httpx.Response(200, json={
            "query": {"usercontribs": [_contrib(1, 1, "Python", "2026-01-01T00:00:00Z")]},
        }),
    ]
    with TestClient(app) as client:
        resp = client.get("/api/articles")
    assert resp.status_code == 200
    data = resp.json()
    assert [a["title"] for a in data] == ["Python", "Chennai"]
    assert data[0]["edit_count"] == 2
    assert data[0]["url"] == "https://en.wikipedia.org/wiki/Python"
    assert route.call_count == 2
    assert route.calls[1].request.url.params["uccontinue"] == "x"


@respx.mock
def test_api_error_returns_502():
    respx.get(API_URL).respond(200, json={"error": {"info": "bad user"}})
    with TestClient(app) as client:
        resp = client.get("/api/articles")
    assert resp.status_code == 502
