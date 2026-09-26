from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import analysis, auth, citations
from .store import Store
from .wikipedia import API_URL, USER_AGENT, WatchedPage, WikipediaClient, WikipediaError

WIKI_USER = os.environ.get("WIKI_USER", "Phoe6")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["local"] = auth.is_local


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as http:
        app.state.wiki = WikipediaClient(
            http,
            api_url=os.environ.get("WIKI_API_URL", API_URL),
            bot_user=os.environ.get("WIKI_BOT_USER"),
            bot_password=os.environ.get("WIKI_BOT_PASSWORD"),
        )
        app.state.store = Store(os.environ.get("EDITOR_DB", "editor.db"))
        yield
        app.state.store.close()


app = FastAPI(title="Wikipedia Workbench", lifespan=lifespan)
# Added last = outermost, so the session is available to require_login.
app.middleware("http")(auth.require_login)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    same_site="lax",
    https_only=not auth.is_local(),
    max_age=30 * 24 * 3600,
)


def _wiki(request: Request) -> WikipediaClient:
    return request.app.state.wiki


def _store(request: Request) -> Store:
    return request.app.state.store


def _upstream_error(e: Exception) -> HTTPException:
    return HTTPException(502, f"Wikipedia API error: {e}")


# -- login -----------------------------------------------------------------


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": error})


@app.post("/login")
async def login(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    stored = auth.password_hash()
    if not stored or not auth.verify_password(password, stored):
        await asyncio.sleep(1)  # slow down guessing
        return RedirectResponse(f"/login?{urlencode({'next': next, 'error': 'Wrong password'})}", 303)
    request.session["user"] = "owner"
    return RedirectResponse(_safe_next(next), status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# -- models ----------------------------------------------------------------


class ArticleOut(BaseModel):
    title: str
    pageid: int
    namespace: int
    edit_count: int
    first_edit: datetime
    last_edit: datetime
    url: str


class ContributionOut(BaseModel):
    revid: int
    pageid: int
    title: str
    namespace: int
    timestamp: datetime
    comment: str
    size: int
    is_top: bool


class WatchedPageOut(BaseModel):
    title: str
    namespace: int
    url: str
    my_edits: int
    my_last_edit: datetime | None
    page_last_edit: datetime | None
    page_last_editor: str | None
    missing: bool
    focus: bool


class FocusOut(BaseModel):
    title: str
    note: str
    added_at: datetime


# -- contributions ---------------------------------------------------------


def _namespace(ns: Literal["articles", "all"]) -> int | None:
    return 0 if ns == "articles" else None


@app.get("/api/articles", response_model=list[ArticleOut])
async def list_articles(
    request: Request,
    ns: Literal["articles", "all"] = "articles",
    max_edits: int = Query(5000, ge=1, le=50000),
    sort: Literal["recent", "edits", "title"] = "recent",
):
    """Distinct Wikipedia pages edited by the configured user."""
    try:
        articles = await _wiki(request).articles(WIKI_USER, _namespace(ns), max_edits)
    except (httpx.HTTPError, WikipediaError) as e:
        raise _upstream_error(e)
    if sort == "edits":
        articles = sorted(articles, key=lambda a: a.edit_count, reverse=True)
    elif sort == "title":
        articles = sorted(articles, key=lambda a: a.title.lower())
    return [asdict(a) for a in articles]


@app.get("/api/contributions", response_model=list[ContributionOut])
async def list_contributions(
    request: Request,
    ns: Literal["articles", "all"] = "articles",
    limit: int = Query(100, ge=1, le=5000),
):
    """Raw edit history (newest first) for the configured user."""
    try:
        contribs = await _wiki(request).contributions(WIKI_USER, _namespace(ns), limit)
    except (httpx.HTTPError, WikipediaError) as e:
        raise _upstream_error(e)
    return [asdict(c) for c in contribs]


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, msg: str = ""):
    """Start here: pick an article to improve."""
    return templates.TemplateResponse(request, "home.html", {
        "user": WIKI_USER, "msg": msg, "focus": _store(request).focus_items(),
    })


@app.get("/articles", response_class=HTMLResponse)
async def articles_page(request: Request, sort: Literal["recent", "edits", "title"] = "recent"):
    articles = await list_articles(request, ns="articles", max_edits=5000, sort=sort)
    return templates.TemplateResponse(request, "articles.html", {
        "user": WIKI_USER, "articles": articles, "focus": _store(request).focus_titles(),
    })


# -- watchlist -------------------------------------------------------------

WatchFilter = Literal["all", "stale", "never", "missing", "focus"]


def _filter_pages(
    pages: list[WatchedPage], focus: set[str], show: WatchFilter, years: int
) -> list[WatchedPage]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=365 * years)
    match show:
        case "never":
            return [p for p in pages if p.my_edits == 0]
        case "stale":
            return [p for p in pages if p.my_last_edit and p.my_last_edit < cutoff]
        case "missing":
            return [p for p in pages if p.missing]
        case "focus":
            return [p for p in pages if p.title in focus]
    return pages


