"""
Weekly Digest Generator — synthesizes competition policy developments from all
data sources into a structured weekly briefing.

In --no-ai mode (free): aggregates and organises data without any API calls.
In AI mode:             uses Claude Haiku for per-item analysis and Claude
                        Sonnet for the weekly narrative.

Reads:
  data/ecn_pulse.json              ECN enforcement press releases
  data/research_items.json         arXiv papers + EUR-Lex + CJEU (research_monitor.py)
  court-cases/data/court_links.json CJEU case linker

Output:
  data/digests/YYYY-WNN.json       week-specific snapshot
  data/digests/latest.json         always points to the latest digest

Usage:
  python digest_generator.py --no-ai
  python digest_generator.py                  # requires ANTHROPIC_API_KEY env var
  python digest_generator.py --lookback 7 --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("digest-generator")

MODEL_SUMMARY = "claude-haiku-4-5-20251001"   # fast, cheap — one call per item
MODEL_SYNTHESIS = "claude-sonnet-4-6"          # quality — one call for the week

THEME_TAGS = [
    "antitrust enforcement", "merger control", "DMA/DSA", "AI regulation",
    "platform economics", "market definition", "behavioral economics",
    "court proceedings", "academic research", "data markets",
    "interoperability", "algorithmic pricing", "self-preferencing",
    "gatekeeper obligations", "consumer harm",
]

SOURCE_LABELS = {
    "arxiv": "Academic (arXiv)",
    "eurlex": "EU Regulation (EUR-Lex)",
    "cjeu": "Court (CJEU)",
}


# ── Data loading ──────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        log.warning("Could not load %s: %s", path, exc)
        return {}


def _is_recent(date_str: str, days: int) -> bool:
    if not date_str:
        return True
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return True


def _collect_items(ecn_path: str, research_path: str, court_path: str, lookback_days: int) -> list[dict]:
    """Load and normalise items from all data sources."""
    items: list[dict] = []

    # ECN press releases — only competition-relevant items
    # Category whitelist: always include these
    _ECN_CATEGORY_WHITELIST = {"digital", "antitrust", "merger", "cartel", "policy"}
    # Whole-word English keyword patterns for "other" category items
    import re as _re
    _ECN_KW_PATTERNS = _re.compile(
        r'\b('
        r'digital market|platform|app store|search engine|e-commerce|'
        r'artificial intelligence|machine learning|algorithm|'
        r'DMA|DSA|gatekeeper|self-preferencing|interoperability|'
        r'big tech|Google|Amazon|Apple|Microsoft|Meta(?!\w)|TikTok|'
        r'market power|dominant position|abuse of dominance|'
        r'merger control|gun-jumping|killer acquisition|'
        r'data portability|online marketplace|cloud computing'
        r')\b',
        _re.IGNORECASE,
    )

    def _ecn_is_relevant(it: dict) -> bool:
        if it.get("category") in _ECN_CATEGORY_WHITELIST:
            return True
        # For "other" / unlabelled items, require an explicit keyword hit in the English title
        if it.get("language", "en") != "en":
            return False
        title = it.get("title", "")
        return bool(_ECN_KW_PATTERNS.search(title))

    ecn = _load(ecn_path)
    for it in ecn.get("items", []):
        if not _is_recent(it.get("date", ""), lookback_days):
            continue
        if not _ecn_is_relevant(it):
            continue
        items.append({
            "source": it.get("authority_code", "ECN"),
            "source_label": it.get("authority_name", "ECN"),
            "item_type": it.get("category", "press_release"),
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "date": it.get("date", ""),
            "abstract": it.get("snippet", ""),
            "authors": [],
            "categories": [it.get("category", "")],
        })

    # Research items (arXiv + EUR-Lex + CJEU)
    research = _load(research_path)
    for it in research.get("items", []):
        if not _is_recent(it.get("date", ""), lookback_days):
            continue
        src = it.get("source", "research")
        items.append({
            "source": src,
            "source_label": SOURCE_LABELS.get(src, src.upper()),
            "item_type": it.get("item_type", "document"),
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "date": it.get("date", ""),
            "abstract": it.get("abstract", ""),
            "authors": it.get("authors", []),
            "categories": it.get("categories", []),
        })

    # CJEU case linker — cases are nested under a "dgcomp" key
    court = _load(court_path)
    for case in court.get("cases", []):
        dgcomp = case.get("dgcomp") or case  # support both flat and nested layouts
        date = dgcomp.get("decision_date", "")
        case_number = dgcomp.get("case_number", "")
        name = dgcomp.get("case_name", "") or dgcomp.get("name", "")
        url = dgcomp.get("url", "") or dgcomp.get("commission_url", "")
        if not case_number and not name:
            continue
        if not _is_recent(date, lookback_days):
            continue
        fine = dgcomp.get("fine_eur")
        fine_str = f"€{fine:,}" if isinstance(fine, (int, float)) else (str(fine) if fine else "N/A")
        appeals = case.get("appeals", [])
        appeal_str = f" {len(appeals)} appeal(s) pending." if appeals else ""
        items.append({
            "source": "cjeu_linker",
            "source_label": "Court (CJEU Case Linker)",
            "item_type": "court_case",
            "title": f"{case_number} — {name}".strip(" —"),
            "url": url,
            "date": date,
            "abstract": (
                f"{dgcomp.get('decision_type', '')}. Fine: {fine_str}."
                f" Sector: {dgcomp.get('sector', '')}."
                f"{appeal_str}"
            ).strip(),
            "authors": [],
            "categories": [dgcomp.get("case_type", "")],
        })

    # Sort by date descending, unknowns last
    items.sort(key=lambda x: x.get("date") or "0000-00-00", reverse=True)
    return items


# ── Claude API helpers ────────────────────────────────────────────────────────

def _call_claude(client: Any, model: str, prompt: str, max_tokens: int) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _parse_json_response(text: str) -> dict | list:
    """Strip markdown fences and parse JSON from a Claude response."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        # parts[1] is the block content (possibly starting with 'json\n')
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _summarize_item(client: Any, item: dict) -> dict:
    """Per-item analysis with Claude Haiku."""
    title = item.get("title", "")
    abstract = item.get("abstract", "")
    src = item.get("source_label", item.get("source", ""))
    item_type = item.get("item_type", "publication")

    prompt = (
        "You are a competition policy expert specialising in digital markets and AI regulation. "
        f"Analyse this {item_type} from {src} for its relevance to digital competition policy.\n\n"
        f"Title: {title}\n"
        f"{'Content: ' + abstract[:600] if abstract else ''}\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "summary": 2-3 sentence plain-language summary of the key points\n'
        '  "relevance_score": integer 1-5 (5=critical for digital competition, 1=tangential)\n'
        '  "relevance_explanation": one sentence explaining WHY this matters for digital competition policy\n'
        '  "key_entities": list of up to 5 companies, laws, or markets mentioned\n'
        f'  "themes": list of 1-3 tags chosen from: {THEME_TAGS}\n\n'
        "Return ONLY valid JSON, no prose."
    )

    try:
        text = _call_claude(client, MODEL_SUMMARY, prompt, 512)
        result = _parse_json_response(text)
        assert isinstance(result, dict)
        return result
    except Exception as exc:
        log.debug("Summarise error for '%s': %s", title[:60], exc)
        return {
            "summary": abstract[:200] if abstract else title,
            "relevance_score": 3,
            "relevance_explanation": "Relates to digital competition policy.",
            "key_entities": [],
            "themes": [],
        }


