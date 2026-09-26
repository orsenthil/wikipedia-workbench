"""Build CS1 citation wikitext for copying into Wikipedia by hand.

Metadata comes from Citoid (the service behind the visual editor's Cite
button). Everything produced here is a starting point to check and edit.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import mwparserfromhell as mwp

TEMPLATES = {
    "webpage": "cite web",
    "blogPost": "cite web",
    "forumPost": "cite web",
    "journalArticle": "cite journal",
    "preprint": "cite journal",
    "book": "cite book",
    "bookSection": "cite book",
    "thesis": "cite thesis",
    "newspaperArticle": "cite news",
    "magazineArticle": "cite magazine",
    "conferencePaper": "cite conference",
    "report": "cite report",
    "encyclopediaArticle": "cite encyclopedia",
    "videoRecording": "cite AV media",
    "podcast": "cite podcast",
    "presentation": "cite speech",
}

# Parameters offered in a blank template when there's no metadata to start from.
SKELETONS = {
    "cite web": ["last", "first", "title", "website", "publisher", "date", "url", "access-date"],
    "cite journal": ["last", "first", "title", "journal", "volume", "issue", "pages", "date",
                     "doi", "url", "access-date"],
    "cite book": ["last", "first", "title", "edition", "publisher", "location", "date",
                  "pages", "isbn", "url"],
    "cite thesis": ["last", "first", "title", "degree", "publisher", "date", "pages", "url",
                    "access-date"],
    "cite news": ["last", "first", "title", "work", "date", "url", "access-date"],
    "cite conference": ["last", "first", "title", "conference", "book-title", "publisher",
                        "date", "pages", "doi", "url"],
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def to_cs1(
    item: dict,
    pages: str = "",
    ref_name: str = "",
    archive_url: str = "",
    today: date | None = None,
) -> str:
    """Citoid item -> `<ref name=…>{{cite …}}</ref>`."""
    kind = item.get("itemType", "webpage")
    template = TEMPLATES.get(kind, "cite web")
    params: list[tuple[str, str]] = []

    people = item.get("author") or []
    for i, person in enumerate(people, start=1):
        first, last = (person + ["", ""])[:2] if isinstance(person, list) else ("", person)
        n = "" if len(people) == 1 else str(i)
        if last:
            params.append((f"last{n}", last))
        if first:
            params.append((f"first{n}", first))
    for i, person in enumerate(item.get("editor") or [], start=1):
        first, last = (person + ["", ""])[:2]
        params += [(f"editor-last{i}", last), (f"editor-first{i}", first)]

    title = item.get("title", "")
    site = _site_name(item.get("websiteTitle", ""))
    title, site = _split_site_from_title(title, site)

    if kind == "bookSection":
        params += [("chapter", title), ("title", item.get("bookTitle", ""))]
    else:
        params.append(("title", title))

    container = {
        "cite web": ("website", site or item.get("publicationTitle", "")),
        "cite journal": ("journal", item.get("publicationTitle", "")),
        "cite news": ("work", item.get("publicationTitle", "")),
        "cite magazine": ("magazine", item.get("publicationTitle", "")),
        "cite conference": ("conference", item.get("conferenceName", "")),
        "cite encyclopedia": ("encyclopedia", item.get("encyclopediaTitle", "")),
    }.get(template)
    if container:
        params.append(container)
    if kind == "conferencePaper" and item.get("proceedingsTitle"):
        params.append(("book-title", item["proceedingsTitle"]))
    if kind == "thesis":
        params.append(("degree", item.get("thesisType", "")))
        params.append(("publisher", item.get("university", "")))

    params += [
        ("edition", re.sub(r"\s*(ed\.?|edition)$", "", item.get("edition", ""), flags=re.I)),
        ("publisher", item.get("publisher", "") if kind != "thesis" else ""),
        ("location", item.get("place", "")),
        ("date", cs1_date(item.get("date", ""))),
        ("volume", item.get("volume", "")),
        ("issue", item.get("issue", "")),
    ]
    params.append(_pages_param(pages or item.get("pages", "")))
    if item.get("DOI"):
        params.append(("doi", item["DOI"]))
    isbn = item.get("ISBN")
    if isbn:
        params.append(("isbn", isbn[0] if isinstance(isbn, list) else isbn))

    url = item.get("url", "")
    if url and not (kind == "book" and isbn):  # catalogue links add nothing to a book cite
        params.append(("url", url))
        params.append(("access-date", (today or date.today()).isoformat()))
        if archive_url:
            params += archive_params(archive_url)

    return wrap_ref(render(template, params), ref_name)


def blank(template: str, url: str = "", pages: str = "", ref_name: str = "",
          archive_url: str = "", today: date | None = None) -> str:
    """Empty skeleton for when there's no metadata, pre-filled where possible."""
    names = SKELETONS.get(template, SKELETONS["cite web"])
    filled = {"url": url, "access-date": (today or date.today()).isoformat() if url else ""}
    params = [(n, filled.get(n, "")) for n in names]
    if pages:
        params = [p for p in params if p[0] != "pages"] + [_pages_param(pages)]
    if archive_url:
        params += archive_params(archive_url)
    return wrap_ref(render(template, params, keep_empty=True), ref_name)


