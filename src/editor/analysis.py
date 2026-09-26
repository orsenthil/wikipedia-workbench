"""Mechanical checks on an article and its talk page.

Everything here is deterministic and works on wikitext: it points at places
worth your attention and never writes content.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import mwparserfromhell as mwp
from mwparserfromhell.nodes import Tag, Template
from mwparserfromhell.wikicode import Wikicode

# Inline tags that mark a specific problem in the text.
INLINE_ISSUES = {
    "citation needed": "Unsourced statement",
    "cn": "Unsourced statement",
    "fact": "Unsourced statement",
    "clarify": "Needs clarification",
    "who": "Vague attribution (who?)",
    "which": "Vague (which?)",
    "when": "Vague time (when?)",
    "by whom": "Vague attribution (by whom?)",
    "according to whom": "Vague attribution",
    "vague": "Vague wording",
    "dubious": "Disputed statement",
    "failed verification": "Source doesn't support the text",
    "better source needed": "Better source needed",
    "unreliable source?": "Possibly unreliable source",
    "page needed": "Page number needed",
    "dead link": "Dead link",
    "update inline": "Out of date",
    "citation needed span": "Unsourced statement",
    "specify": "Needs specifics",
    "example needed": "Example needed",
}

# Banners that flag the whole article or a section.
BANNER_ISSUES = {
    "more citations needed": "Needs more citations",
    "refimprove": "Needs more citations",
    "unreferenced": "No references",
    "unreferenced section": "Section has no references",
    "more citations needed section": "Section needs more citations",
    "primary sources": "Relies on primary sources",
    "one source": "Relies on a single source",
    "technical": "Too technical for general readers",
    "update": "Out of date",
    "update section": "Section out of date",
    "cleanup": "Needs cleanup",
    "copy edit": "Needs copy editing",
    "expand section": "Section needs expanding",
    "empty section": "Empty section",
    "lead too short": "Lead too short",
    "lead missing": "No lead section",
    "no footnotes": "References lack inline citations",
    "more footnotes needed": "Needs more inline citations",
    "original research": "May contain original research",
    "essay-like": "Written like an essay",
    "advert": "Reads like an advertisement",
    "peacock": "Promotional wording",
    "orphan": "Few other articles link here",
    "dead end": "Links to no other articles",
    "underlinked": "Needs more wikilinks",
    "overlinked": "Too many wikilinks",
    "notability": "Notability questioned",
    "multiple issues": "Multiple issues",
}

# Domains that are usually self-published; each use deserves a second look (WP:SPS).
SELF_PUBLISHED = (
    "github.com", "gitlab.com", "blogspot.", "wordpress.com", "medium.com", "substack.com",
    "perlmonks.org", "stackoverflow.com", "stackexchange.com", "quora.com", "reddit.com",
    "tumblr.com", "wikia.", "fandom.com", "youtube.com", "twitter.com", "x.com",
    "facebook.com", "linkedin.com", "wikipedia.org",
)

# Hidden categories that are routine bookkeeping, not problems.
BENIGN_CATEGORY = re.compile(
    r"Articles with short description|Short description |Webarchive template|"
    r"Use .* dates|Use .* English|Pages using|Articles with hCards|"
    r"Wikipedia articles incorporating|Commons category link|Official website"
)

NON_PROSE_SECTIONS = {"see also", "references", "notes", "footnotes", "citations",
                      "external links", "further reading", "bibliography", "sources",
                      "works cited", "notes and references"}

SIGNATURE_TS = re.compile(r"(\d{1,2}:\d{2}), (\d{1,2} \w+ \d{4}) \(UTC\)")
USER_LINK = re.compile(r"\[\[\s*(?:User(?: talk)?|Special:Contributions)\s*[:/]\s*([^|\]/#]+)", re.I)
URL = re.compile(r"https?://[^\s\]|}<>\"']+")
RESOLVED_TEMPLATES = {"resolved", "done", "archive top", "atop", "discussion top",
                      "closed rfc top", "hat", "archive bottom"}
EDIT_REQUEST_TEMPLATES = {"edit request", "edit semi-protected", "edit protected",
                          "edit extended-protected", "edit template-protected", "edit coi"}

MIN_UNCITED_CHARS = 200  # shorter paragraphs are often transitions or summaries
MIN_TRAILING_CHARS = 150  # uncited text after the paragraph's last citation


@dataclass
class Finding:
    kind: str  # "maintenance" | "citation" | "uncited" | "structure" | "lint"
    severity: str  # "high" | "medium" | "low"
    message: str
    section: str = ""
    snippet: str = ""
    detail: str = ""
    url: str = ""
    section_no: int | None = None  # for ?action=edit&section=N

    @property
    def key(self) -> str:
        """Stable id, so a dismissal still applies the next time the page is analysed."""
        basis = "\x1f".join((self.kind, self.message, self.section, self.url or self.snippet))
        return hashlib.sha1(basis.encode()).hexdigest()[:16]


@dataclass
class Reference:
    index: int
    section: str
    section_no: int
    template: str  # e.g. "cite web", or "" for a bare/untemplated ref
    url: str
    title: str
    has_archive: bool
    access_date: str
    raw: str
    problems: list[str] = field(default_factory=list)


@dataclass
class TalkThread:
    heading: str
    comments: int
    participants: list[str]
    first_comment: datetime | None
    last_comment: datetime | None
    resolved: bool
    edit_request: bool
    urls: list[str]
    snippet: str


@dataclass
class ArticleReport:
    findings: list[Finding]
    references: list[Reference]
    stats: dict


# -- helpers ---------------------------------------------------------------


def _name(t: Template) -> str:
    return str(t.name).strip().lower().replace("_", " ")


def _param(t: Template, *names: str) -> str:
    for n in names:
        if t.has(n):
            return t.get(n).value.strip_code().strip() if n != "url" else str(t.get(n).value).strip()
    return ""


def _plain(code: Wikicode | str, limit: int = 140) -> str:
    text = mwp.parse(str(code)).strip_code(normalize=True, collapse=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sections(code: Wikicode) -> list[tuple[str, int, Wikicode]]:
    """(heading, section number, body); the lead is ("", 0). Subsections are separate.

    Numbers follow MediaWiki's section editing: the lead is 0, then every
    heading in document order.
    """
    out = []
    number = 0
    for sec in code.get_sections(flat=True, include_lead=True):
        headings = sec.filter_headings(recursive=False)
        if headings and str(sec).lstrip().startswith("="):
            number += 1
            out.append((headings[0].title.strip_code().strip(), number, sec))
        else:
            out.append(("", 0, sec))
    return out


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _domain(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


# -- article ---------------------------------------------------------------


def analyze_article(wikitext: str, hidden_categories: list[str] | None = None) -> ArticleReport:
    code = mwp.parse(wikitext)
    findings: list[Finding] = []
    references: list[Reference] = []
    sections = _sections(code)

    for heading, number, body in sections:
        start = len(findings)
        _check_templates(body, heading, findings)
        references.extend(_collect_refs(body, heading, number, start=len(references)))
        if heading.lower() not in NON_PROSE_SECTIONS:
            _check_paragraphs(body, heading, findings)
        for f in findings[start:]:
            f.section_no = number

    findings.extend(_reference_findings(references))
    findings.extend(_structure_findings(code, sections))

    for cat in hidden_categories or []:
        name = cat.removeprefix("Category:")
        if not BENIGN_CATEGORY.search(name):
            findings.append(Finding("maintenance", "low", f"Tracking category: {name}",
                                    url="https://en.wikipedia.org/wiki/" + cat.replace(" ", "_")))

    stats = {
        "bytes": len(wikitext.encode()),
        "sections": len([h for h, _, _ in sections if h]),
        "references": len(references),
        "unique_sources": len({r.url for r in references if r.url} |
                              {r.raw for r in references if not r.url}),
    }
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order[f.severity])
    return ArticleReport(findings, references, stats)


def _check_templates(body: Wikicode, heading: str, findings: list[Finding]) -> None:
    for t in body.filter_templates(recursive=True):
        name = _name(t)
        date = _param(t, "date")
        detail = f"tagged {date}" if date else ""
        if name in INLINE_ISSUES:
            findings.append(Finding(
                "maintenance", "high", INLINE_ISSUES[name], heading,
                snippet=_context_before(body, t), detail=detail))
        elif name in BANNER_ISSUES:
            where = "Section" if heading else "Article"
            findings.append(Finding(
                "maintenance", "high", f"{where} banner: {BANNER_ISSUES[name]}", heading,
                detail=detail))


def _context_before(body: Wikicode, node) -> str:
    """The sentence a tag is attached to, as plain text."""
    text = str(body)
    idx = text.find(str(node))
    before = text[max(0, idx - 600) : idx]
    before = re.sub(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", "", before, flags=re.S)
    plain = _plain(before, limit=10**6)
    sentences = re.split(r"(?<=[.!?])\s+", plain)
    return ("…" + sentences[-1][-200:]) if sentences and sentences[-1] else ""


def _collect_refs(body: Wikicode, heading: str, number: int, start: int) -> list[Reference]:
    refs = []
    for tag in body.filter_tags(matches=lambda n: n.tag.lower() == "ref", recursive=True):
        if tag.self_closing or not tag.contents or not str(tag.contents).strip():
            continue  # reuse of a named ref
        refs.append(_reference(tag, heading, number, start + len(refs)))
    return refs


def _reference(tag: Tag, heading: str, number: int, index: int) -> Reference:
    contents = tag.contents
    cites = [t for t in contents.filter_templates(recursive=False)
             if _name(t).startswith(("cite", "citation"))]
    problems: list[str] = []
    if cites:
        t = cites[0]
        url = _param(t, "url")
        title = _param(t, "title")
        has_archive = bool(_param(t, "archive-url", "archiveurl"))
        tname = _name(t)
        access_date = _param(t, "access-date", "accessdate")
        if not title:
            problems.append("missing title")
        if not (_param(t, "date", "year") or _param(t, "access-date", "accessdate")):
            problems.append("no date or access-date")
        if not _param(t, "author", "last", "last1", "author1", "authors", "vauthors",
                      "editor", "publisher", "website", "work", "journal", "newspaper"):
            problems.append("no author, publisher or website")
        author = " ".join(str(t.get(p).value) for p in ("author", "last", "first")
                          if t.has(p))
        if "@" in author or "<" in author:
            problems.append("author field contains an email or markup")
        if "{{!}}" in str(t.get("title").value) if t.has("title") else False:
            problems.append("title includes the site name (\"Title | Site\")")
    else:
        tname = ""
        raw = str(contents).strip()
        m = URL.search(raw)
        url = m.group(0) if m else ""
        title = _plain(raw, 80)
        has_archive = "web.archive.org" in raw or "archive.today" in raw
        access_date = ""
        if url:
            problems.append("bare URL; convert to a citation template")
        else:
            problems.append("free-text citation; consider a citation template")

    if url.startswith("http://") and "web.archive.org" not in url:
        problems.append("uses http://")
    if url and not has_archive and "web.archive.org" not in url:
        problems.append("no archive-url")
    domain = _domain(url)
    if any(d in domain for d in SELF_PUBLISHED):
        problems.append(f"possibly self-published source ({domain})")
    return Reference(index, heading, number, tname, url, title, has_archive, access_date,
                     str(tag), problems)


def _reference_findings(refs: list[Reference]) -> list[Finding]:
    findings = []
    serious = ("bare URL", "free-text", "author field", "missing title", "self-published")
    for r in refs:
        important = [p for p in r.problems if p.startswith(serious) or "self-published" in p]
        if important:
            sev = "medium" if any("self-published" in p or "bare URL" in p for p in important) else "low"
            findings.append(Finding(
                "citation", sev, _sentence("; ".join(important)), r.section,
                snippet=r.title or r.url, url=r.url, section_no=r.section_no))

    by_url: dict[str, list[Reference]] = {}
    for r in refs:
        if r.url:
            by_url.setdefault(r.url, []).append(r)
    for url, dupes in by_url.items():
        if len(dupes) > 1:
            findings.append(Finding(
                "citation", "low", f"Same source cited {len(dupes)} times; use a named ref",
                snippet=dupes[0].title or url, url=url))

    no_archive = [r for r in refs if "no archive-url" in r.problems]
    if no_archive:
        findings.append(Finding(
            "citation", "low", f"{len(no_archive)} of {len(refs)} references have no archived copy",
            detail="Look up Wayback Machine snapshots below"))
    http = [r for r in refs if "uses http://" in r.problems]
    if http:
        findings.append(Finding(
            "citation", "low", f"{len(http)} references use http:// links",
            detail="Check whether https:// works for each"))
    return findings


def _check_paragraphs(body: Wikicode, heading: str, findings: list[Finding]) -> None:
    if not heading:
        return  # the lead summarises cited body text (WP:LEADCITE)
    text = str(body)
    text = re.sub(r"^=+[^=\n]+=+\s*$", "", text, flags=re.M)
    for para in re.split(r"\n\s*\n", text):
        stripped = para.strip()
        if not stripped or stripped[0] in "*#:;{|[" and not stripped.startswith("[["):
            continue  # lists, tables, templates, images
        if stripped.startswith("[[File:") or stripped.startswith("[[Image:"):
            continue
        plain = _plain(stripped, limit=10**6)
        has_cn = re.search(r"\{\{\s*(citation needed|cn|fact)\b", stripped, re.I)
        if "<ref" not in stripped:
            if len(plain) >= MIN_UNCITED_CHARS and not has_cn:
                findings.append(Finding("uncited", "medium", "Paragraph has no citations",
                                        heading, snippet=_plain(stripped, 160)))
            continue
        tail = re.split(r"<ref[^>]*/>|</ref>", stripped)[-1]
        tail_plain = _plain(tail, limit=10**6)
        if len(tail_plain) >= MIN_TRAILING_CHARS and not has_cn:
            findings.append(Finding("uncited", "low", "Text after the last citation in a paragraph",
                                    heading, snippet="…" + tail_plain[-160:]))


def _structure_findings(code: Wikicode, sections: list[tuple[str, int, Wikicode]]) -> list[Finding]:
    findings = []
    names = {_name(t) for t in code.filter_templates(recursive=False)}
    tags = {str(t.tag).lower() for t in code.filter_tags(recursive=True)}
    if "short description" not in names:
        findings.append(Finding("structure", "medium", "No short description",
                                detail="Add {{Short description|…}} at the top"))
    if not ({"reflist", "references", "refs"} & names) and "references" not in tags:
        findings.append(Finding("structure", "high", "No {{Reflist}} or <references />"))
    lead = sections[0][2] if sections and not sections[0][0] else None
    if lead is not None:
        lead_chars = len(_plain(lead, limit=10**6))
        if lead_chars < 250:
            findings.append(Finding("structure", "low", "Lead is very short",
                                    detail=f"{lead_chars} characters"))
    for h, number, body in sections:
        if h and len(_plain(re.sub(r"^=+.*=+$", "", str(body), flags=re.M), 10**6)) == 0 \
                and not body.filter_templates():
            findings.append(Finding("structure", "low", "Empty section", h, section_no=number))
    return findings


# -- talk page -------------------------------------------------------------


def analyze_talk(wikitext: str) -> list[TalkThread]:
    code = mwp.parse(wikitext)
    threads = []
    for sec in code.get_sections(levels=[2]):
        heading = sec.filter_headings(recursive=False)[0].title.strip_code().strip()
        text = html.unescape(str(sec))
        stamps = [_parse_ts(t, d) for t, d in SIGNATURE_TS.findall(text)]
        stamps = [s for s in stamps if s]
        names = {_name(t) for t in sec.filter_templates(recursive=True)}
        participants = list(dict.fromkeys(u.strip() for u in USER_LINK.findall(text)))
        body = re.sub(r"^==.*==\s*$", "", text, count=1, flags=re.M)
        body = SIGNATURE_TS.sub("", body)
        threads.append(TalkThread(
            heading=heading,
            comments=len(stamps),
            participants=participants,
            first_comment=min(stamps) if stamps else None,
            last_comment=max(stamps) if stamps else None,
            resolved=bool(names & RESOLVED_TEMPLATES),
            edit_request=bool(names & EDIT_REQUEST_TEMPLATES),
            urls=list(dict.fromkeys(URL.findall(text))),
            snippet=_plain(body, 240),
        ))
    threads.sort(key=lambda t: t.last_comment or datetime.min, reverse=True)
    return threads


def wayback_timestamp(date: str) -> str:
    """Turn a citation date such as 2014-09-09 or 9 September 2014 into YYYYMMDD."""
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(date.strip(), fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return ""


def _parse_ts(time: str, date: str) -> datetime | None:
    try:
        return datetime.strptime(f"{date} {time}", "%d %B %Y %H:%M")
    except ValueError:
        return None


def talk_findings(threads: list[TalkThread], article: list[Finding] = ()) -> list[Finding]:
    """Open talk threads that point at concrete work.

    A thread offering sources is matched to unsourced-statement tags in any
    section it names, since that's usually what the source is for.
    """
    unsourced = {f.section: f.section_no for f in article
                 if f.message == "Unsourced statement" and f.section}
    out = []
    for t in threads:
        if t.resolved:
            continue
        when = t.last_comment.strftime("%Y-%m-%d") if t.last_comment else "undated"
        if t.edit_request:
            out.append(Finding("talk", "high", f"Open edit request: {t.heading}", detail=when,
                               snippet=t.snippet))
        elif t.urls:
            detail = (f"{when} · {len(t.participants)} participant(s), "
                      f"{'no reply yet' if len(t.participants) <= 1 else 'discussed'}")
            text = f"{t.heading} {t.snippet}".lower()
            matches = sorted(s for s in unsourced if s.lower() in text)
            if matches:
                detail += " · may answer the unsourced statement in " + ", ".join(matches)
            out.append(Finding("talk", "high" if len(t.participants) <= 1 else "medium",
                               f"Source suggested on talk: {t.heading}",
                               detail=detail, snippet=t.snippet, url=t.urls[0],
                               section=matches[0] if matches else "",
                               section_no=unsourced[matches[0]] if matches else None))
        elif len(t.participants) <= 1 and t.comments:
            out.append(Finding("talk", "medium", f"Unanswered thread: {t.heading}", detail=when,
                               snippet=t.snippet))
    return out