def _sort_pages(pages: list[WatchedPage], sort: str) -> list[WatchedPage]:
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "title":
        return sorted(pages, key=lambda p: (p.namespace, p.title.lower()))
    if sort == "activity":
        return sorted(pages, key=lambda p: p.page_last_edit or oldest)
    # Default: pages you touched least recently first — the likeliest to drop.
    return sorted(pages, key=lambda p: (p.my_last_edit or oldest, p.title.lower()))


async def _watched(request: Request) -> list[WatchedPage]:
    wiki = _wiki(request)
    if not wiki.can_authenticate:
        raise HTTPException(503, "Set WIKI_BOT_USER and WIKI_BOT_PASSWORD to read your watchlist")
    try:
        return await wiki.watched_pages(WIKI_USER)
    except (httpx.HTTPError, WikipediaError) as e:
        raise _upstream_error(e)


@app.get("/api/watchlist", response_model=list[WatchedPageOut])
async def api_watchlist(
    request: Request,
    show: WatchFilter = "all",
    years: int = Query(3, ge=1, le=30),
    sort: Literal["mine", "activity", "title"] = "mine",
):
    """Your watchlist with your own edit history per page, for triage."""
    focus = _store(request).focus_titles()
    pages = _sort_pages(_filter_pages(await _watched(request), focus, show, years), sort)
    return [{**asdict(p), "focus": p.title in focus} for p in pages]


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(
    request: Request,
    show: WatchFilter = "all",
    years: int = Query(3, ge=1, le=30),
    sort: Literal["mine", "activity", "title"] = "mine",
    msg: str = "",
):
    ctx = {"user": WIKI_USER, "show": show, "years": years, "sort": sort, "msg": msg}
    if not _wiki(request).can_authenticate:
        return templates.TemplateResponse(request, "watchlist.html", {**ctx, "configured": False})
    focus = _store(request).focus_titles()
    all_pages = await _watched(request)
    counts = {f: len(_filter_pages(all_pages, focus, f, years))
              for f in ("all", "stale", "never", "missing", "focus")}
    pages = _sort_pages(_filter_pages(all_pages, focus, show, years), sort)
    return templates.TemplateResponse(request, "watchlist.html", {
        **ctx, "configured": True, "pages": pages, "focus": focus, "counts": counts,
    })


def _back(request: Request, msg: str, default: str = "/watchlist") -> RedirectResponse:
    """Redirect to the page the form came from, with a flash message."""
    target = request.headers.get("referer") or default
    base, _, query = target.partition("?")
    params = {k: v for k, v in parse_qsl(query) if k != "msg"}
    params["msg"] = msg
    return RedirectResponse(f"{base}?{urlencode(params)}", status_code=303)


@app.post("/watchlist/unwatch")
async def unwatch(request: Request, titles: Annotated[list[str], Form()] = []):
    store = _store(request)
    protected = store.focus_titles()
    targets = [t for t in titles if t not in protected]
    if not targets:
        return _back(request, "Nothing to unwatch (focus pages are protected)")
    try:
        await _wiki(request).unwatch(targets)
    except (httpx.HTTPError, WikipediaError) as e:
        raise _upstream_error(e)
    skipped = len(titles) - len(targets)
    note = f"; skipped {skipped} focus page(s)" if skipped else ""
    return _back(request, f"Unwatched {len(targets)} page(s){note}")


