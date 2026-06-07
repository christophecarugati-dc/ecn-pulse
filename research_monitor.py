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

EURLEX_FEED_URL = "https://eur-lex.europa.eu/tools/rss.do"
EURLEX_FEED_PARAMS = [
    # Recently added documents (broad — filtered locally by keyword)
    {"type": "recently-added", "facet_lang": "EN"},
    # Recent Official Journal C-series (competition decisions often appear here)
    {"type": "latest-oj", "facet_lang": "EN"},
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

    for params in EURLEX_FEED_PARAMS:
        try:
            resp = _get(session, EURLEX_FEED_URL, params=params,
                        headers={"Accept": "application/rss+xml, application/atom+xml, */*"})
            resp.raise_for_status()
        except Exception as exc:
            log.warning("EUR-Lex feed (%s) error: %s", params, exc)
            continue

        entries = _parse_atom_or_rss(resp.text)
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

            # Classify document type from title / URL
            item_type = _classify_eurlex_type(title, url)

            # CELEX identifier for stable IDs
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

        log.info("EUR-Lex feed %s: %d relevant items", params.get("type"), kept)

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Research monitor for digital competition policy")
    ap.add_argument("--output", default="data/research_items.json")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--only", help="Comma-separated sources to run: arxiv,eurlex,cjeu")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    sources = {s.strip().lower() for s in args.only.split(",")} if args.only else {"arxiv", "eurlex", "cjeu"}
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
