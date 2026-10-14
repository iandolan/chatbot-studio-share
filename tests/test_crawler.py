"""Crawler extraction: chrome is stripped from text but still yields links."""
from server.crawler import _extract

PAGE = """
<html>
  <head><title>Acme Widgets</title></head>
  <body>
    <header><a href="/">Home</a></header>
    <nav>
      <a href="/pricing">Pricing</a>
      <a href="/about">About</a>
    </nav>
    <main>
      <p>We sell widgets to people who need widgets.</p>
      <a href="/contact">Talk to us</a>
    </main>
    <footer><a href="/privacy">Privacy</a></footer>
  </body>
</html>
"""


def _links(html, url="https://acme.test/"):
    return _extract(html, url)[2]


def test_nav_header_and_footer_links_are_still_discovered():
    """The regression that left every bot training on 2 pages of a 109-page site.

    Chrome is stripped from the indexed text, which is correct, but it used to be
    stripped before links were read, so the crawler could only follow in-body
    links and never found the rest of the site.
    """
    links = _links(PAGE)
    assert "https://acme.test/pricing" in links
    assert "https://acme.test/about" in links
    assert "https://acme.test/privacy" in links
    assert "https://acme.test/contact" in links


def test_chrome_text_is_still_excluded_from_the_indexed_body():
    title, text, _ = _extract(PAGE, "https://acme.test/")
    assert title == "Acme Widgets"
    assert "We sell widgets" in text
    # Nav/footer wording repeats on every page and would swamp retrieval.
    assert "Pricing" not in text
    assert "Privacy" not in text


def test_relative_links_resolve_against_the_page_and_fragments_are_dropped():
    html = '<html><body><nav><a href="docs/start#intro">Start</a></nav></body></html>'
    assert _links(html, "https://acme.test/guide/") == ["https://acme.test/guide/docs/start"]