def _synthesize(client: Any, items: list[dict], week_id: str) -> dict:
    """Weekly narrative synthesis with Claude Sonnet."""
    lines: list[str] = []
    for i, it in enumerate(items[:60], 1):
        analysis = it.get("_analysis", {})
        lines.append(
            f"{i}. [{it.get('source', '?').upper()}] {it.get('title', '')}\n"
            f"   Date: {it.get('date', '?')} | Score: {analysis.get('relevance_score', '?')}/5\n"
            f"   Summary: {analysis.get('summary', '')}\n"
            f"   Themes: {', '.join(analysis.get('themes', []))}\n"
            f"   Entities: {', '.join(analysis.get('key_entities', []))}"
        )

    prompt = (
        f"You are a senior competition policy expert specialising in digital markets (week {week_id}).\n"
        f"You have reviewed {len(items)} recent publications.\n\n"
        "Publications:\n" + "\n\n".join(lines) + "\n\n"
        "Generate a JSON digest with these keys:\n\n"
        '"headline": punchy 10-15 word newsletter subject line for the week\n\n'
        '"executive_summary": 3-4 sentence narrative of the most important digital competition '
        "policy developments this week.\n\n"
        '"key_themes": array of up to 5 objects, each with:\n'
        '  "theme": theme name\n'
        '  "description": 2-3 sentences on what is happening in this area\n'
        '  "item_indices": array of 1-based item numbers belonging to this theme\n\n'
        '"connections": array of up to 5 objects describing cross-cutting relationships:\n'
        '  "description": 1-2 sentences on how the publications connect\n'
        '  "item_indices": array of 2-4 item numbers\n'
        '  "connection_type": one of [academic_supports_regulation, case_follows_theory, '
        "regulatory_gap, conflicting_approaches, enforcement_trend, "
        "academic_challenges_regulation]\n\n"
        '"policy_implications": array of up to 4 objects:\n'
        '  "implication": 1-2 sentences on what this means for practitioners/policymakers\n'
        '  "urgency": "immediate" | "medium_term" | "watch"\n\n'
        "Return ONLY valid JSON, no prose."
    )

    try:
        text = _call_claude(client, MODEL_SYNTHESIS, prompt, 2000)
        result = _parse_json_response(text)
        assert isinstance(result, dict)
        return result
    except Exception as exc:
        log.error("Synthesis error: %s", exc)
        return {
            "headline": f"Digital Competition Digest — {week_id}",
            "executive_summary": "Weekly AI synthesis could not be generated. See individual items below.",
            "key_themes": [],
            "connections": [],
            "policy_implications": [],
        }


