"""Small same-domain crawler for the Training tab, with SSRF protection."""
import ipaddress
import socket
import time
import urllib3
import certifi
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import Config

USER_AGENT = "ChatbotStudioBot/1.0 (+training crawler)"
MAX_PAGES_HARD = 15
MAX_TEXT_PER_PAGE = 60_000
_TIMEOUT = (5, 15)

_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form",
               "nav", "footer", "header", "aside")


class CrawlError(Exception):
    pass


def _resolve(hostname):
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(hostname, None)))
    except socket.gaierror as exc:
        raise CrawlError("The website address could not be resolved") from exc
    trusted = Config.CRAWLER_ALLOW_PRIVATE or hostname.lower() in Config.CRAWLER_PRIVATE_ALLOWLIST
    if not addresses or (not trusted and any(not ipaddress.ip_address(a).is_global for a in addresses)):
        raise CrawlError("That host resolves to a private/internal address and cannot be crawled")
    return addresses


def _host_is_public(hostname):
    try:
        _resolve(hostname)
        return True
    except CrawlError:
        return False


def _check_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CrawlError("Enter a complete http or https website address")
    if parsed.username or parsed.password:
        raise CrawlError("Website addresses cannot contain credentials")
    try:
        if parsed.port not in (None, 80, 443):
            raise CrawlError("Only standard website ports are supported")
    except ValueError as exc:
        raise CrawlError("Invalid website port") from exc
    _resolve(parsed.hostname)
    return parsed


def _same_site(hostname, origin_host):
    def canonical(host):
        host = (host or "").lower()
        return host[4:] if host.startswith("www.") else host
    return canonical(hostname) == canonical(origin_host)


def _fetch_http(url, origin_host):
    """Pin each connection to a validated address, retaining TLS hostname checks."""
    for _ in range(6):
        parsed = _check_url(url)
        if not _same_site(parsed.hostname, origin_host):
            raise CrawlError("Redirects must stay on the original website")
        address = _resolve(parsed.hostname)[0]
        kwargs = {"host": address, "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                  "timeout": urllib3.Timeout(connect=5, read=15), "maxsize": 1}
        if parsed.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(**kwargs, server_hostname=parsed.hostname,
                assert_hostname=parsed.hostname, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where())
        else:
            pool = urllib3.HTTPConnectionPool(**kwargs)
        response = None
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            response = pool.urlopen("GET", path, headers={"Host": parsed.netloc, "User-Agent": USER_AGENT},
                                    redirect=False, retries=False, preload_content=False)
            if response.status in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get("Location", ""))
                if not response.headers.get("Location"):
                    raise CrawlError("Redirect has no destination")
                if parsed.scheme == "https" and urlparse(target).scheme != "https":
                    raise CrawlError("HTTPS redirects must stay on HTTPS")
                url = target
                continue
            if response.status >= 400:
                raise CrawlError(f"Website returned HTTP {response.status}")
            if "html" not in response.headers.get("Content-Type", "").lower():
                raise CrawlError("The website did not return an HTML page")
            chunks, size = [], 0
            started = time.monotonic()
            for chunk in response.stream(65536, decode_content=True):
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() - started > 30:
                    raise CrawlError("Page exceeded the download limit")
                chunks.append(chunk)
            return b"".join(chunks), url
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise CrawlError("Too many website redirects")


def _extract(html, url):
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else url

    # Harvest links BEFORE stripping chrome. _STRIP_TAGS removes nav, header and
    # footer, which is right for the text (that boilerplate repeats on every page
    # and pollutes retrieval) but fatal for discovery: on a normally-built site
    # the nav IS the site map, so stripping first left the crawler able to follow
    # only in-body links. A 15-page crawl of a 109-page site returned 2 pages.
    links = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a["href"]))[0]
        links.append(href)

    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    body = soup.body or soup
    text = body.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return title, text[:MAX_TEXT_PER_PAGE], links


def crawl(start_url, max_pages=5):
    """BFS crawl within the start URL's host. Returns
    {title, content, pages: [urls]}. Raises CrawlError on total failure."""
    max_pages = max(1, min(int(max_pages or 1), MAX_PAGES_HARD))
    parsed = _check_url(start_url)
    origin_host = parsed.hostname

    queue = [start_url]
    seen = set()
    pages = []
    sections = []
    site_title = ""
    errors = []

    started = time.monotonic()
    while queue and len(pages) < max_pages and len(seen) < 60 and time.monotonic() - started < 120:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            if not _same_site(urlparse(url).hostname, origin_host):
                continue
            html, final_url = _fetch_http(url, origin_host)
            title, text, links = _extract(html, final_url)
            seen.add(final_url)
        except (CrawlError, urllib3.exceptions.HTTPError, OSError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        if not site_title:
            site_title = title
        if text:
            pages.append(final_url)
            sections.append(f"## {title}\nURL: {final_url}\n\n{text}")
        for link in links:
            if link not in seen and _same_site(urlparse(link).hostname, origin_host):
                queue.append(link)

    if not pages:
        detail = f" ({errors[0]})" if errors else ""
        raise CrawlError(f"Couldn't fetch any readable pages from {start_url}{detail}")

    return {
        "title": site_title or origin_host,
        "content": "\n\n\n".join(sections),
        "pages": pages,
    }
