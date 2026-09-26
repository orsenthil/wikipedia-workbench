from datetime import date

import respx
from fastapi.testclient import TestClient

from editor.app import app
from editor.citations import add_archive, blank, cs1_date, suggest_ref_name, to_cs1

TODAY = date(2026, 9, 25)


def test_webpage_moves_site_name_out_of_title():
    item = {"itemType": "webpage", "url": "https://www.linuxjournal.com/article/6530",
            "title": "Introducing the 2.6 Kernel | Linux Journal",
            "websiteTitle": "www.linuxjournal.com"}
    assert to_cs1(item, today=TODAY) == (
        "<ref>{{cite web |title=Introducing the 2.6 Kernel |website=Linux Journal "
        "|url=https://www.linuxjournal.com/article/6530 |access-date=2026-09-25}}</ref>")


def test_book_with_pages_and_name():
    item = {"itemType": "book", "ISBN": ["978-0-672-32720-9"], "title": "Linux kernel development",
            "edition": "2nd ed", "publisher": "Novell Press", "date": "2005",
            "author": [["Robert", "Love"]], "url": "https://catalog.example/123"}
    out = to_cs1(item, pages="80-85", ref_name=suggest_ref_name(item), today=TODAY)
    assert out.startswith('<ref name="Love2005">{{cite book |last=Love |first=Robert ')
    assert "|edition=2nd |" in out and "|pages=80–85 |" in out
    assert "url=" not in out  # catalogue URL dropped when there's an ISBN


def test_journal_multiple_authors_and_month_date():
    item = {"itemType": "journalArticle", "title": "T", "publicationTitle": "J", "date": "2004-11",
            "DOI": "10.1/x", "author": [["A", "One"], ["B", "Two"]]}
    out = to_cs1(item, today=TODAY)
    assert "|last1=One |first1=A |last2=Two |first2=B |" in out
    assert "|date=November 2004 |" in out and "|doi=10.1/x" in out


def test_pipes_escaped_and_archive_added():
    item = {"itemType": "webpage", "title": "A | B | C", "url": "http://x.org"}
    out = to_cs1(item, archive_url="https://web.archive.org/web/20140910195617/http://x.org",
                 today=TODAY)
    assert "|title=A {{!}} B {{!}} C |" in out
    assert "|archive-date=2014-09-10 |url-status=live}}" in out


def test_blank_skeleton():
    out = blank("cite thesis", url="https://u/t.pdf", pages="49 - 52", ref_name="M2011", today=TODAY)
    assert out == ('<ref name="M2011">{{cite thesis |last= |first= |title= |degree= |publisher= '
                   '|date= |url=https://u/t.pdf |access-date=2026-09-25 |pages=49–52}}</ref>')


def test_add_archive_keeps_existing_formatting():
    ref = "<ref>{{cite web\n|url = http://x\n|title = T\n}}</ref>"
    out = add_archive(ref, "https://web.archive.org/web/20140910195617/http://x")
    assert out.startswith("<ref>{{cite web\n|url = http://x\n|title = T\n|archive-url = ")
    assert out.endswith("|url-status = live\n}}</ref>")
    assert add_archive("<ref>plain text</ref>", "https://web.archive.org/web/2014/x") == "<ref>plain text</ref>"


def test_cs1_date():
    assert cs1_date("2004-11") == "November 2004"
    assert cs1_date("2004-11-05") == "2004-11-05"


@respx.mock
def test_cite_page_falls_back_to_skeleton_when_citoid_fails():
    respx.get(url__regex=r".*/api/rest_v1/data/citation/.*").respond(415, json={"error": "pdf"})
    with TestClient(app) as client:
        resp = client.get("/cite", params={"source": "https://u.edu/phd-thesis.pdf", "pages": "49-52"})
    assert resp.status_code == 200
    assert "{{cite thesis |last= |first=" in resp.text
    assert "couldn&#39;t read this source" in resp.text
