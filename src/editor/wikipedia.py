"""Thin async client for the MediaWiki API: contributions and watchlist."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import httpx

API_URL = "https://en.wikipedia.org/w/api.php"
# Wikimedia requires a descriptive User-Agent: https://meta.wikimedia.org/wiki/User-Agent_policy
USER_AGENT = "WikipediaWorkbench/0.1 (personal project; https://en.wikipedia.org/wiki/User:Phoe6)"
PAGE_SIZE = 500  # max allowed per request for non-bot users
TITLES_PER_REQUEST = 50  # max titles in one multi-value parameter


@dataclass
class Contribution:
    revid: int
    pageid: int
    title: str
    namespace: int
    timestamp: datetime
    comment: str
    size: int
    is_top: bool  # this edit is still the latest revision of the page


@dataclass
class Article:
    title: str
    pageid: int
    namespace: int
    edit_count: int
    first_edit: datetime
    last_edit: datetime
    url: str


@dataclass
class WatchedPage:
    title: str
    namespace: int
    url: str
    my_edits: int  # edits to the page plus its talk page
    my_last_edit: datetime | None
    page_last_edit: datetime | None
    page_last_editor: str | None
    missing: bool  # page no longer exists (deleted or never created)


@dataclass
class PageBundle:
    """An article and its talk page, fetched together."""

    title: str
    exists: bool
    revid: int | None
    timestamp: datetime | None
    wikitext: str
    hidden_categories: list[str]
    assessments: dict[str, dict]  # WikiProject -> {"class", "importance"}
    talk_title: str
    talk_wikitext: str
    talk_revid: int | None


class WikipediaError(Exception):
    def __init__(self, code: str, info: str):
        super().__init__(f"{code}: {info}")
        self.code = code


class WikipediaClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_url: str = API_URL,
        bot_user: str | None = None,
        bot_password: str | None = None,
        cache_ttl: float = 600.0,
    ):
        self._http = http
        self.api_url = api_url
        self._bot_user = bot_user
        self._bot_password = bot_password
        self._cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._logged_in = False

    @property
    def can_authenticate(self) -> bool:
        return bool(self._bot_user and self._bot_password)

    def page_url(self, title: str) -> str:
        path = quote(title.replace(" ", "_"), safe=":/(),'!*")
        return self.api_url.removesuffix("/w/api.php") + "/wiki/" + path

    # -- low level ---------------------------------------------------------

    async def _call(self, method: str, params: dict, auth: bool = False) -> dict:
        params = {**params, "format": "json", "formatversion": 2}
        if auth:
            if not self._logged_in:
                await self._login()
            params["assert"] = "user"  # fail loudly instead of acting anonymously
        for attempt in range(2):
            if method == "GET":
                resp = await self._http.get(self.api_url, params=params)
            else:
                resp = await self._http.post(self.api_url, data=params)
            resp.raise_for_status()
            data = resp.json()
            err = data.get("error")
            if err and err.get("code") == "assertuserfailed" and attempt == 0:
                await self._login()  # session expired; log in again and retry once
                continue
            if err:
                raise WikipediaError(err.get("code", "unknown"), err.get("info", ""))
            return data
        raise AssertionError("unreachable")

    async def _login(self) -> None:
        if not self.can_authenticate:
            raise WikipediaError("notconfigured", "WIKI_BOT_USER / WIKI_BOT_PASSWORD not set")
        token = await self._token("login")
        data = await self._call(
            "POST",
            {"action": "login", "lgname": self._bot_user,
             "lgpassword": self._bot_password, "lgtoken": token},
        )
        result = data.get("login", {})
        if result.get("result") != "Success":
            raise WikipediaError("loginfailed", result.get("reason", str(result)))
        self._logged_in = True

    async def _token(self, kind: str) -> str:
        data = await self._call("GET", {"action": "query", "meta": "tokens", "type": kind})
        return data["query"]["tokens"][f"{kind}token"]

    def _cached(self, key: tuple):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._cache_ttl:
            return hit[1]
        return None

    def _store(self, key: tuple, value):
        self._cache[key] = (time.monotonic(), value)
        return value

    # -- contributions -----------------------------------------------------

    async def contributions(
        self, user: str, namespace: int | None = 0, max_edits: int = 5000
    ) -> list[Contribution]:
        """Fetch up to `max_edits` of `user`'s edits, newest first.

        namespace=0 limits results to articles; None returns every namespace.
        """
        key = ("contribs", user, namespace, max_edits)
        if (cached := self._cached(key)) is not None:
            return cached

        params: dict[str, str | int] = {
            "action": "query",
            "list": "usercontribs",
            "ucuser": user,
            "ucprop": "ids|title|timestamp|comment|size|flags",
        }
        if namespace is not None:
            params["ucnamespace"] = namespace

        results: list[Contribution] = []
        while len(results) < max_edits:
            params["uclimit"] = min(PAGE_SIZE, max_edits - len(results))
            data = await self._call("GET", params)
            for c in data["query"]["usercontribs"]:
                results.append(
                    Contribution(
                        revid=c["revid"],
                        pageid=c["pageid"],
                        title=c["title"],
                        namespace=c["ns"],
                        timestamp=datetime.fromisoformat(c["timestamp"]),
                        comment=c.get("comment", ""),
                        size=c.get("size", 0),
                        is_top=bool(c.get("top", False)),
                    )
                )
            cont = data.get("continue")
            if not cont:
                break
            params.update(cont)

        return self._store(key, results)

    async def articles(
        self, user: str, namespace: int | None = 0, max_edits: int = 5000
    ) -> list[Article]:
        """Distinct pages the user has edited, most recently edited first."""
        by_page: dict[int, Article] = {}
        for c in await self.contributions(user, namespace, max_edits):
            art = by_page.get(c.pageid)
            if art is None:
                by_page[c.pageid] = Article(
                    title=c.title,
                    pageid=c.pageid,
                    namespace=c.namespace,
                    edit_count=1,
                    first_edit=c.timestamp,
                    last_edit=c.timestamp,
                    url=self.page_url(c.title),
                )
            else:
                art.edit_count += 1
                art.first_edit = min(art.first_edit, c.timestamp)
                art.last_edit = max(art.last_edit, c.timestamp)
        return sorted(by_page.values(), key=lambda a: a.last_edit, reverse=True)

    # -- watchlist ---------------------------------------------------------

    async def watchlist_titles(self) -> list[tuple[int, str]]:
        """(namespace, title) for every subject page on the logged-in user's watchlist.

        MediaWiki always watches a page and its talk page together, so talk
        pages are dropped here and handled via their subject page.
        """
        if (cached := self._cached(("watchlist",))) is not None:
            return cached
        params: dict[str, str | int] = {
            "action": "query", "list": "watchlistraw", "wrlimit": "max",
        }
        pages: list[tuple[int, str]] = []
        while True:
            data = await self._call("GET", params, auth=True)
            for p in data.get("watchlistraw", []):
                if p["ns"] % 2 == 0:
                    pages.append((p["ns"], p["title"]))
            cont = data.get("continue")
            if not cont:
                break
            params.update(cont)
        return self._store(("watchlist",), pages)

    async def latest_revisions(self, titles: list[str]) -> dict[str, dict | None]:
        """title -> {"timestamp", "user"} of its latest revision, or None if missing."""
        out: dict[str, dict | None] = {}
        for i in range(0, len(titles), TITLES_PER_REQUEST):
            chunk = titles[i : i + TITLES_PER_REQUEST]
            data = await self._call("GET", {
                "action": "query", "prop": "revisions", "rvprop": "timestamp|user",
                "titles": "|".join(chunk),
            })
            query = data.get("query", {})
            # Map normalized titles back to what we asked for.
            renamed = {n["to"]: n["from"] for n in query.get("normalized", [])}
            for p in query.get("pages", []):
                title = renamed.get(p["title"], p["title"])
                revs = p.get("revisions")
                out[title] = None if p.get("missing") or not revs else revs[0]
        return out

    async def watched_pages(self, user: str) -> list[WatchedPage]:
        """Watchlist joined with the user's own edit history and page activity."""
        titles = await self.watchlist_titles()

        mine: dict[tuple[int, str], list[datetime]] = {}
        for c in await self.contributions(user, namespace=None, max_edits=50000):
            mine.setdefault(_subject_key(c.namespace, c.title), []).append(c.timestamp)

        revs = await self.latest_revisions([t for _, t in titles])

        pages = []
        for ns, title in titles:
            edits = mine.get(_subject_key(ns, title), [])
            rev = revs.get(title)
            pages.append(WatchedPage(
                title=title,
                namespace=ns,
                url=self.page_url(title),
                my_edits=len(edits),
                my_last_edit=max(edits) if edits else None,
                page_last_edit=datetime.fromisoformat(rev["timestamp"]) if rev else None,
                page_last_editor=rev.get("user") if rev else None,
                missing=rev is None,
            ))
        return pages

    async def unwatch(self, titles: list[str]) -> None:
        """Remove pages (and their talk pages) from the watchlist, in batches of 50."""
        token = await self._token_authed("watch")
        for i in range(0, len(titles), TITLES_PER_REQUEST):
            await self._call("POST", {
                "action": "watch", "unwatch": 1, "token": token,
                "titles": "|".join(titles[i : i + TITLES_PER_REQUEST]),
            }, auth=True)
        self._cache.pop(("watchlist",), None)

    # -- single article ----------------------------------------------------

    async def page_bundle(self, title: str) -> PageBundle:
        """Current wikitext, tracking categories and assessments for a page and its talk page."""
        data = await self._call("GET", {
            "action": "query", "titles": title, "redirects": 1,
            "prop": "revisions|categories|pageassessments|info",
            "rvprop": "content|ids|timestamp", "rvslots": "main",
            "clshow": "hidden", "cllimit": "max", "inprop": "talkid",
        })
        query = data["query"]
        page = query["pages"][0]
        title = page["title"]
        talk_title = _talk_title(title, page["ns"])
        rev = (page.get("revisions") or [{}])[0]

        talk_text, talk_revid = "", None
        if page.get("talkid"):
            tdata = await self._call("GET", {
                "action": "query", "pageids": page["talkid"], "prop": "revisions",
                "rvprop": "content|ids", "rvslots": "main",
            })
            trev = (tdata["query"]["pages"][0].get("revisions") or [{}])[0]
            talk_text = trev.get("slots", {}).get("main", {}).get("content", "")
            talk_revid = trev.get("revid")

        return PageBundle(
            title=title,
            exists=not page.get("missing"),
            revid=rev.get("revid"),
            timestamp=datetime.fromisoformat(rev["timestamp"]) if rev.get("timestamp") else None,
            wikitext=rev.get("slots", {}).get("main", {}).get("content", ""),
            hidden_categories=[c["title"] for c in page.get("categories", [])],
            assessments=page.get("pageassessments", {}),
            talk_title=talk_title,
            talk_wikitext=talk_text,
            talk_revid=talk_revid,
        )

    async def lint_errors(self, title: str) -> list[dict]:
        """Markup errors Wikipedia's Linter has recorded for the page."""
        data = await self._call("GET", {
            "action": "query", "list": "linterrors", "lnttitle": title, "lntlimit": "max",
        })
        return data.get("query", {}).get("linterrors", [])

    async def talk_archives(self, talk_title: str) -> list[str]:
        ns_name, _, bare = talk_title.partition(":")
        data = await self._call("GET", {
            "action": "query", "list": "allpages", "apnamespace": _talk_ns(ns_name),
            "apprefix": f"{bare}/", "aplimit": 50,
        })
        return [p["title"] for p in data["query"]["allpages"]
                if "archive" in p["title"].lower()]

    async def pageviews(self, title: str, days: int = 30) -> int | None:
        """Total human pageviews over the last `days` days (Wikimedia REST API)."""
        end = datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        article = quote(title.replace(" ", "_"), safe="")
        project = urlsplit(self.api_url).netloc
        url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/"
               f"all-access/user/{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}")
        try:
            resp = await self._http.get(url)
            if resp.status_code == 404:
                return 0
            resp.raise_for_status()
            return sum(i["views"] for i in resp.json().get("items", []))
        except httpx.HTTPError:
            return None

    async def wayback(self, url: str, near: str = "") -> str | None:
        """Closest Internet Archive snapshot for `url`, or None."""
        params = {"url": url}
        if near:
            params["timestamp"] = near
        try:
            resp = await self._http.get("https://archive.org/wayback/available",
                                        params=params, timeout=15)
            resp.raise_for_status()
            snap = resp.json().get("archived_snapshots", {}).get("closest")
        except (httpx.HTTPError, ValueError):
            return None
        if snap and snap.get("available") and str(snap.get("status", "200")).startswith("2"):
            return snap["url"].replace("http://web.archive.org", "https://web.archive.org")
        return None

    async def citoid(self, source: str) -> dict | None:
        """Citation metadata for a URL, DOI, ISBN or PMID; None if Citoid can't read it."""
        base = self.api_url.removesuffix("/w/api.php")
        url = f"{base}/api/rest_v1/data/citation/mediawiki/{quote(source.strip(), safe='')}"
        try:
            resp = await self._http.get(url, timeout=20)
            if resp.status_code >= 400:
                return None
            items = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return items[0] if isinstance(items, list) and items else None

    async def _token_authed(self, kind: str) -> str:
        if not self._logged_in:
            await self._login()
        return await self._token(kind)


NAMESPACES = {"": 0, "Talk": 1, "User": 2, "User talk": 3, "Wikipedia": 4, "Wikipedia talk": 5,
              "File": 6, "File talk": 7, "Template": 10, "Template talk": 11,
              "Help": 12, "Help talk": 13, "Category": 14, "Category talk": 15,
              "Portal": 100, "Portal talk": 101, "Draft": 118, "Draft talk": 119}


def _talk_title(title: str, ns: int) -> str:
    if ns % 2 == 1:
        return title
    if ns == 0:
        return f"Talk:{title}"
    prefix, _, bare = title.partition(":")
    return f"{prefix} talk:{bare}"


def _talk_ns(ns_name: str) -> int:
    return NAMESPACES.get(ns_name, 1)


def _subject_key(ns: int, title: str) -> tuple[int, str]:
    """Key that treats a page and its talk page as the same thing."""
    bare = title.split(":", 1)[1] if ns != 0 else title
    return (ns & ~1, bare)
