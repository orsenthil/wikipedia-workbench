import httpx
import respx
from fastapi.testclient import TestClient

from editor.app import app

WIKITEXT = """{{Short description|X}}
Lead paragraph that is long enough not to be flagged as too short by the structure check in the analysis code, so it keeps going for a bit longer than that.

== History ==
Fact.<ref>{{cite web |url=https://github.com/a/b |title=Doc |access-date=2020-01-01}}</ref>

== References ==
{{Reflist}}
"""


def fake_wikipedia(request: httpx.Request) -> httpx.Response:
    p = dict(request.url.params)
    if "rest_v1/metrics" in str(request.url):
        return httpx.Response(200, json={"items": []})
    if p.get("prop", "").startswith("revisions|categories"):
        return httpx.Response(200, json={"query": {"pages": [{
            "title": "Thing", "ns": 0, "revisions": [{
                "revid": 1, "timestamp": "2026-09-01T00:00:00Z",
                "slots": {"main": {"content": WIKITEXT}}}]}]}})
    if p.get("list") == "linterrors":
        return httpx.Response(200, json={"query": {"linterrors": []}})
    if p.get("list") == "allpages":
        return httpx.Response(200, json={"query": {"allpages": []}})
    if p.get("list") == "usercontribs":
        return httpx.Response(200, json={"query": {"usercontribs": []}})
    raise AssertionError(f"unexpected {request.url}")


@respx.mock
def test_dismiss_and_restore():
    respx.route().mock(side_effect=fake_wikipedia)
    with TestClient(app) as client:
        data = client.get("/api/article", params={"title": "Thing"}).json()
        github = next(f for f in data["suggestions"] if "github.com" in f["message"])
        assert data["dismissed"] == []

        client.post("/article/dismiss", data={"title": "Thing", "key": github["key"],
                                              "message": github["message"]})
        data = client.get("/api/article", params={"title": "Thing"}).json()
        assert github["key"] not in {f["key"] for f in data["suggestions"]}
        assert [f["key"] for f in data["dismissed"]] == [github["key"]]
        assert "Dismissed (1)" in client.get("/article", params={"title": "Thing"}).text

        client.post("/article/restore", data={"title": "Thing", "key": github["key"]})
        data = client.get("/api/article", params={"title": "Thing"}).json()
        assert github["key"] in {f["key"] for f in data["suggestions"]}
        assert data["dismissed"] == []