# -- article analysis ------------------------------------------------------


def _title_from_input(value: str) -> str:
    """Accept a page title or a Wikipedia URL."""
    value = value.strip()
    if value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        if "/wiki/" in parts.path:
            value = parts.path.split("/wiki/", 1)[1]
        elif "title=" in parts.query:
            value = dict(parse_qsl(parts.query)).get("title", value)
    return unquote(value).replace("_", " ").strip()


async def _analyze(request: Request, title: str, archives: bool) -> dict:
    wiki = _wiki(request)
    try:
        page = await wiki.page_bundle(title)
    except (httpx.HTTPError, WikipediaError) as e:
        raise _upstream_error(e)
    if not page.exists:
        raise HTTPException(404, f"No page titled “{title}”")

    report = analysis.analyze_article(page.wikitext, page.hidden_categories)
    threads = analysis.analyze_talk(page.talk_wikitext)
    talk = analysis.talk_findings(threads, report.findings)

    async def my_edit_count() -> int:
        try:
            contribs = await wiki.contributions(WIKI_USER, namespace=None, max_edits=50000)
        except (httpx.HTTPError, WikipediaError):
            return 0
        return sum(1 for c in contribs if c.title in (page.title, page.talk_title))

    lint, archive_pages, views, mine = await asyncio.gather(
        wiki.lint_errors(page.title), wiki.talk_archives(page.talk_title),
        wiki.pageviews(page.title), my_edit_count(), return_exceptions=True)
    lint = lint if isinstance(lint, list) else []
    archive_pages = archive_pages if isinstance(archive_pages, list) else []
    lint_findings = [analysis.Finding("lint", "medium", f"Markup error: {e['category']}",
                                      detail=str(e.get("templateInfo", {}).get("name", "")))
                     for e in lint]

    snapshots: dict[int, str | None] = {}
    if archives:
        need = [r for r in report.references if r.url and not r.has_archive][:40]
        sem = asyncio.Semaphore(5)

        async def lookup(r):
            async with sem:
                snapshots[r.index] = await wiki.wayback(
                    r.url, analysis.wayback_timestamp(r.access_date))

        await asyncio.gather(*(lookup(r) for r in need))

    with_archive = {i: citations.add_archive(r.raw, snapshots[i])
                    for i, r in enumerate(report.references) if snapshots.get(i)}

    order = {"high": 0, "medium": 1, "low": 2}
    everything = sorted(talk + lint_findings + report.findings, key=lambda f: order[f.severity])
    dismissed_keys = _store(request).dismissed(page.title)
    suggestions = [f for f in everything if f.key not in dismissed_keys]
    dismissed = [f for f in everything if f.key in dismissed_keys]
    return {
        "page": page, "report": report, "threads": threads, "suggestions": suggestions,
        "dismissed": dismissed,
        "archive_pages": archive_pages, "views": views if isinstance(views, int) else None,
        "my_edits": mine if isinstance(mine, int) else 0, "snapshots": snapshots,
        "archives": archives, "page_url": wiki.page_url, "with_archive": with_archive,
    }


@app.get("/api/article")
async def api_article(request: Request, title: str, archives: bool = False):
    """Suggested improvements for an article, from the page and its talk page."""
    ctx = await _analyze(request, _title_from_input(title), archives)
    page = ctx["page"]
    return {
        "title": page.title, "revid": page.revid, "assessments": page.assessments,
        "stats": ctx["report"].stats, "views_30d": ctx["views"], "my_edits": ctx["my_edits"],
        "suggestions": [{**asdict(f), "key": f.key} for f in ctx["suggestions"]],
        "dismissed": [{**asdict(f), "key": f.key} for f in ctx["dismissed"]],
        "references": [{**asdict(r), "snapshot": ctx["snapshots"].get(r.index)}
                       for r in ctx["report"].references],
        "talk_threads": [asdict(t) for t in ctx["threads"]],
        "talk_archives": ctx["archive_pages"],
    }