# ── Week ID ───────────────────────────────────────────────────────────────────

def _week_id(dt: datetime) -> str:
    return f"{dt.year}-W{dt.strftime('%V')}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly competition policy digest generator")
    ap.add_argument("--ecn-data", default="data/ecn_pulse.json")
    ap.add_argument("--research-data", default="data/research_items.json")
    ap.add_argument("--court-data", default="court-cases/data/court_links.json")
    ap.add_argument("--output-dir", default="data/digests")
    ap.add_argument("--lookback", type=int, default=7)
    ap.add_argument("--no-ai", action="store_true", help="Skip AI analysis (free mode)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    items = _collect_items(args.ecn_data, args.research_data, args.court_data, args.lookback)
    log.info(
        "Items collected: %d total (lookback %d days)",
        len(items), args.lookback,
    )

    now = datetime.now(timezone.utc)
    week = _week_id(now)
    os.makedirs(args.output_dir, exist_ok=True)

    if args.no_ai:
        synthesis = {
            "headline": f"Digital Competition Digest — {week}",
            "executive_summary": (
                "AI analysis is disabled (--no-ai mode). "
                f"This digest aggregates {len(items)} items from ECN, arXiv, EUR-Lex, and CJEU "
                f"published in the last {args.lookback} days. "
                "Add ANTHROPIC_API_KEY and remove --no-ai to enable summaries and thematic analysis."
            ),
            "key_themes": [],
            "connections": [],
            "policy_implications": [],
            "ai_enabled": False,
        }
        digest = {
            "week": week,
            "generated_at": now.isoformat(),
            "total_items": len(items),
            "items": items,
            "synthesis": synthesis,
        }
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            log.error(
                "ANTHROPIC_API_KEY environment variable is not set.\n"
                "Run with --no-ai for free mode, or set the variable and re-run."
            )
            sys.exit(1)

        try:
            import anthropic
        except ImportError:
            log.error("anthropic package not installed. Run: pip install anthropic")
            sys.exit(1)

        client = anthropic.Anthropic(api_key=api_key)

        log.info("Analysing %d items with %s …", len(items), MODEL_SUMMARY)
        for idx, item in enumerate(items, 1):
            log.debug("  [%d/%d] %s", idx, len(items), item.get("title", "")[:70])
            item["_analysis"] = _summarize_item(client, item)

        # Only send high-relevance items to the synthesis call
        high = [it for it in items if it.get("_analysis", {}).get("relevance_score", 0) >= 3]
        log.info("High-relevance items: %d / %d", len(high), len(items))

        log.info("Generating weekly synthesis with %s …", MODEL_SYNTHESIS)
        synthesis = _synthesize(client, high, week)
        synthesis["ai_enabled"] = True

        digest = {
            "week": week,
            "generated_at": now.isoformat(),
            "total_items": len(items),
            "high_relevance_items": len(high),
            "items": items,
            "synthesis": synthesis,
        }

    week_file = os.path.join(args.output_dir, f"{week.replace('/', '-')}.json")
    latest_file = os.path.join(args.output_dir, "latest.json")

    for path in (week_file, latest_file):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(digest, fh, ensure_ascii=False, indent=2)
        log.info("Wrote %s", path)

    print(f"Digest ready: {week} ({len(items)} items)", file=sys.stderr)


if __name__ == "__main__":
    main()
