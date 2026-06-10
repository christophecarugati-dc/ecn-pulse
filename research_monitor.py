"""
Research Monitor — academic papers and regulatory publications for
digital competition policy monitoring.

Sources:
  arxiv      Recent academic papers on digital competition, platform economics,
             AI regulation (via arXiv Atom API — no auth required)
  eurlex     Recent EU competition-law and DMA/DSA regulatory documents
             (via EUR-Lex recently-added Atom feed, filtered by keyword)
  cjeu       Recent CJEU/General Court competition judgments
             (via EUR-Lex judgment search)

Output: data/research_items.json

Usage:
  python research_monitor.py
  python research_monitor.py --lookback 14 --verbose
  python research_monitor.py --only arxiv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "ECN-Pulse/0.3 (+https://github.com/christophecarugati-dc/ecn-pulse; "
    "contact: christophe.carugati@digital-competition.com)"
)
REQUEST_TIMEOUT = 25
RATE_LIMIT_SECONDS = 1.5
DEFAULT_LOOKBACK_DAYS = 7

log = logging.getLogger("research-monitor")

DIGITAL_COMPETITION_KEYWORDS = [
    "antitrust", "competition", "digital market", "platform", "gatekeeper",
    "DMA", "DSA", "market power", "dominant", "abuse", "merger", "cartel",
    "big tech", "algorithmic", "interoperability", "self-preferencing",
    "tying", "foreclosure", "network effect", "AI Act", "artificial intelligence",
    "data economy", "platform economy", "GDPR", "data protection", "tech giant",
    "online platform", "search engine", "app store", "cloud computing",
    "marketplace", "price algorithm", "recommendation system",
    "acquisition", "concentration", "notification", "DMA Article 14",
    "gatekeeper acquisition", "Article 14",
]

# ── arXiv search configuration ────────────────────────────────────────────────
# Two complementary queries:
#
#  QUERY_COMPETITION  — economics/policy papers that explicitly address
#                       digital competition, platforms, antitrust.
#
#  QUERY_MARKET_TECH  — technology papers (cs.AI, cs.IR …) about AI/ML
#                       systems that will reshape digital markets even when
#                       they don't use the word "competition":
#                       • AI agents displacing traditional search
#                       • LLMs entering advertising, content, e-commerce
#                       • Recommendation algorithms driving platform lock-in
#                       • Foundation models as new market infrastructure

# Fetch ALL recent econ.IO / econ.GN / econ.TH papers — these categories are
# Industrial Organization and General Economics, so every paper is potentially
# relevant. A title-only filter discards too many relevant submissions.
ARXIV_QUERY_COMPETITION = "cat:econ.IO OR cat:econ.GN OR cat:econ.TH"

ARXIV_QUERY_MARKET_TECH = (
    "(cat:cs.AI OR cat:cs.IR OR cat:cs.CY OR cat:cs.NI OR cat:cs.LG OR cat:cs.GT) AND ("
    # AI agents & autonomous systems reshaping markets
    'abs:"AI agent" OR abs:"autonomous agent" OR abs:"agentic" OR '
    'abs:"AI assistant" OR abs:"virtual assistant" OR '
    # LLMs / foundation models entering markets
    'abs:"large language model" OR abs:LLM OR abs:"foundation model" OR '
    'abs:"generative AI" OR abs:"ChatGPT" OR abs:"GPT" OR '
    # Search & information retrieval disruption
    'abs:"web search" OR abs:"search engine" OR abs:"information retrieval" OR '
    'abs:"retrieval-augmented" OR abs:RAG OR '
    # Advertising & content monetisation
    'abs:"online advertising" OR abs:"digital advertising" OR '
    'abs:"ad auction" OR abs:"content recommendation" OR '
    # Recommendation & ranking systems (platform lock-in)
    'abs:"recommendation system" OR abs:"recommender system" OR '
    'abs:"ranking algorithm" OR abs:"personalization" OR '
    # Cloud & infrastructure markets
    'abs:"cloud computing" OR abs:"API economy" OR '
    # Data and AI market structure
    'abs:"data market" OR abs:"AI market" OR '
    'abs:"platform market" OR abs:"digital market" OR abs:"market power"'
    ")"
)


@dataclass
class ResearchItem:
    source: str          # arxiv | eurlex | cjeu
    item_type: str       # academic | regulation | decision | judgment | opinion
    item_id: str
    title: str
    url: str
    date: str            # YYYY-MM-DD (empty string if unknown)
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    categories: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat()


_last_request: float = 0.0


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    global _last_request
    wait = RATE_LIMIT_SECONDS - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", USER_AGENT)
    return session.get(url, **kwargs)


def _parse_date(text: str) -> str:
    """Parse a date string to YYYY-MM-DD; return empty string on failure."""
    if not text:
        return ""
    try:
        dt = dateparser.parse(text.strip(), ignoretz=True)
        return dt.strftime("%Y-%m-%d") if dt else ""
    except Exception:
        return ""


def _is_recent(date_str: str, days: int) -> bool:
    if not date_str:
        return True  # include items with unknown dates
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return True


def _has_competition_keyword(text: str) -> bool:
    tl = text.lower()
    return any(kw.lower() in tl for kw in DIGITAL_COMPETITION_KEYWORDS)


# ── arXiv ─────────────────────────────────────────────────────────────────────

def _arxiv_query(
    session: requests.Session,
    query: str,
    max_results: int,
    lookback_days: int,
    seen: set[str],
    label: str,
) -> list[ResearchItem]:
    """Run a single arXiv API query and return ResearchItems not in seen."""
    try:
        resp = _get(
            session,
            "https://export.arxiv.org/api/query",
            params={
                "search_query": query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("arXiv %s query failed: %s", label, exc)
        return []

    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        log.warning("arXiv %s XML parse error: %s", label, exc)
        return []

    items: list[ResearchItem] = []

    for entry in root.findall("a:entry", ns):
        try:
            id_el = entry.find("a:id", ns)
            if id_el is None:
                continue
            full_url = (id_el.text or "").strip()
            arxiv_id = full_url.split("/abs/")[-1] if "/abs/" in full_url else full_url
            if arxiv_id in seen:
                continue
            seen.add(arxiv_id)

            title_el = entry.find("a:title", ns)
            title = " ".join((title_el.text or "").split()) if title_el is not None else ""

            summary_el = entry.find("a:summary", ns)
            abstract = " ".join((summary_el.text or "").split()) if summary_el is not None else ""

            published_el = entry.find("a:published", ns)
            date = _parse_date(published_el.text if published_el is not None else "")

            if not _is_recent(date, lookback_days):
                continue

            authors = [
                (a.find("a:name", ns).text or "").strip()
                for a in entry.findall("a:author", ns)
                if a.find("a:name", ns) is not None
            ]
            categories = [
                c.get("term", "")
                for c in entry.findall("a:category", ns)
            ]

            items.append(ResearchItem(
                source="arxiv",
                item_type="academic",
                item_id=arxiv_id,
                title=title,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                date=date,
                authors=authors,
                abstract=abstract[:1200],
                categories=categories,
            ))
        except Exception as exc:
            log.debug("arXiv entry skipped: %s", exc)

    log.info("arXiv %s: %d items", label, len(items))
    return items


def fetch_arxiv(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """Run two complementary arXiv queries and merge results.

    Query 1 — competition economics: papers in econ.IO/econ.GN that explicitly
               address digital markets, platforms, antitrust.
    Query 2 — market-disrupting technology: papers in cs.AI/cs.IR/cs.CY about
               AI agents, LLMs, search, recommendation, advertising.  These
               papers signal how markets will evolve even when they do not use
               the word "antitrust".
    """
    seen: set[str] = set()
    items: list[ResearchItem] = []
    # arXiv papers are processed with delays; use at least 14 days so we don't
    # miss submissions that appeared in the index after our regular lookback.
    arxiv_lookback = max(lookback_days, 14)

    log.info("arXiv query 1 — competition economics (lookback %d days)", arxiv_lookback)
    items += _arxiv_query(
        session, ARXIV_QUERY_COMPETITION,
        max_results=60, lookback_days=arxiv_lookback,
        seen=seen, label="competition-economics",
    )

    log.info("arXiv query 2 — AI/tech market disruption (lookback %d days)", arxiv_lookback)
    items += _arxiv_query(
        session, ARXIV_QUERY_MARKET_TECH,
        max_results=100, lookback_days=arxiv_lookback,
        seen=seen, label="market-tech",
    )

    log.info("arXiv total: %d items (lookback %d days)", len(items), arxiv_lookback)
    return items


# ── EUR-Lex ───────────────────────────────────────────────────────────────────

# EUR-Lex feeds: try each base URL; the tools/rss.do endpoint returns 404 on
# the current site — fall back to the OJ daily Atom feed (CELEX identifier feed)
# and the ELI / official-journal RSS if available.
EURLEX_FEEDS: list[tuple[str, dict]] = [
    # OJ C-series recent additions (competition notices, DMA, DSA)
    ("https://eur-lex.europa.eu/oj/daily-view/C-series/default.html?ojDate=",
     {}),  # HTML only – handled via _scrape_eurlex_search; kept for documentation
    # Fallback RSS candidates (site has been restructuring these URLs)
    ("https://eur-lex.europa.eu/tools/rss.do",
     {"type": "recently-added", "facet_lang": "EN"}),
    ("https://eur-lex.europa.eu/tools/rss.do",
     {"type": "latest-oj", "facet_lang": "EN"}),
    # Alternative Atom format
    ("https://eur-lex.europa.eu/RSSONE.do",
     {"moreMaxIsAllowed": "false", "where": "", "db": "ALL", "feedFormat": "RSS", "facet_lang": "EN"}),
]

ATOM_NS = "http://www.w3.org/2005/Atom"
RSS_NS = ""  # RSS has no default namespace in its elements


def _parse_atom_or_rss(xml_text: str) -> list[dict]:
    """Parse Atom or RSS feed XML into list of {title, url, date, summary} dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[dict] = []
    tag = root.tag.lower()

    if "feed" in tag or root.tag == f"{{{ATOM_NS}}}feed":
        # Atom
        ns = {"a": ATOM_NS}
        for entry in root.findall("a:entry", ns):
            title_el = entry.find("a:title", ns)
            link_el = entry.find("a:link", ns)
            date_el = entry.find("a:updated", ns) or entry.find("a:published", ns)
            summary_el = entry.find("a:summary", ns) or entry.find("a:content", ns)
            entries.append({
                "title": (title_el.text or "") if title_el is not None else "",
                "url": (link_el.get("href") or link_el.text or "") if link_el is not None else "",
                "date": _parse_date((date_el.text or "") if date_el is not None else ""),
                "summary": (summary_el.text or "") if summary_el is not None else "",
            })
    elif "rss" in tag or root.tag == "rss":
        # RSS
        channel = root.find("channel")
        if channel is None:
            return []
        for item in channel.findall("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            date_el = item.find("pubDate") or item.find("dc:date", {"dc": "http://purl.org/dc/elements/1.1/"})
            desc_el = item.find("description")
            entries.append({
                "title": (title_el.text or "") if title_el is not None else "",
                "url": (link_el.text or "") if link_el is not None else "",
                "date": _parse_date((date_el.text or "") if date_el is not None else ""),
                "summary": BeautifulSoup((desc_el.text or ""), "html.parser").get_text(" ", strip=True)
                           if desc_el is not None else "",
            })

    return entries


def fetch_eurlex(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """Regulatory documents from EUR-Lex Atom/RSS feeds, filtered by competition keywords."""
    items: list[ResearchItem] = []
    seen: set[str] = set()

    for feed_url, params in EURLEX_FEEDS:
        if not params:  # HTML-only placeholder entry
            continue
        try:
            resp = _get(session, feed_url, params=params,
                        headers={"Accept": "application/rss+xml, application/atom+xml, */*"})
            resp.raise_for_status()
        except Exception as exc:
            log.debug("EUR-Lex feed (%s %s) error: %s", feed_url, params, exc)
            continue

        entries = _parse_atom_or_rss(resp.text)
        if not entries:
            log.debug("EUR-Lex feed (%s): 0 entries parsed", feed_url)
            continue
        kept = 0
        for e in entries:
            url = e["url"].strip()
            title = e["title"].strip()
            if not url or not title:
                continue
            if url in seen:
                continue
            seen.add(url)

            combined = f"{title} {e['summary']}"
            if not _has_competition_keyword(combined):
                continue

            date = e["date"]
            if not _is_recent(date, lookback_days):
                continue

            item_type = _classify_eurlex_type(title, url)
            celex = re.search(r'[A-Z]\d{4}[A-Z]\d+', url)
            item_id = celex.group(0) if celex else url
            summary_text = BeautifulSoup(e["summary"], "html.parser").get_text(" ", strip=True)

            items.append(ResearchItem(
                source="eurlex",
                item_type=item_type,
                item_id=item_id,
                title=title,
                url=url,
                date=date,
                abstract=summary_text[:800],
            ))
            kept += 1

        log.info("EUR-Lex feed (%s): %d relevant items", params.get("type", feed_url), kept)

    # Also scrape EUR-Lex search for DMA/DSA/competition decisions (HTML)
    items.extend(_scrape_eurlex_search(session, lookback_days, seen))

    log.info("EUR-Lex total: %d items", len(items))
    return items


def _classify_eurlex_type(title: str, url: str) -> str:
    tl = (title + " " + url).lower()
    if "judgment" in tl or "arrêt" in tl:
        return "judgment"
    if "decision" in tl and "commission" in tl:
        return "commission_decision"
    if "decision" in tl:
        return "decision"
    if "regulation" in tl and "proposal" not in tl:
        return "regulation"
    if "directive" in tl and "proposal" not in tl:
        return "directive"
    if "proposal" in tl or "proposition" in tl:
        return "proposal"
    if "opinion" in tl:
        return "opinion"
    if "communication" in tl or "notice" in tl:
        return "communication"
    if "working document" in tl or "staff working" in tl or "swd" in tl:
        return "working_document"
    if "impact assessment" in tl:
        return "working_document"
    if "report" in tl:
        return "report"
    if "guidance" in tl or "guidelines" in tl:
        return "guidelines"
    return "document"


_EURLEX_SEARCH_QUERIES = [
    "digital markets act gatekeeper",
    "competition digital platform abuse dominant",
    "DMA DSA enforcement",
    "artificial intelligence competition regulation",
]


def _scrape_eurlex_search(
    session: requests.Session, lookback_days: int, seen: set[str]
) -> list[ResearchItem]:
    items: list[ResearchItem] = []

    for query in _EURLEX_SEARCH_QUERIES:
        try:
            resp = _get(
                session,
                "https://eur-lex.europa.eu/search.html",
                params={
                    "scope": "EURLEX",
                    "text": query,
                    "lang": "en",
                    "type": "quick",
                    "sortOne": "DATETIME_SORT",
                    "sortOneOrder": "desc",
                },
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.debug("EUR-Lex search '%s' error: %s", query, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # EUR-Lex search result selectors (may change with site redesigns)
        for result in soup.select(".SearchResult, .searchResult, [class*='result-item']"):
            try:
                link = result.select_one("a[href*='legal-content'], a[href*='eur-lex.europa.eu']")
                if not link:
                    continue
                title = link.get_text(strip=True)
                href = link.get("href", "")
                url = href if href.startswith("http") else urljoin("https://eur-lex.europa.eu", href)

                if url in seen:
                    continue
                seen.add(url)

                date_el = result.select_one("[class*='date'], time")
                date_str = _parse_date(date_el.get_text()) if date_el else ""
                if not _is_recent(date_str, lookback_days):
                    continue

                desc_el = result.select_one("[class*='description'], [class*='snippet']")
                abstract = desc_el.get_text(strip=True)[:600] if desc_el else ""

                celex = re.search(r'[A-Z]\d{4}[A-Z]\d+', url)
                item_id = celex.group(0) if celex else url

                items.append(ResearchItem(
                    source="eurlex",
                    item_type=_classify_eurlex_type(title, url),
                    item_id=item_id,
                    title=title,
                    url=url,
                    date=date_str,
                    abstract=abstract,
                    categories=[query],
                ))
            except Exception as exc:
                log.debug("EUR-Lex search result parse error: %s", exc)

    return items


# ── CJEU ──────────────────────────────────────────────────────────────────────

def fetch_cjeu(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """Recent competition-related judgments from CJEU / General Court via EUR-Lex."""
    items: list[ResearchItem] = []
    seen: set[str] = set()

    competition_queries = [
        "competition abuse dominant digital platform",
        "merger control digital markets",
        "cartel technology platform",
    ]

    for query in competition_queries:
        try:
            resp = _get(
                session,
                "https://eur-lex.europa.eu/search.html",
                params={
                    "scope": "EURLEX",
                    "text": query,
                    "lang": "en",
                    "type": "quick",
                    "facet_subtype": "JUDGMENT",
                    "sortOne": "DATETIME_SORT",
                    "sortOneOrder": "desc",
                },
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.debug("CJEU search '%s' error: %s", query, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        for result in soup.select(".SearchResult, .searchResult, [class*='result-item']"):
            try:
                link = result.select_one("a[href*='legal-content']")
                if not link:
                    continue
                title = link.get_text(strip=True)
                href = link.get("href", "")
                url = href if href.startswith("http") else urljoin("https://eur-lex.europa.eu", href)

                if url in seen:
                    continue
                seen.add(url)

                date_el = result.select_one("[class*='date'], time")
                date_str = _parse_date(date_el.get_text()) if date_el else ""
                if not _is_recent(date_str, lookback_days):
                    continue

                desc_el = result.select_one("[class*='description'], [class*='snippet']")
                abstract = desc_el.get_text(strip=True)[:600] if desc_el else ""

                celex = re.search(r'[A-Z]\d{4}[A-Z]\d+', url)
                item_id = celex.group(0) if celex else url

                items.append(ResearchItem(
                    source="cjeu",
                    item_type="judgment",
                    item_id=item_id,
                    title=title,
                    url=url,
                    date=date_str,
                    abstract=abstract,
                    categories=["Competition Judgment"],
                ))
            except Exception as exc:
                log.debug("CJEU result parse error: %s", exc)

    # Try CJEU press releases (competition-related)
    try:
        resp = _get(
            session,
            "https://curia.europa.eu/jcms/jcms/Jo2_7056/en/",
            headers={"Accept": "text/html"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for row in soup.select("li.list_item, tr, .pressRelease"):
            try:
                link = row.select_one("a")
                if not link:
                    continue
                title = link.get_text(strip=True)
                if not _has_competition_keyword(title):
                    continue
                href = link.get("href", "")
                url = href if href.startswith("http") else urljoin("https://curia.europa.eu", href)
                if url in seen:
                    continue
                seen.add(url)

                text = row.get_text(" ", strip=True)
                date_match = re.search(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}[/\-]\d{2}[/\-]\d{2}', text)
                date_str = _parse_date(date_match.group(0)) if date_match else ""
                if not _is_recent(date_str, lookback_days):
                    continue

                items.append(ResearchItem(
                    source="cjeu",
                    item_type="press_release",
                    item_id=url,
                    title=title,
                    url=url,
                    date=date_str,
                    categories=["CJEU Press Release"],
                ))
            except Exception as exc:
                log.debug("CJEU press release parse error: %s", exc)
    except Exception as exc:
        log.debug("CJEU press releases page error: %s", exc)

    log.info("CJEU: %d items", len(items))
    return items


# ── NBER ──────────────────────────────────────────────────────────────────────

_NBER_RSS_CANDIDATES = [
    "https://www.nber.org/rss/new_working_papers.xml",
    "https://papers.nber.org/rss/new_working_papers.xml",
    "https://www.nber.org/workingpapers.rss",
    "https://www.nber.org/papers.rss",
]


def fetch_nber(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """NBER working papers filtered by competition/digital keywords."""
    xml_text = ""
    for candidate in _NBER_RSS_CANDIDATES:
        try:
            resp = _get(session, candidate, allow_redirects=False)
            if resp.status_code == 200:
                xml_text = resp.text
                break
            if resp.status_code in (301, 302):
                loc = resp.headers.get("Location", "")
                if loc:
                    try:
                        resp2 = _get(session, loc)
                        if resp2.status_code == 200:
                            xml_text = resp2.text
                            break
                    except Exception:
                        pass
        except Exception as exc:
            log.debug("NBER candidate %s failed: %s", candidate, exc)

    if not xml_text:
        log.warning("NBER RSS fetch failed: all candidate URLs exhausted, falling back to OpenAlex")
        return _fetch_openalex(
            session,
            query="digital competition antitrust platform DMA regulation",
            lookback_days=lookback_days,
            source_filter="nber",
            max_results=30,
        )

    entries = _parse_atom_or_rss(xml_text)
    items: list[ResearchItem] = []

    for e in entries:
        title = e["title"].strip()
        url = e["url"].strip()
        date = e["date"]
        summary = e["summary"].strip()

        if not _has_competition_keyword(f"{title} {summary}"):
            continue
        if not _is_recent(date, lookback_days):
            continue

        # Extract numeric NBER paper ID from URL (last numeric segment)
        numeric = re.findall(r'\d+', url.rstrip("/").split("/")[-1])
        item_id = numeric[0] if numeric else url

        items.append(ResearchItem(
            source="nber",
            item_type="working_paper",
            item_id=item_id,
            title=title,
            url=url,
            date=date,
            abstract=summary[:1200],
        ))

    log.info("NBER: %d items", len(items))
    return items


# ── OpenAlex (SSRN + Google Scholar equivalent) ───────────────────────────────

def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """Reconstruct text from OpenAlex inverted index {word: [positions]}."""
    if not inverted_index:
        return ""
    tokens: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in (positions or []):
            tokens.append((pos, word))
    tokens.sort()
    return " ".join(w for _, w in tokens)


def _fetch_openalex(
    session: requests.Session,
    query: str,
    lookback_days: int,
    source_filter: str | None = None,
    max_results: int = 50,
) -> list[ResearchItem]:
    """Fetch competition papers from OpenAlex API (free, 10 req/s, no key needed).

    source_filter: "ssrn" | "nber" | None
      - "ssrn"    → only papers hosted on SSRN
      - "nber"    → only papers affiliated with NBER
      - None      → all scholarly papers, skipping arXiv and SSRN (already covered)
    """
    from_date = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    try:
        resp = _get(
            session,
            "https://api.openalex.org/works",
            params={
                "search": query,
                "filter": f"from_publication_date:{from_date}",
                "sort": "publication_date:desc",
                "per-page": str(min(max_results, 200)),
                "select": (
                    "id,title,abstract_inverted_index,publication_date,"
                    "authorships,primary_location,locations,doi"
                ),
                "mailto": "ecn-pulse@digital-competition.com",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        raw_count = len(data.get("results", []))
        log.debug("OpenAlex raw results: %d (source_filter=%s)", raw_count, source_filter)
        items: list[ResearchItem] = []
        for work in data.get("results", []):
            try:
                title = work.get("title") or ""
                abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

                if not _has_competition_keyword(f"{title} {abstract}"):
                    continue

                pub_date = work.get("publication_date") or ""
                if not _is_recent(pub_date, lookback_days):
                    continue

                # Classify location
                primary_loc = work.get("primary_location") or {}
                all_locs = work.get("locations") or [primary_loc]
                primary_src_name = (
                    (primary_loc.get("source") or {}).get("host_organization_name") or ""
                ).lower()

                is_ssrn = "ssrn" in primary_src_name or "social science research network" in primary_src_name
                ssrn_url = primary_loc.get("landing_page_url") or "" if is_ssrn else ""
                if not ssrn_url:
                    for loc in all_locs:
                        lu = (loc.get("landing_page_url") or "").lower()
                        if "ssrn" in lu:
                            ssrn_url = loc.get("landing_page_url") or ""
                            is_ssrn = True
                            break

                is_arxiv = any(
                    "arxiv" in (loc.get("landing_page_url") or "").lower()
                    for loc in all_locs
                )

                # Apply source filter
                if source_filter == "ssrn" and not is_ssrn:
                    continue
                if source_filter is None and (is_arxiv or is_ssrn):
                    continue  # skip papers already covered by fetch_arxiv / fetch_ssrn
                if source_filter == "nber":
                    has_nber = any(
                        "national bureau of economic research" in
                        (inst.get("display_name") or "").lower()
                        for auth in (work.get("authorships") or [])
                        for inst in (auth.get("institutions") or [])
                    )
                    if not has_nber:
                        continue

                # Build URL and item_id
                doi = (work.get("doi") or "").replace("https://doi.org/", "")
                openalex_id = (work.get("id") or "").split("/")[-1]
                landing = primary_loc.get("landing_page_url") or ""

                if is_ssrn or source_filter == "ssrn":
                    source = "ssrn"
                    url = ssrn_url or landing or (f"https://doi.org/{doi}" if doi else "")
                    m = re.search(r'/abstract=?(\d+)', url)
                    item_id = m.group(1) if m else (doi or openalex_id)
                elif source_filter == "nber":
                    source = "nber"
                    url = landing or (f"https://doi.org/{doi}" if doi else "")
                    item_id = doi or openalex_id
                else:
                    source = "scholarly"
                    url = landing or (f"https://doi.org/{doi}" if doi else "")
                    item_id = doi or openalex_id

                if not url:
                    continue

                authors = [
                    (auth.get("author") or {}).get("display_name") or ""
                    for auth in (work.get("authorships") or [])
                ][:5]
                authors = [a for a in authors if a]

                items.append(ResearchItem(
                    source=source,
                    item_type="working_paper",
                    item_id=str(item_id),
                    title=title,
                    url=url,
                    date=pub_date,
                    authors=authors,
                    abstract=abstract[:1200],
                ))
            except Exception as exc:
                log.debug("OpenAlex work parse error: %s", exc)

        log.info("OpenAlex (%s): %d items", source_filter or "scholarly", len(items))
        return items

    except Exception as exc:
        log.warning("OpenAlex fetch failed (%s): %s", source_filter or "scholarly", exc)
        return []


def fetch_ssrn(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """SSRN working papers via OpenAlex API (SSRN blocks direct scraping)."""
    return _fetch_openalex(
        session,
        query="digital competition antitrust platform DMA regulation merger cartel market power",
        lookback_days=lookback_days,
        source_filter="ssrn",
        max_results=50,
    )


def fetch_scholarly(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """Broad academic competition papers via OpenAlex (Google Scholar equivalent)."""
    return _fetch_openalex(
        session,
        query=(
            "digital competition antitrust platform economics DMA regulation "
            "merger market power gatekeeper algorithm pricing"
        ),
        lookback_days=lookback_days,
        source_filter=None,
        max_results=50,
    )


# ── Think tanks ───────────────────────────────────────────────────────────────

THINK_TANK_FEEDS = [
    ("bruegel", [
        "https://www.bruegel.org/feed/",
        "https://www.bruegel.org/rss",
        "https://www.bruegel.org/rss/publications",
    ]),
    ("cerre", ["https://cerre.eu/feed/"]),
    ("cpi",   ["https://www.competitionpolicyinternational.com/feed/"]),
]


def fetch_think_tanks(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """Think tank publications from Bruegel, CERRE, and CPI RSS feeds."""
    items: list[ResearchItem] = []

    for name, feed_urls in THINK_TANK_FEEDS:
        entries = []
        last_exc: Exception | None = None
        for feed_url in feed_urls:
            try:
                resp = _get(session, feed_url)
                resp.raise_for_status()
                entries = _parse_atom_or_rss(resp.text)
                if entries:
                    break
            except Exception as exc:
                last_exc = exc
                log.debug("Think tank feed '%s' (%s) failed: %s", name, feed_url, exc)
        if not entries and last_exc:
            log.warning("Think tank feed '%s' all URLs failed: %s", name, last_exc)

        for e in entries:
            try:
                title = e["title"].strip()
                url = e["url"].strip()
                date = e["date"]
                summary = e["summary"].strip()

                if not _has_competition_keyword(f"{title} {summary}"):
                    continue
                if not _is_recent(date, lookback_days):
                    continue

                items.append(ResearchItem(
                    source=name,
                    item_type="policy_paper",
                    item_id=url,
                    title=title,
                    url=url,
                    date=date,
                    abstract=summary[:1200],
                ))
            except Exception as exc:
                log.debug("Think tank entry parse error (%s): %s", name, exc)

    log.info("Think tanks: %d items total", len(items))
    return items


# ── EC portal helpers ────────────────────────────────────────────────────────

def _prewarm_ec_portal(session: requests.Session, url: str) -> str:
    """GET an EC Angular portal page to obtain session cookies. Returns XSRF token if found."""
    try:
        _get(session, url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        })
        for cookie_name in ("XSRF-TOKEN", "XSRF-TOKEN-EC", "csrftoken", "_csrf"):
            val = session.cookies.get(cookie_name)
            if val:
                return val
    except Exception as exc:
        log.debug("EC portal pre-warm failed for %s: %s", url, exc)
    return ""


def _eurlex_search_for_source(
    session: requests.Session,
    queries: list[str],
    source: str,
    item_type: str,
    lookback_days: int,
    filter_fn=None,
    seen: set | None = None,
) -> list[ResearchItem]:
    """Search EUR-Lex HTML and return items tagged with the given source / item_type.

    Uses the same request parameters and CSS selectors as the working
    _scrape_eurlex_search so the two code paths behave consistently.
    """
    items: list[ResearchItem] = []
    _seen = seen if seen is not None else set()

    for query in queries:
        try:
            resp = _get(
                session,
                "https://eur-lex.europa.eu/search.html",
                params={
                    "scope": "EURLEX",
                    "text": query,
                    "lang": "en",
                    "type": "quick",
                    "sortOne": "DATETIME_SORT",
                    "sortOneOrder": "desc",
                },
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.debug("EUR-Lex %s search '%s' failed: %s", source, query, exc)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select(".SearchResult, .searchResult, [class*='result-item']"):
            try:
                link = result.select_one(
                    "a[href*='legal-content'], a[href*='eur-lex.europa.eu']"
                )
                if not link:
                    continue
                title = link.get_text(strip=True)
                if not title:
                    continue
                href = link.get("href", "")
                url = href if href.startswith("http") else urljoin(
                    "https://eur-lex.europa.eu", href
                )
                if url in _seen:
                    continue
                text = result.get_text(" ", strip=True)
                if filter_fn and not filter_fn(f"{title} {text}"):
                    continue
                date_el = result.select_one("[class*='date'], time")
                date_str = _parse_date(date_el.get_text()) if date_el else ""
                if not _is_recent(date_str, lookback_days):
                    continue
                _seen.add(url)
                celex = re.search(r'[A-Z]\d{4}[A-Z]\d+', url)
                item_id = celex.group(0) if celex else url
                desc_el = result.select_one("[class*='description'], [class*='snippet']")
                abstract = desc_el.get_text(strip=True)[:600] if desc_el else text[:600]
                items.append(ResearchItem(
                    source=source,
                    item_type=item_type,
                    item_id=item_id,
                    title=title,
                    url=url,
                    date=date_str,
                    abstract=abstract,
                ))
            except Exception as exc:
                log.debug("EUR-Lex %s result parse error: %s", source, exc)

    return items


# ── DG COMP ───────────────────────────────────────────────────────────────────

_DGCOMP_DIGITAL_TERMS = [
    "google", "apple", "amazon", "meta", "microsoft", "tiktok",
    "dma", "digital", "platform", "app store", "search", "ai", "cloud",
]


def _is_digital_case(text: str) -> bool:
    tl = text.lower()
    return any(term in tl for term in _DGCOMP_DIGITAL_TERMS)


def fetch_dgcomp(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """DG COMP competition enforcement cases (open, digital/tech-related)."""
    items: list[ResearchItem] = []

    _PORTAL = "https://competition-cases.ec.europa.eu"
    xsrf = _prewarm_ec_portal(session, f"{_PORTAL}/cases")
    api_headers: dict[str, str] = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{_PORTAL}/cases",
        "Origin": _PORTAL,
    }
    if xsrf:
        api_headers["X-XSRF-TOKEN"] = xsrf

    try:
        resp = _get(
            session,
            f"{_PORTAL}/api/cases",
            params={
                "_limit": "50",
                "_sortBy": "caseOpenDate:desc",
                "lng": "en",
                "status": "OPEN",
            },
            headers=api_headers,
        )
        resp.raise_for_status()
        data = resp.json()

        # Accept either a list directly or a dict with a list value
        cases = data if isinstance(data, list) else (
            data.get("cases") or data.get("results") or data.get("data") or []
        )

        for case in cases:
            try:
                case_num = (
                    case.get("caseNum") or case.get("caseNumber") or
                    case.get("case_number") or case.get("id") or ""
                )
                case_name = (
                    case.get("caseName") or case.get("name") or
                    case.get("case_name") or ""
                )
                opened = (
                    case.get("openedDate") or case.get("caseOpenDate") or
                    case.get("open_date") or ""
                )
                status = case.get("status", "")
                case_type = case.get("type") or case.get("caseType") or ""
                policy_area = case.get("policyArea") or case.get("policy_area") or ""

                combined = f"{case_num} {case_name} {case_type} {policy_area}"
                if not _is_digital_case(combined):
                    continue

                date_str = _parse_date(str(opened)) if opened else ""
                if not _is_recent(date_str, lookback_days):
                    continue

                url = f"https://competition-cases.ec.europa.eu/cases/{case_num}"
                abstract = f"Type: {case_type}. Status: {status}."
                if policy_area:
                    abstract += f" Policy area: {policy_area}."

                items.append(ResearchItem(
                    source="dgcomp",
                    item_type="enforcement_case",
                    item_id=str(case_num),
                    title=f"{case_num} — {case_name}".strip(" —"),
                    url=url,
                    date=date_str,
                    abstract=abstract.strip(),
                ))
            except Exception as exc:
                log.debug("DG COMP case parse error: %s", exc)

    except Exception as exc:
        log.warning("DG COMP API failed (%s), trying EU Open Data Portal then HTML scrape", exc)

        # Try EU Open Data Portal (data.europa.eu) competition catalogue
        try:
            resp = _get(
                session,
                "https://data.europa.eu/api/hub/search/datasets",
                params={
                    "filter": "catalogue:comp",
                    "query": "EU Competition antitrust digital merger",
                    "sort": "modified+desc",
                    "limit": "20",
                    "page": "1",
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            portal_data = resp.json()
            datasets = (
                portal_data.get("result", {}).get("results", [])
                or portal_data.get("results", [])
                or []
            )
            for ds in datasets:
                try:
                    title = ds.get("title", {})
                    title_en = title.get("en") or (list(title.values())[0] if title else "") or ""
                    if not title_en or not _is_digital_case(title_en):
                        continue
                    ds_id = ds.get("id") or ds.get("identifier") or ""
                    modified = ds.get("modified") or ds.get("issued") or ""
                    date_str = _parse_date(str(modified)) if modified else ""
                    if not _is_recent(date_str, lookback_days):
                        continue
                    ds_url = ds.get("landingPage") or ds.get("catalog_url") or f"https://data.europa.eu/data/datasets/{ds_id}"
                    desc = ds.get("description", {})
                    abstract = desc.get("en") or (list(desc.values())[0] if desc else "") or ""
                    items.append(ResearchItem(
                        source="dgcomp",
                        item_type="enforcement_case",
                        item_id=str(ds_id),
                        title=title_en,
                        url=ds_url,
                        date=date_str,
                        abstract=abstract[:600],
                    ))
                except Exception as exc2:
                    log.debug("EU Open Data Portal dataset parse error: %s", exc2)
            if items:
                log.info("DG COMP (EU Open Data Portal): %d items", len(items))
        except Exception as exc2:
            log.warning("EU Open Data Portal fallback failed: %s", exc2)

        if items:
            log.info("DG COMP: %d items", len(items))
            return items

        # Final fallback: HTML scrape
        try:
            resp = _get(
                session,
                "https://competition-cases.ec.europa.eu/cases",
                params={"lng": "EN", "status": "OPEN", "type": "ALL"},
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for row in soup.select("tr, [class*='result-item'], [class*='case-item']"):
                try:
                    link = row.select_one("a")
                    if not link:
                        continue
                    title = link.get_text(strip=True)
                    href = link.get("href", "")
                    url = href if href.startswith("http") else urljoin(
                        "https://competition-cases.ec.europa.eu", href
                    )
                    text = row.get_text(" ", strip=True)
                    if not _is_digital_case(text):
                        continue
                    date_match = re.search(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}', text)
                    date_str = _parse_date(date_match.group(0)) if date_match else ""
                    if not _is_recent(date_str, lookback_days):
                        continue
                    case_num_match = re.search(r'AT\.\d+|COMP/\w+/\d+', text)
                    item_id = case_num_match.group(0) if case_num_match else url
                    items.append(ResearchItem(
                        source="dgcomp",
                        item_type="enforcement_case",
                        item_id=item_id,
                        title=title,
                        url=url,
                        date=date_str,
                        abstract=text[:600],
                    ))
                except Exception as exc:
                    log.debug("DG COMP HTML row parse error: %s", exc)
        except Exception as exc2:
            log.warning("DG COMP HTML fallback also failed: %s", exc2)

    # EUR-Lex fallback: competition decisions and procedures published in OJ.
    # Use 30-day minimum since formal Commission decisions take time to publish.
    if not items:
        log.info("DG COMP: all portal/portal-fallback paths empty, trying EUR-Lex")
        eurlex_items = _eurlex_search_for_source(
            session,
            queries=[
                "Commission antitrust digital platform decision",
                "competition enforcement digital gatekeeper AT.40",
                "merger digital acquisition Commission clearance",
            ],
            source="dgcomp",
            item_type="enforcement_case",
            lookback_days=max(lookback_days, 30),
        )
        items.extend(eurlex_items)
        if eurlex_items:
            log.info("DG COMP (EUR-Lex fallback): %d items", len(eurlex_items))

    log.info("DG COMP: %d items", len(items))
    return items


# ── DMA Acquisitions ──────────────────────────────────────────────────────────

_DMA_ACQ_TYPES = {"concentration", "acquisition", "notification", "art_14", "14"}


def _is_dma_acquisition(case: dict) -> bool:
    """Return True if this DMA case looks like an Article 14 acquisition notification."""
    case_type = str(
        case.get("caseType") or case.get("type") or case.get("case_type") or ""
    ).lower()
    return any(t in case_type for t in _DMA_ACQ_TYPES)


_DMA_ACQ_API_CANDIDATES = [
    # Try acquisitions-specific endpoint first
    ("https://digital-markets-act-cases.ec.europa.eu/api/acquisitions", {}),
    # Then general cases endpoint with type filter
    ("https://digital-markets-act-cases.ec.europa.eu/api/cases", {"type": "ACQUISITION", "_limit": "100", "_sortBy": "openedDate:desc"}),
    # Then unfiltered cases (client-side type filter)
    ("https://digital-markets-act-cases.ec.europa.eu/api/cases", {"_limit": "100", "_sortBy": "openedDate:desc"}),
]


def fetch_dma_acquisitions(session: requests.Session, lookback_days: int) -> list[ResearchItem]:
    """DMA Article 14 gatekeeper acquisition notifications — all available, no lookback filter."""
    items: list[ResearchItem] = []

    _DMA_PORTAL = "https://digital-markets-act-cases.ec.europa.eu"
    xsrf = _prewarm_ec_portal(session, f"{_DMA_PORTAL}/acquisitions")
    dma_api_headers: dict[str, str] = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{_DMA_PORTAL}/acquisitions",
        "Origin": _DMA_PORTAL,
    }
    if xsrf:
        dma_api_headers["X-XSRF-TOKEN"] = xsrf

    # Try API endpoints in order of specificity
    data: list | None = None
    for api_url, api_params in _DMA_ACQ_API_CANDIDATES:
        try:
            resp = _get(session, api_url, params=api_params, headers=dma_api_headers)
            resp.raise_for_status()
            raw = resp.json()
            cases_candidate = raw if isinstance(raw, list) else (
                raw.get("cases") or raw.get("acquisitions") or
                raw.get("results") or raw.get("data") or []
            )
            if cases_candidate:
                data = cases_candidate
                log.debug("DMA acquisitions: got %d entries from %s", len(data), api_url)
                break
        except Exception as exc:
            log.debug("DMA acquisitions API %s failed: %s", api_url, exc)

    if data is not None:
        for case in data:
            try:
                if not _is_dma_acquisition(case):
                    continue

                case_id = (
                    case.get("caseId") or case.get("id") or
                    case.get("caseNum") or case.get("caseNumber") or ""
                )
                case_name = (
                    case.get("caseName") or case.get("name") or
                    case.get("case_name") or ""
                )
                gatekeeper = (
                    case.get("gatekeeper") or case.get("gatekeeperName") or
                    case.get("notifyingParty") or ""
                )
                target = (
                    case.get("target") or case.get("targetName") or
                    case.get("acquiredCompany") or case.get("subject") or ""
                )
                opened = (
                    case.get("openedDate") or case.get("notificationDate") or
                    case.get("open_date") or ""
                )
                status = case.get("status") or case.get("caseStatus") or ""
                case_type = case.get("caseType") or case.get("type") or ""

                date_str = _parse_date(str(opened)) if opened else ""
                url = f"https://digital-markets-act-cases.ec.europa.eu/cases/{case_id}"

                display_name = gatekeeper or case_name
                display_target = target or case_name
                title = (
                    f"{display_name} — DMA acquisition notification: {display_target}"
                    if display_name and display_target and display_name != display_target
                    else f"DMA acquisition notification: {case_name or case_id}"
                )

                abstract_parts = [f"Case: {case_id}."]
                if gatekeeper:
                    abstract_parts.append(f"Gatekeeper: {gatekeeper}.")
                if target:
                    abstract_parts.append(f"Target: {target}.")
                if opened:
                    abstract_parts.append(f"Notification date: {date_str or opened}.")
                if status:
                    abstract_parts.append(f"Status: {status}.")
                if case_type:
                    abstract_parts.append(f"Type: {case_type}.")

                items.append(ResearchItem(
                    source="dma_acquisitions",
                    item_type="acquisition_notification",
                    item_id=str(case_id),
                    title=title,
                    url=url,
                    date=date_str,
                    abstract=" ".join(abstract_parts),
                ))
            except Exception as exc:
                log.debug("DMA acquisition case parse error: %s", exc)
    else:
        # HTML fallback — scrape the acquisitions register page
        log.warning("DMA acquisitions: all API endpoints empty, trying HTML scrape")
        try:
            resp = _get(
                session,
                "https://digital-markets-act-cases.ec.europa.eu/acquisitions",
                headers={"Accept": "text/html"},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for row in soup.select("tr, [class*='result-item'], [class*='case-item'], [class*='acquisition']"):
                try:
                    link = row.select_one("a")
                    if not link:
                        continue
                    title = link.get_text(strip=True)
                    href = link.get("href", "")
                    url = href if href.startswith("http") else urljoin(
                        "https://digital-markets-act-cases.ec.europa.eu", href
                    )
                    text = row.get_text(" ", strip=True)
                    date_match = re.search(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}', text)
                    date_str = _parse_date(date_match.group(0)) if date_match else ""
                    items.append(ResearchItem(
                        source="dma_acquisitions",
                        item_type="acquisition_notification",
                        item_id=url,
                        title=f"DMA acquisition notification: {title}",
                        url=url,
                        date=date_str,
                        abstract=text[:600],
                    ))
                except Exception as exc:
                    log.debug("DMA acquisitions HTML row parse error: %s", exc)
        except Exception as exc2:
            log.warning("DMA acquisitions HTML fallback also failed: %s", exc2)

    # EUR-Lex fallback: OJ C-series notices for DMA Art 14 acquisition notifications
    if not items:
        log.info("DMA acquisitions: all portal paths empty, trying EUR-Lex")
        eurlex_items = _eurlex_search_for_source(
            session,
            queries=[
                "Article 14 Digital Markets Act acquisition notification gatekeeper",
                "concentration notification Digital Markets Act DMA gatekeeper",
                "DMA Article 14 acquisition notification concentration",
            ],
            source="dma_acquisitions",
            item_type="acquisition_notification",
            # DMA came into force March 2024; use 2-year window to capture all notifications
            lookback_days=max(lookback_days, 730),
        )
        items.extend(eurlex_items)
        if eurlex_items:
            log.info("DMA acquisitions (EUR-Lex fallback): %d items", len(eurlex_items))

    log.info("DMA acquisitions: %d items", len(items))
    return items


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Research monitor for digital competition policy")
    ap.add_argument("--output", default="data/research_items.json")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument(
        "--only",
        help="Comma-separated sources to run: arxiv,eurlex,cjeu,nber,ssrn,scholarly,thinktanks,dgcomp,dma",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    sources = {s.strip().lower() for s in args.only.split(",")} if args.only else {
        "arxiv", "eurlex", "cjeu", "nber", "ssrn", "scholarly", "thinktanks", "dgcomp", "dma"
    }
    session = requests.Session()

    all_items: list[ResearchItem] = []

    if "arxiv" in sources:
        log.info("── arXiv ──────────────────────────────────────────────")
        all_items.extend(fetch_arxiv(session, args.lookback))

    if "eurlex" in sources:
        log.info("── EUR-Lex ────────────────────────────────────────────")
        all_items.extend(fetch_eurlex(session, args.lookback))

    if "cjeu" in sources:
        log.info("── CJEU ───────────────────────────────────────────────")
        all_items.extend(fetch_cjeu(session, args.lookback))

    if "nber" in sources:
        log.info("── NBER ───────────────────────────────────────────────")
        all_items.extend(fetch_nber(session, args.lookback))

    if "ssrn" in sources:
        log.info("── SSRN ───────────────────────────────────────────────")
        all_items.extend(fetch_ssrn(session, args.lookback))

    if "scholarly" in sources:
        log.info("── Scholarly (Semantic Scholar) ───────────────────────")
        all_items.extend(fetch_scholarly(session, args.lookback))

    if "thinktanks" in sources:
        log.info("── Think tanks ────────────────────────────────────────")
        all_items.extend(fetch_think_tanks(session, args.lookback))

    if "dgcomp" in sources:
        log.info("── DG COMP ────────────────────────────────────────────")
        all_items.extend(fetch_dgcomp(session, args.lookback))

    if "dma" in sources:
        log.info("── DMA acquisitions ───────────────────────────────────")
        all_items.extend(fetch_dma_acquisitions(session, args.lookback))

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[ResearchItem] = []
    for item in all_items:
        if item.url and item.url not in seen:
            seen.add(item.url)
            unique.append(item)

    unique.sort(key=lambda x: x.date, reverse=True)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": args.lookback,
        "total_items": len(unique),
        "items": [asdict(it) for it in unique],
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    log.info("Wrote %d items to %s", len(unique), args.output)


if __name__ == "__main__":
    main()
