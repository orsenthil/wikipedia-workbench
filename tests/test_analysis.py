from editor.analysis import analyze_article, analyze_talk, talk_findings, wayback_timestamp

ARTICLE = """{{Short description|Test}}
'''Thing''' is a thing that is described in this lead paragraph, which is long enough to not be flagged as a short lead by the checks in the analysis module. It goes on for a while to reach the length.

== History ==
The thing was invented in 1990.<ref>{{cite web |url=http://example.com/a |title=A |access-date=2014-09-09}}</ref> It was later improved by many people over several decades, which made it considerably more popular across the whole industry and in academic research too.

The second paragraph has no references at all, and it is long enough that the checker should notice it and suggest adding a citation to support the claims it makes here, since readers deserve to be able to verify them.

== Issues ==
It is slow.{{citation needed|date=July 2016}}<ref>https://github.com/x/y</ref>
<ref>{{cite web |url=http://example.com/a |title=A again |last=<a@b.c> |access-date=2014-09-09}}</ref>

== References ==
{{Reflist}}
"""

TALK = """{{WikiProject banner shell|class=Start|}}

== Citation for Issues ==
My [https://example.org/thesis.pdf thesis] covers this. [[User talk:Someone|talk]] 20:54, 5 August 2026 (UTC)

== Old question ==
{{resolved}}
Why? [[User:A|A]] 10:00, 1 January 2010 (UTC)
:Because. [[User:B|B]] 11:00, 2 January 2010 (UTC)
"""


def messages(report):
    return [(f.message, f.section) for f in report.findings]


def test_article_findings():
    r = analyze_article(ARTICLE)
    m = messages(r)
    assert ("Unsourced statement", "Issues") in m
    assert ("Paragraph has no citations", "History") in m
    assert ("Text after the last citation in a paragraph", "History") in m
    assert any(msg.startswith("Bare URL") and s == "Issues" for msg, s in m)
    assert any("self-published source (github.com)" in msg for msg, _ in m)
    assert any("Author field contains an email" in msg for msg, _ in m)
    assert any(msg == "Same source cited 2 times; use a named ref" for msg, _ in m)
    assert not any(msg == "No short description" for msg, _ in m)
    assert r.stats["references"] == 3


def test_structure_findings():
    m = messages(analyze_article("Short.\n\n== A ==\nText."))
    assert ("No short description", "") in m
    assert ("No {{Reflist}} or <references />", "") in m
    assert ("Lead is very short", "") in m


def test_talk_threads_and_cross_reference():
    threads = analyze_talk(TALK)
    assert [t.heading for t in threads] == ["Citation for Issues", "Old question"]
    assert threads[0].urls == ["https://example.org/thesis.pdf"]
    assert threads[1].resolved and threads[1].comments == 2

    found = talk_findings(threads, analyze_article(ARTICLE).findings)
    assert len(found) == 1  # the resolved thread is skipped
    assert found[0].severity == "high"
    assert "unsourced statement in Issues" in found[0].detail


def test_wayback_timestamp():
    assert wayback_timestamp("2014-09-09") == "20140909"
    assert wayback_timestamp("9 September 2014") == "20140909"
    assert wayback_timestamp("August 1, 2009") == "20090801"
    assert wayback_timestamp("sometime") == ""