@app.get("/article", response_class=HTMLResponse)
async def article_page(request: Request, title: str = "", archives: bool = False, msg: str = ""):
    title = _title_from_input(title)
    if not title:
        return RedirectResponse("/focus", status_code=303)
    ctx = await _analyze(request, title, archives)
    return templates.TemplateResponse(request, "article.html", {
        **ctx, "user": WIKI_USER, "msg": msg,
        "in_focus": ctx["page"].title in _store(request).focus_titles(),
    })


@app.post("/article/dismiss")
async def dismiss_finding(
    request: Request, title: Annotated[str, Form()], key: Annotated[str, Form()],
    message: Annotated[str, Form()] = "",
):
    """Mark a suggestion as reviewed so it stops appearing for this article."""
    _store(request).dismiss(title, key, message)
    return _back(request, f"Dismissed “{message}”", default=f"/article?{urlencode({'title': title})}")


@app.post("/article/restore")
async def restore_finding(
    request: Request, title: Annotated[str, Form()], key: Annotated[str, Form()],
):
    _store(request).restore(title, key)
    return _back(request, "Suggestion restored", default=f"/article?{urlencode({'title': title})}")


# -- citation builder ------------------------------------------------------

CITE_TEMPLATES = ["cite web", "cite journal", "cite book", "cite thesis", "cite news",
                  "cite conference"]


def _guess_template(source: str, is_url: bool) -> str:
    if not is_url:
        return "cite book"
    if re.search(r"thesis|dissertation", source, re.I):
        return "cite thesis"
    return "cite web"


@app.get("/cite", response_class=HTMLResponse)
async def cite_page(
    request: Request,
    source: str = "",
    pages: str = "",
    name: str | None = None,
    template: str = "",
    archive: bool = False,
    article: str = "",
    section_no: int | None = None,
):
    """Turn a URL, DOI or ISBN into citation wikitext to copy into Wikipedia."""
    wiki = _wiki(request)
    source = source.strip()
    wikitext, item, archive_url, note = "", None, "", ""
    if source:
        is_url = source.startswith(("http://", "https://"))
        if not template:
            item = await wiki.citoid(source)
        if archive and is_url:
            archive_url = await wiki.wayback(source) or ""
            if not archive_url:
                note = "No Wayback Machine snapshot yet. Use “Save page now” and look up again."
        if item:
            if name is None:
                name = citations.suggest_ref_name(item)
            wikitext = citations.to_cs1(item, pages=pages, ref_name=name,
                                        archive_url=archive_url)
            template = citations.TEMPLATES.get(item.get("itemType", ""), "cite web")
        else:
            template = template or _guess_template(source, is_url)
            wikitext = citations.blank(template, url=source if is_url else "", pages=pages,
                                       ref_name=name or "", archive_url=archive_url)
            if not note:
                note = ("Wikipedia's citation service couldn't read this source, so fill in "
                        "the blanks by hand. Empty parameters can be deleted.")
    return templates.TemplateResponse(request, "cite.html", {
        "user": WIKI_USER, "source": source, "pages": pages, "name": name or "",
        "template": template, "templates_list": CITE_TEMPLATES, "archive": archive,
        "wikitext": wikitext, "item": item, "note": note, "archive_url": archive_url,
        "article": article, "section_no": section_no, "page_url": wiki.page_url,
        "msg": "",
    })


# -- focus list ------------------------------------------------------------


@app.get("/api/focus", response_model=list[FocusOut])
async def api_focus(request: Request):
    return [asdict(f) for f in _store(request).focus_items()]


@app.get("/focus", response_class=HTMLResponse)
async def focus_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "focus.html", {
        "user": WIKI_USER, "msg": msg, "items": _store(request).focus_items(),
        "page_url": _wiki(request).page_url,
    })


@app.post("/focus/add")
async def focus_add(request: Request, title: Annotated[str, Form()], note: Annotated[str, Form()] = ""):
    title = title.strip()
    if title:
        _store(request).add_focus(title, note.strip())
    return _back(request, f"Added “{title}” to focus", default="/focus")


@app.post("/focus/remove")
async def focus_remove(request: Request, title: Annotated[str, Form()]):
    _store(request).remove_focus(title)
    return _back(request, f"Removed “{title}” from focus", default="/focus")
