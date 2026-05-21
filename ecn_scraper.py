"""
ECN Pulse — scraper for European Competition Network member authorities.

Fetches the latest press releases / news items from every ECN member NCA,
plus DG COMP, EFTA Surveillance Authority, and the UK CMA (observer).
Outputs a stable JSON document with the shape:

  {
    "generated_at": "2026-05-13T04:00:00Z",
    "items": [
      {
        "authority_code": "DE",
        "authority_name": "Bundeskartellamt",
        "country": "DE",
        "title": "...",
        "url": "https://...",
        "date": "2026-05-12",
        "snippet": "...",
        "category": "merger|antitrust|cartel|state_aid|policy|other",
        "language": "en",
        "source_fetched_at": "2026-05-13T04:00:00Z"
      },
      ...
    ],
    "errors": [ {"authority_code": "...", "error": "..."} ]
  }

Designed to run anywhere with outbound HTTP access:
  - locally:           python ecn_scraper.py --output ecn_pulse.json
  - GitHub Action:     see .github/workflows/scrape.yml
  - Cowork scheduled:  once Settings → Capabilities permits the domains

Network egress is *required*. Inside Cowork's default sandbox most NCA
domains are blocked; either lift the allowlist or run this elsewhere.

The HTML selectors are best-effort starting points and will need tuning
the first time you run it — authorities change their pages and not every
selector is stable. The scraper degrades gracefully: a parser failure on
one authority is logged in `errors` and does not stop the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

USER_AGENT = (
    "ECN-Pulse/0.1 (+https://github.com/christophecarugati-dc/ecn-pulse) "
    "Mozilla/5.0 (research; contact: christophe.carugati@digital-competition.com)"
)
REQUEST_TIMEOUT = 20
RATE_LIMIT_SECONDS = 1.0
DEFAULT_MAX_ITEMS = 12

log = logging.getLogger("ecn-pulse")


# ---------------------------------------------------------------------------
# Authority configuration
# ---------------------------------------------------------------------------
# Each authority has:
#   code        ISO-style country code (or 'EU', 'EFTA', 'UK')
#   name        official short name
#   url         press-release / news index URL
#   parser      one of: 'gov_uk_json' | 'html_list' | 'rss'
#   selectors   for html_list: a dict {item, title, link, date, snippet}
#               CSS selectors; relative to the item selector except for `item` itself.
#               Date format can be set via `date_format` (strftime); otherwise we use
#               dateutil's flexible parser.
# ---------------------------------------------------------------------------

@dataclass
class Authority:
    code: str
    name: str
    url: str
    parser: str
    selectors: dict = field(default_factory=dict)
    language: str = "en"
    base_url: Optional[str] = None  # for resolving relative hrefs

    def absolute_url(self, href: str) -> str:
        base = self.base_url or self.url
        return urljoin(base, href) if href else ""


AUTHORITIES: list[Authority] = [
    # ---- European-level ----
    Authority(
        code="EU",
        name="European Commission · DG COMP",
        url="https://competition-policy.ec.europa.eu/news_en",
        base_url="https://competition-policy.ec.europa.eu/",
        parser="html_list",
        selectors={
            "item": "article.ecl-content-item, li.ecl-list-item, div.ecl-card",
            "title": "h1, h2, h3, a",
            "link": "a",
            "date": "time, .ecl-date-block, .ecl-meta__item",
            "snippet": "p, .ecl-content-item__description",
        },
    ),
    Authority(
        code="EFTA",
        name="EFTA Surveillance Authority",
        url="https://www.eftasurv.int/news",
        base_url="https://www.eftasurv.int/",
        parser="html_list",
        selectors={
            "item": "article, .news-item, .post",
            "title": "h2, h3, a",
            "link": "a",
            "date": "time, .date, .meta",
            "snippet": "p, .excerpt",
        },
    ),

    # ---- UK (observer; not ECN post-Brexit but still part of any sensible EU scope) ----
    Authority(
        code="UK",
        name="Competition and Markets Authority",
        url=(
            "https://www.gov.uk/api/search.json?"
            "filter_organisations=competition-and-markets-authority"
            "&filter_content_store_document_type[]=press_release"
            "&filter_content_store_document_type[]=news_story"
            "&filter_content_store_document_type[]=decision"
            "&order=-public_timestamp&count=20"
        ),
        parser="gov_uk_json",
    ),

    # ---- ECN member NCAs (alphabetical by code) ----
    Authority(
        code="AT", name="Bundeswettbewerbsbehörde (BWB)",
        url="https://www.bwb.gv.at/en/news/news",
        base_url="https://www.bwb.gv.at/",
        parser="html_list",
        selectors={"item": "article, .news-entry, li", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="BE", name="Belgian Competition Authority (BMA/ABC)",
        url="https://www.bma-abc.be/en/news",
        base_url="https://www.bma-abc.be/",
        parser="html_list",
        selectors={"item": "article, .news-item, li.news",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="BG", name="Commission for Protection of Competition",
        url="https://www.cpc.bg/en/News",
        base_url="https://www.cpc.bg/",
        parser="html_list",
        selectors={"item": "article, .news-item, li", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date, .meta", "snippet": "p"},
    ),
    Authority(
        code="HR", name="Croatian Competition Agency (AZTN)",
        url="https://www.aztn.hr/en/news/",
        base_url="https://www.aztn.hr/",
        parser="html_list",
        selectors={"item": "article, .news-item, .post",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p, .excerpt"},
    ),
    Authority(
        code="CY", name="Commission for the Protection of Competition (Cyprus)",
        url="https://www.competition.gov.cy/en",
        base_url="https://www.competition.gov.cy/",
        parser="html_list",
        selectors={"item": "article, .news-item, li", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="CZ", name="Office for the Protection of Competition (ÚOHS)",
        url="https://www.uohs.cz/en/information-centre/press-releases.html",
        base_url="https://www.uohs.cz/",
        parser="html_list",
        selectors={"item": "article, .article, li.tz", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="DK", name="Konkurrence- og Forbrugerstyrelsen",
        url="https://www.en.kfst.dk/news/",
        base_url="https://www.en.kfst.dk/",
        parser="html_list",
        selectors={"item": "article, .news-item", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p, .excerpt"},
    ),
    Authority(
        code="EE", name="Estonian Competition Authority",
        url="https://www.konkurentsiamet.ee/en/news",
        base_url="https://www.konkurentsiamet.ee/",
        parser="html_list",
        selectors={"item": "article, .news-item, li.news",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="FI", name="Finnish Competition and Consumer Authority (KKV)",
        url="https://www.kkv.fi/en/current-issues/press-releases/",
        base_url="https://www.kkv.fi/",
        parser="html_list",
        selectors={"item": "article, .release-item, .news-item",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p, .excerpt"},
    ),
    Authority(
        code="FR", name="Autorité de la concurrence",
        url="https://www.autoritedelaconcurrence.fr/en/press-releases",
        base_url="https://www.autoritedelaconcurrence.fr/",
        parser="html_list",
        selectors={"item": "article, .views-row, .press-release-item",
                   "title": "h2, h3, .field--name-title, a",
                   "link": "a", "date": "time, .date-display-single, .field--name-field-date",
                   "snippet": ".field--name-body, .views-field-body, p"},
    ),
    Authority(
        code="DE", name="Bundeskartellamt",
        url="https://www.bundeskartellamt.de/EN/Press/PressReleases/pressreleases_node.html",
        base_url="https://www.bundeskartellamt.de/",
        parser="html_list",
        selectors={"item": "div.c-teaser, article, li.c-articleList__item",
                   "title": "h3, h2, a, .c-teaser__headline",
                   "link": "a", "date": "time, .c-publication-info__date, .meta",
                   "snippet": "p, .c-teaser__text"},
    ),
    Authority(
        code="GR", name="Hellenic Competition Commission",
        url="https://www.epant.gr/en/enimerosi/press-releases.html",
        base_url="https://www.epant.gr/",
        parser="html_list",
        selectors={"item": "article, .item, li.news", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="HU", name="Gazdasági Versenyhivatal (GVH)",
        url="https://www.gvh.hu/en/press_room/press_releases",
        base_url="https://www.gvh.hu/",
        parser="html_list",
        selectors={"item": "article, .news, li.news-item",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date, .meta-date", "snippet": "p"},
    ),
    Authority(
        code="IE", name="Competition and Consumer Protection Commission",
        url="https://www.ccpc.ie/business/about/news/",
        base_url="https://www.ccpc.ie/",
        parser="html_list",
        selectors={"item": "article, .news-item, .post",
                   "title": "h2, h3, a, .entry-title",
                   "link": "a", "date": "time, .date, .entry-date",
                   "snippet": "p, .excerpt, .entry-summary"},
    ),
    Authority(
        code="IT", name="Autorità Garante della Concorrenza e del Mercato (AGCM)",
        url="https://en.agcm.it/en/media/press-releases",
        base_url="https://en.agcm.it/",
        parser="html_list",
        selectors={"item": "article, .item, .views-row",
                   "title": "h2, h3, .field--name-title, a",
                   "link": "a", "date": "time, .date, .field--name-field-date",
                   "snippet": "p, .field--name-body"},
    ),
    Authority(
        code="LV", name="Konkurences padome",
        url="https://www.kp.gov.lv/en/news",
        base_url="https://www.kp.gov.lv/",
        parser="html_list",
        selectors={"item": "article, .news-item", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="LT", name="Konkurencijos taryba",
        url="https://kt.gov.lt/en/news",
        base_url="https://kt.gov.lt/",
        parser="html_list",
        selectors={"item": "article, .news-item, li.news",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="LU", name="Autorité de la concurrence du Luxembourg (ADLC)",
        url="https://concurrence.public.lu/en/news.html",
        base_url="https://concurrence.public.lu/",
        parser="html_list",
        selectors={"item": "article, .news-item, .actu",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="MT", name="MCCAA · Office for Competition",
        url="https://mccaa.org.mt/topics/view/office-for-competition",
        base_url="https://mccaa.org.mt/",
        parser="html_list",
        selectors={"item": "article, .news-item, .post",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="NL", name="Autoriteit Consument & Markt (ACM)",
        url="https://www.acm.nl/en/publications/news",
        base_url="https://www.acm.nl/",
        parser="html_list",
        selectors={"item": "article, .acm-card, li.acm-list-item",
                   "title": "h2, h3, a, .acm-card__title",
                   "link": "a", "date": "time, .acm-card__date, .acm-meta__date",
                   "snippet": "p, .acm-card__intro"},
    ),
    Authority(
        code="PL", name="Urząd Ochrony Konkurencji i Konsumentów (UOKiK)",
        url="https://uokik.gov.pl/news.php",
        base_url="https://uokik.gov.pl/",
        parser="html_list",
        selectors={"item": "article, .news-item, .article",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date, .data", "snippet": "p"},
    ),
    Authority(
        code="PT", name="Autoridade da Concorrência (AdC)",
        url="https://www.concorrencia.pt/en/news",
        base_url="https://www.concorrencia.pt/",
        parser="html_list",
        selectors={"item": "article, .news-item, .listing-item",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date, .meta-date",
                   "snippet": "p, .excerpt"},
    ),
    Authority(
        code="RO", name="Consiliul Concurenței",
        url="http://www.consiliulconcurentei.ro/en/press-releases.html",
        base_url="http://www.consiliulconcurentei.ro/",
        parser="html_list",
        selectors={"item": "article, .news-item, li", "title": "h2, h3, a",
                   "link": "a", "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="SK", name="Protimonopolný úrad SR (PMÚ)",
        url="https://www.antimon.gov.sk/news/",
        base_url="https://www.antimon.gov.sk/",
        parser="html_list",
        selectors={"item": "article, .news-item, .post",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="SI", name="Javna agencija RS za varstvo konkurence (AVK)",
        url="https://www.varstvo-konkurence.si/en/news/",
        base_url="https://www.varstvo-konkurence.si/",
        parser="html_list",
        selectors={"item": "article, .news-item, .post",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p"},
    ),
    Authority(
        code="ES", name="Comisión Nacional de los Mercados y la Competencia (CNMC)",
        url="https://www.cnmc.es/en/news",
        base_url="https://www.cnmc.es/",
        parser="html_list",
        selectors={"item": "article, .views-row, .news-item",
                   "title": "h2, h3, .field--name-title, a",
                   "link": "a", "date": "time, .date, .field--name-field-date",
                   "snippet": "p, .field--name-body"},
    ),
    Authority(
        code="SE", name="Konkurrensverket",
        url="https://www.konkurrensverket.se/en/news/",
        base_url="https://www.konkurrensverket.se/",
        parser="html_list",
        selectors={"item": "article, .news-item, .listing__item",
                   "title": "h2, h3, a", "link": "a",
                   "date": "time, .date", "snippet": "p, .excerpt"},
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "merger": ["merger", "concentration", "acquisition", "phase i", "phase ii",
               "art. 22", "article 22", "smc", "siec", "fúsion", "fusion",
               "konzentration", "Übernahme", "concentrazione"],
    "antitrust": ["abuse", "dominant", "article 102", "art. 102", "monopoly",
                  "vertical agreement", "vertical restraints",
                  "missbrauch", "abus de position"],
    "cartel": ["cartel", "bid rigging", "price fixing", "article 101",
               "art. 101", "kartell", "entente"],
    "state_aid": ["state aid", "subsidy", "subvention", "staatliche beihilfe",
                  "ayuda de estado"],
    "policy": ["consultation", "guidelines", "guidance", "call for evidence",
               "policy", "advocacy", "leitfaden", "consulta"],
    "digital": ["dma", "gatekeeper", "platform", "digital markets",
                "p2b", "online intermediation", "data act"],
}


def categorize(text: str) -> str:
    t = (text or "").lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in t for k in keywords):
            return cat
    return "other"


def parse_date_safe(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    # Try ISO first
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        pass
    # Try dateutil
    try:
        return dateparser.parse(s, dayfirst=True, fuzzy=True).date().isoformat()
    except (ValueError, TypeError, dateparser.ParserError):
        return None


def fetch(url: str, accept: str = "text/html,application/xhtml+xml") -> str:
    log.debug("GET %s", url)
    headers = {"User-Agent": USER_AGENT, "Accept": accept,
               "Accept-Language": "en-GB,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def text_of(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def first(soup, selectors_csv: Optional[str]):
    if not selectors_csv:
        return None
    for sel in [s.strip() for s in selectors_csv.split(",")]:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            return el
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_gov_uk_json(auth: Authority, max_items: int) -> list[dict]:
    raw = fetch(auth.url, accept="application/json")
    payload = json.loads(raw)
    items = []
    for r in (payload.get("results") or [])[:max_items]:
        title = r.get("title") or ""
        link = r.get("link") or ""
        if link and not link.startswith("http"):
            link = "https://www.gov.uk" + link
        date = (r.get("public_timestamp") or "")[:10] or None
        snippet = (r.get("description") or "").strip()
        items.append(make_item(auth, title, link, date, snippet))
    return items


def parse_html_list(auth: Authority, max_items: int) -> list[dict]:
    html = fetch(auth.url)
    soup = BeautifulSoup(html, "html.parser")
    sel = auth.selectors
    item_sel = sel.get("item") or "article"
    # Try each item-selector candidate until we get a non-empty list
    nodes = []
    for candidate in [s.strip() for s in item_sel.split(",")]:
        try:
            found = soup.select(candidate)
        except Exception:
            continue
        if found:
            nodes = found
            break
    if not nodes:
        raise RuntimeError(f"no items found with selector {item_sel!r}")

    items = []
    for node in nodes[:max_items]:
        title_el = first(node, sel.get("title")) or node
        link_el = first(node, sel.get("link"))
        date_el = first(node, sel.get("date"))
        snip_el = first(node, sel.get("snippet"))

        title = text_of(title_el)
        href = (link_el.get("href") if link_el else None) or ""
        link = auth.absolute_url(href) if href else ""
        date_raw = (date_el.get("datetime") if (date_el and date_el.has_attr("datetime"))
                    else text_of(date_el)) if date_el else None
        date = parse_date_safe(date_raw)
        snippet = text_of(snip_el)
        if not title or len(title) < 4:
            continue
        items.append(make_item(auth, title, link, date, snippet))
    return items


PARSERS: dict[str, Callable[[Authority, int], list[dict]]] = {
    "gov_uk_json": parse_gov_uk_json,
    "html_list": parse_html_list,
}


# ---------------------------------------------------------------------------
# Item builder
# ---------------------------------------------------------------------------

def make_item(auth: Authority, title: str, url: str,
              date: Optional[str], snippet: str) -> dict:
    return {
        "authority_code": auth.code,
        "authority_name": auth.name,
        "country": auth.code if auth.code not in ("EU", "EFTA") else auth.code,
        "title": title,
        "url": url,
        "date": date,
        "snippet": snippet[:600] if snippet else "",
        "category": categorize(f"{title} {snippet}"),
        "language": auth.language,
        "source_fetched_at": datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape(only: Optional[list[str]] = None,
           max_items: int = DEFAULT_MAX_ITEMS) -> dict:
    items: list[dict] = []
    errors: list[dict] = []
    selected = [a for a in AUTHORITIES if (not only or a.code in only)]
    for auth in selected:
        log.info("Scraping %s — %s", auth.code, auth.name)
        try:
            parser = PARSERS[auth.parser]
            got = parser(auth, max_items)
            items.extend(got)
            log.info("  %d items", len(got))
        except requests.HTTPError as e:
            log.warning("  HTTP error for %s: %s", auth.code, e)
            errors.append({"authority_code": auth.code,
                           "error": f"HTTP {e.response.status_code}: {e}"})
        except requests.RequestException as e:
            log.warning("  request error for %s: %s", auth.code, e)
            errors.append({"authority_code": auth.code, "error": str(e)})
        except Exception as e:
            log.warning("  parse error for %s: %s", auth.code, e)
            errors.append({"authority_code": auth.code, "error": str(e)})
        time.sleep(RATE_LIMIT_SECONDS)

    # Sort newest first by date string (ISO sorts correctly)
    items.sort(key=lambda x: (x.get("date") or "0000-00-00"), reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat(),
        "total_items": len(items),
        "total_errors": len(errors),
        "items": items,
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ECN Pulse scraper")
    ap.add_argument("--output", default="ecn_pulse.json",
                    help="output JSON path (default: ecn_pulse.json)")
    ap.add_argument("--only", default=None,
                    help="comma-separated authority codes (e.g. 'EU,DE,FR,IT')")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX_ITEMS,
                    help=f"max items per authority (default {DEFAULT_MAX_ITEMS})")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    only = [c.strip().upper() for c in args.only.split(",")] if args.only else None
    payload = scrape(only=only, max_items=args.max)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    log.info("Wrote %s — %d items, %d errors",
             args.output, payload["total_items"], payload["total_errors"])
    return 0 if payload["total_items"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
