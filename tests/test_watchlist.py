from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from editor.app import app
from editor.wikipedia import API_URL


class FakeWiki:
    """Just enough of the MediaWiki API to exercise login, watchlist and watch."""

    def __init__(self):
        self.logged_in = False
        self.watched = {(0, "Python"), (1, "Talk:Python"), (0, "Old page"),
                        (1, "Talk:Old page"), (0, "Gone"), (2, "User:Phoe6")}
        self.watch_posts = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            p = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        else:
            p = dict(request.url.params)
        if p.get("assert") == "user" and not self.logged_in:
            return httpx.Response(200, json={"error": {"code": "assertuserfailed", "info": ""}})

        if p.get("meta") == "tokens":
            return httpx.Response(200, json={"query": {"tokens": {f"{p['type']}token": "tok"}}})
        if p.get("action") == "login":
            ok = p["lgpassword"] == "secret"
            self.logged_in = ok
            return httpx.Response(200, json={"login": {"result": "Success" if ok else "Failed"}})
        if p.get("list") == "usercontribs":
            return httpx.Response(200, json={"query": {"usercontribs": [
                _c("Python", 0, "2026-09-01T00:00:00Z"),
                _c("Talk:Python", 1, "2026-08-01T00:00:00Z"),
                _c("Old page", 0, "2015-01-01T00:00:00Z"),
            ]}})
        if p.get("list") == "watchlistraw":
            return httpx.Response(200, json={"watchlistraw": [
                {"ns": ns, "title": t} for ns, t in sorted(self.watched)]})
        if p.get("prop") == "revisions":
            pages = []
            for t in p["titles"].split("|"):
                if t == "Gone":
                    pages.append({"title": t, "missing": True})
                else:
                    pages.append({"title": t, "revisions": [
                        {"timestamp": "2026-09-10T00:00:00Z", "user": "Someone"}]})
            return httpx.Response(200, json={"query": {"pages": pages}})
        if p.get("action") == "watch":
            self.watch_posts.append(p)
            titles = p["titles"].split("|")
            if p.get("unwatch"):
                self.watched = {(ns, t) for ns, t in self.watched
                                if t not in titles and t.removeprefix("Talk:") not in titles}
            return httpx.Response(200, json={"watch": [{"title": t} for t in titles]})
        raise AssertionError(f"unexpected request {p}")


def _c(title, ns, ts):
    return {"revid": 1, "pageid": 1, "ns": ns, "title": title, "timestamp": ts,
            "comment": "", "size": 1}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("WIKI_BOT_USER", "Phoe6@editor")
    monkeypatch.setenv("WIKI_BOT_PASSWORD", "secret")
    wiki = FakeWiki()
    with respx.mock:
        respx.route(url__startswith=API_URL).mock(side_effect=wiki)
        yield wiki


def test_watchlist_joins_edits_and_collapses_talk_pages(fake):
    with TestClient(app) as client:
        data = client.get("/api/watchlist?sort=title").json()
    by_title = {p["title"]: p for p in data}
    assert set(by_title) == {"Python", "Old page", "Gone", "User:Phoe6"}
    assert by_title["Python"]["my_edits"] == 2  # article + talk page
    assert by_title["Gone"]["missing"] is True
    assert by_title["User:Phoe6"]["my_edits"] == 0


def test_filters(fake):
    with TestClient(app) as client:
        titles = lambda show: {p["title"] for p in client.get(f"/api/watchlist?show={show}").json()}
        assert titles("stale") == {"Old page"}
        assert titles("never") == {"Gone", "User:Phoe6"}
        assert titles("missing") == {"Gone"}


def test_unwatch_skips_focus_pages(fake):
    with TestClient(app) as client:
        client.post("/focus/add", data={"title": "Python"})
        resp = client.post("/watchlist/unwatch", data={"titles": ["Python", "Old page"]})
        assert resp.status_code == 200 and "Unwatched 1 page" in resp.text
        assert fake.watch_posts[-1]["titles"] == "Old page"
        assert fake.watch_posts[-1]["unwatch"] == "1"
        remaining = {p["title"] for p in client.get("/api/watchlist").json()}
        assert "Old page" not in remaining and "Python" in remaining


def test_watchlist_page_without_credentials_shows_setup(monkeypatch, tmp_path):
    monkeypatch.delenv("WIKI_BOT_USER", raising=False)
    monkeypatch.delenv("WIKI_BOT_PASSWORD", raising=False)
    with TestClient(app) as client:
        resp = client.get("/watchlist")
    assert resp.status_code == 200 and "Special:BotPasswords" in resp.text