def render(template: str, params: list[tuple[str, str]], keep_empty: bool = False) -> str:
    seen: set[str] = set()
    parts = []
    for name, value in params:
        if not name or name in seen or (not value and not keep_empty):
            continue
        seen.add(name)
        parts.append(f" |{name}={_escape(value)}")
    return "{{" + template + "".join(parts) + "}}"


def wrap_ref(citation: str, ref_name: str = "") -> str:
    name = ref_name.strip().replace('"', "")
    open_tag = f'<ref name="{name}">' if name else "<ref>"
    return f"{open_tag}{citation}</ref>"


def suggest_ref_name(item: dict) -> str:
    people = item.get("author") or []
    last = people[0][1] if people and isinstance(people[0], list) and len(people[0]) > 1 else ""
    year = re.search(r"\d{4}", item.get("date", ""))
    base = last or re.sub(r"[^A-Za-z0-9]", "", (item.get("title") or "")[:20])
    return re.sub(r"\s+", "", f"{base}{year.group(0) if year else ''}")


def archive_params(archive_url: str) -> list[tuple[str, str]]:
    return [("archive-url", archive_url), ("archive-date", archive_date(archive_url)),
            ("url-status", "live")]


def archive_date(archive_url: str) -> str:
    m = re.search(r"/web/(\d{8})", archive_url)
    return datetime.strptime(m.group(1), "%Y%m%d").date().isoformat() if m else ""


def add_archive(ref_wikitext: str, archive_url: str) -> str:
    """The same <ref> with archive-url/archive-date/url-status added, formatting kept."""
    code = mwp.parse(ref_wikitext)
    for t in code.filter_templates():
        name = str(t.name).strip().lower()
        if name.startswith(("cite", "citation")):
            for key, value in archive_params(archive_url):
                if not t.has(key):
                    t.add(key, value)
            return str(code)
    return ref_wikitext


def cs1_date(value: str) -> str:
    """Citoid gives ISO-ish dates; CS1 rejects "YYYY-MM", so spell the month out."""
    value = value.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})", value):
        return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return value


def _pages_param(pages: str) -> tuple[str, str]:
    pages = pages.strip().removeprefix("pp.").removeprefix("p.").strip()
    if not pages:
        return ("pages", "")
    if re.fullmatch(r"\d+\s*[-–]\s*\d+", pages):
        return ("pages", re.sub(r"\s*[-–]\s*", "–", pages))
    if re.fullmatch(r"[\divxlc]+", pages, re.I):
        return ("page", pages)
    return ("pages", pages)


def _site_name(site: str) -> str:
    return site.removeprefix("www.") if site else ""


def _split_site_from_title(title: str, site: str) -> tuple[str, str]:
    """ "Introducing the 2.6 Kernel | Linux Journal" -> ("Introducing the 2.6 Kernel", "Linux Journal")."""
    m = re.fullmatch(r"(.+?)\s+[|–—-]\s+([^|–—-]{2,60})", title)
    if m:
        name = m.group(2).strip()
        squashed = re.sub(r"[^a-z0-9]", "", name.lower())
        if squashed and squashed in re.sub(r"[^a-z0-9]", "", site.lower()):
            return m.group(1).strip(), name
    return title, site


def _escape(value: str) -> str:
    return str(value).replace("|", "{{!}}").replace("\n", " ").strip()
