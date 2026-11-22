"""Web search + page fetching, provider-agnostic.

Search providers (first available wins, the rest are fallbacks):
  Serper (SERPER_API_KEY) → Tavily (TAVILY_API_KEY) → DuckDuckGo Lite (no key).
"""
from __future__ import annotations

import html
import logging
import random
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from .http import Http, ProviderError
from .settings import Settings

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    date: str = ""
    provider: str = ""


class SearchBudgetExceeded(ProviderError):
    def __init__(self):
        super().__init__("search", "budget", "search budget for this tick exhausted")


class Search:
    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings
        self.used = 0
        self.cache: dict[tuple, list[SearchResult]] = {}
        self._ddg_last = 0.0

    @property
    def providers(self) -> list[str]:
        out = []
        if self.s.keys.serper:
            out.append("serper")
        if self.s.keys.tavily:
            out.append("tavily")
        out.append("duckduckgo")
        return out

    def search(self, query: str, num: int = 8, news: bool = False) -> list[SearchResult]:
        key = (query, num, news)
        if key in self.cache:
            return self.cache[key]
        if self.used >= self.s.search_budget_per_tick:
            raise SearchBudgetExceeded()
        last: ProviderError | None = None
        for p in self.providers:
            if not self.http.available(p):
                continue
            try:
                self.used += 1
                res = getattr(self, f"_{p}")(query, num, news)
                self.cache[key] = res
                return res
            except ProviderError as e:
                last = e
                log.info("search via %s failed (%s); trying next provider", p, e.category)
        if last:
            raise last
        return []

    def _serper(self, q: str, num: int, news: bool) -> list[SearchResult]:
        data = self.http.request("POST", f"https://google.serper.dev/{'news' if news else 'search'}",
                                 provider="serper", json={"q": q, "num": min(num, 10)},
                                 headers={"X-API-KEY": self.s.keys.serper, "Content-Type": "application/json"})
        items = data.get("news" if news else "organic") or []
        return [SearchResult(title=i.get("title", ""), url=i.get("link", ""), snippet=i.get("snippet", ""),
                             date=i.get("date", ""), provider="serper") for i in items if i.get("link")]

    def _tavily(self, q: str, num: int, news: bool) -> list[SearchResult]:
        body = {"query": q, "max_results": min(num, 10), "topic": "news" if news else "general"}
        data = self.http.request("POST", "https://api.tavily.com/search", provider="tavily", json=body,
                                 headers={"Authorization": f"Bearer {self.s.keys.tavily}"})
        return [SearchResult(title=i.get("title", ""), url=i.get("url", ""), snippet=(i.get("content") or "")[:500],
                             date=i.get("published_date", ""), provider="tavily")
                for i in data.get("results", []) if i.get("url")]

    DDG_MIN_INTERVAL_S = 4.0

    def _duckduckgo(self, q: str, num: int, news: bool) -> list[SearchResult]:
        # Free fallback: be polite (paced queries) and stop for the run as soon as DDG shows its bot challenge.
        wait = self._ddg_last + self.DDG_MIN_INTERVAL_S + random.uniform(0, 2) - time.time()
        if wait > 0:
            time.sleep(wait)
        resp = self.http.request("POST", "https://lite.duckduckgo.com/lite/", provider="duckduckgo",
                                 data={"q": q, "kl": "wt-wt"}, expect_json=False, timeout=(6, 15))
        self._ddg_last = time.time()
        if "anomaly" in resp.text[:20000].lower() and "result-link" not in resp.text:
            self.http.trip("duckduckgo", "bot challenge served — paused for this run (add SERPER_API_KEY for reliable search)")
            raise ProviderError("duckduckgo", "rate_limit", "DuckDuckGo bot challenge")
        return parse_ddg_lite(resp.text)[:num]


_DDG_LINK = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", re.S)
_DDG_LINK2 = re.compile(r"<a[^>]+class=['\"]result-link['\"][^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S)
_DDG_SNIP = re.compile(r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>", re.S)


def _strip_tags(s: str) -> str:
    return html.unescape(html.unescape(re.sub(r"<[^>]+>", "", s))).strip()  # DDG double-encodes


def _ddg_target(href: str) -> str:
    if "uddg=" in href:
        return unquote(parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query).get("uddg", [""])[0])
    return href if href.startswith("http") else ""


def parse_ddg_lite(page: str) -> list[SearchResult]:
    links = _DDG_LINK.findall(page) or _DDG_LINK2.findall(page)
    snippets = _DDG_SNIP.findall(page)
    out = []
    for i, (href, title) in enumerate(links):
        url = _ddg_target(href)
        if not url or "duckduckgo.com/y.js" in url:
            continue
        out.append(SearchResult(title=_strip_tags(title), url=url,
                                snippet=_strip_tags(snippets[i]) if i < len(snippets) else "", provider="duckduckgo"))
    return out


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "form", "header", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "section", "article", "tr"):
            self.parts.append("\n")
        if tag == "meta":
            a = dict(attrs)
            if a.get("name", "").lower() in ("description", "og:description") or a.get("property") == "og:description":
                self.parts.append("\n" + (a.get("content") or "") + "\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(page: str, limit: int = 12000) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(page)
    except Exception:  # malformed HTML: keep what we parsed
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return p.title.strip(), text[:limit]


def fetch_text(http: Http, url: str, limit: int = 12000) -> tuple[str, str]:
    """(title, visible text). Raises ProviderError on failure."""
    resp = http.request("GET", url, provider=f"fetch:{urlparse(url).netloc}", expect_json=False,
                        timeout=(6, 15), allow_redirects=True)
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        raise ProviderError("fetch", "parse", f"unsupported content-type {ctype}")
    return html_to_text(resp.text, limit)
