"""
Weekly Digest Generator — synthesizes competition policy developments from all
data sources into a structured weekly briefing.

In --no-ai mode (free):    aggregates and structures data with no API calls.
With ANTHROPIC_API_KEY:    uses Claude Haiku (summaries) + Sonnet (synthesis).
With MISTRAL_API_KEY:      uses Mistral Small for everything — FREE tier
                           available at console.mistral.ai, no credit card needed.

Priority: ANTHROPIC_API_KEY > MISTRAL_API_KEY > --no-ai

Reads:
  data/ecn_pulse.json              ECN enforcement press releases
  data/research_items.json         arXiv papers + EUR-Lex + CJEU (research_monitor.py)
  court-cases/data/court_links.json CJEU case linker

Output:
  data/digests/YYYY-WNN.json       week-specific snapshot
  data/digests/latest.json         always points to the latest digest

Usage:
  python digest_generator.py --no-ai
  MISTRAL_API_KEY=... python digest_generator.py     # free via Mistral
  ANTHROPIC_API_KEY=... python digest_generator.py   # via Claude
  python digest_generator.py --lookback 7 --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("digest-generator")

# Anthropic models
MODEL_CLAUDE_SUMMARY   = "claude-haiku-4-5-20251001"
MODEL_CLAUDE_SYNTHESIS = "claude-sonnet-4-6"

# Mistral models (free tier available at console.mistral.ai)
MODEL_MISTRAL = "mistral-small-latest"

THEME_TAGS = [
    "antitrust enforcement", "merger control", "DMA/DSA", "AI regulation",
    "platform economics", "market definition", "behavioral economics",
    "court proceedings", "academic research", "data markets",
    "interoperability", "algorithmic pricing", "self-preferencing",
    "gatekeeper obligations", "consumer harm",
]

SOURCE_LABELS = {
    "arxiv": "Academic paper",
    "nber": "NBER Working Paper",
    "ssrn": "SSRN Paper",
    "eurlex": "EU document",
    "cjeu": "Court judgment",
    "cjeu_linker": "Court case",
    "dgcomp": "DG COMP Case",
    "dma_acquisitions": "DMA Acquisition",
    "bruegel": "Bruegel",
    "cerre": "CERRE",
    "cpi": "CPI Antitrust",
}

EURLEX_TYPE_LABELS = {
    "regulation": "EU Regulation",
    "directive": "EU Directive",
    "commission_decision": "Commission Decision",
    "decision": "EU Decision",
    "judgment": "Court Judgment",
    "proposal": "Legislative Proposal",
    "communication": "Commission Communication",
    "opinion": "Advocate General Opinion",
    "working_document": "Commission Staff Document",
    "report": "EU Report",
    "guidelines": "EU Guidelines",
    "document": "EU Document",
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

    # ECN press releases — only digital/tech competition items
    import re as _re

    # Digital/tech companies and markets — any of these in title/snippet = include
    _DIGITAL_TECH_RE = _re.compile(
        r'\b('
        r'Google|Alphabet|Apple|Amazon|Meta(?!\w)|Microsoft|TikTok|ByteDance|'
        r'Samsung|Qualcomm|Intel|NVIDIA|Broadcom|ARM|'
        r'Booking|Airbnb|Uber|Lyft|Deliveroo|Just Eat|'
        r'Spotify|Netflix|Disney\+|YouTube|'
        r'digital market|online platform|app store|search engine|'
        r'e-commerce|online marketplace|cloud computing|cloud service|'
        r'social media|social network|online advertising|digital advertising|'
        r'artificial intelligence|machine learning|generative AI|'
        r'DMA|DSA|gatekeeper|self-preferencing|interoperability|'
        r'market power.*digital|dominant.*platform|platform.*dominant|'
        r'data portability|algorithmic|fintech|edtech|adtech'
        r')\b',
        _re.IGNORECASE,
    )

    # Antitrust/digital enforcement — always include (already filtered by ECN scraper category)
    _ALWAYS_INCLUDE = {"digital", "antitrust"}

    # Mergers/cartels/policy: only if digital/tech sector involved
    _CONDITIONAL_INCLUDE = {"merger", "cartel", "policy"}

    def _ecn_is_relevant(it: dict) -> bool:
        # Skip state aid — almost never digital competition relevant
        if it.get("category") == "state_aid":
            return False
        # Skip non-English items without a clearly recognisable company/topic
        lang = it.get("language", "en")
        cat = it.get("category", "other")
        txt = it.get("title", "") + " " + it.get("snippet", "")

        if cat in _ALWAYS_INCLUDE:
            return True
        if cat in _CONDITIONAL_INCLUDE:
            return bool(_DIGITAL_TECH_RE.search(txt))
        # "other" category: require explicit digital keyword, English only
        if lang != "en":
            return False
        return bool(_DIGITAL_TECH_RE.search(txt))

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
            "source_label": (
                EURLEX_TYPE_LABELS.get(it.get("item_type", "document"), "EU Document")
                if src == "eurlex"
                else SOURCE_LABELS.get(src, src.upper())
            ),
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


# ── Provider-agnostic AI client ───────────────────────────────────────────────

class _AIClient:
    """Thin wrapper so the rest of the code is provider-agnostic."""

    # Gemini free tier: 15 requests/minute → 4-second minimum gap between calls.
    _GEMINI_MIN_INTERVAL = 4.0

    def __init__(self, provider: str, client: Any):
        self.provider = provider   # "anthropic" | "gemini"
        self._client = client
        self._quota_zero = False   # True when free-tier quota is hard-capped at 0
        self._last_call: float = 0.0

    def _rate_limit(self) -> None:
        """Enforce minimum interval between Gemini calls."""
        if self.provider != "gemini":
            return
        gap = self._GEMINI_MIN_INTERVAL - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()

    def _call_mistral(self, prompt: str) -> str:
        if self._quota_zero:
            raise RuntimeError("Mistral quota exhausted — skipping")
        self._rate_limit()
        try:
            resp = self._client.chat.complete(
                model=MODEL_MISTRAL,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            err = str(exc)
            if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                if not self._quota_zero:
                    self._quota_zero = True
                    log.error(
                        "Mistral API quota or rate limit reached. "
                        "Check your usage at https://console.mistral.ai. "
                        "Remaining items will be processed without AI."
                    )
            raise

    def complete(self, prompt: str, max_tokens: int = 1024) -> str:
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=MODEL_CLAUDE_SUMMARY,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        else:  # mistral
            return self._call_mistral(prompt)

    def complete_synthesis(self, prompt: str, max_tokens: int = 2000) -> str:
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=MODEL_CLAUDE_SYNTHESIS,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        else:  # mistral uses same model for both
            return self._call_mistral(prompt)


def _parse_json_response(text: str) -> dict | list:
    """Strip markdown fences and parse JSON from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _build_item_prompt(item: dict) -> str:
    title = item.get("title", "")
    abstract = item.get("abstract", "")
    src = item.get("source_label", item.get("source", ""))
    item_type = item.get("item_type", "publication")
    return (
        "You are a competition policy expert specialising in digital markets and AI regulation. "
        f"Analyse this document (source type: {src}, document type: {item_type}) "
        "for its relevance to digital competition policy.\n\n"
        f"Title: {title}\n"
        f"{'Content: ' + abstract[:600] if abstract else ''}\n\n"
        "CRITICAL RULES:\n"
        "1. A 'Commission Staff Working Document' or 'Impact Assessment' is a preparatory report, NOT a law or court case.\n"
        "2. A 'Regulation' or 'Directive' is EU legislation, NOT a court case.\n"
        "3. A 'Judgment' or 'Order' from a court is case law, NOT legislation.\n"
        "4. An arXiv paper is an academic preprint, NOT a policy decision.\n"
        "5. Write the summary in plain English — define any technical terms (e.g. write "
        "'DMA (the EU's Digital Markets Act, which sets rules for large tech platforms)' "
        "not just 'DMA').\n\n"
        "For technology papers (AI agents, LLMs, recommendation systems, etc.), "
        "assess how they signal changes in market structure or competitive dynamics, "
        "even if they do not use the word 'competition' or 'antitrust'.\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "plain_english_type": what this document is in plain English '
        '(e.g. "Academic research paper", "EU regulatory proposal", "Court judgment", '
        '"Commission preparatory report", "Enforcement decision")\n'
        '  "summary": 2-3 sentence plain-language summary of the key findings. '
        'Define any jargon on first use.\n'
        '  "relevance_score": integer 1-5\n'
        '    5 = directly shapes digital competition policy (enforcement, regulation, market power)\n'
        '    4 = strong market signal (technology disrupting a digital market, revealing competitive dynamics)\n'
        '    3 = useful context (platform economics, industry structure, related market)\n'
        '    2 = tangential\n'
        '    1 = not relevant\n'
        '  "relevance_explanation": one sentence on WHY this matters for digital competition '
        '(e.g. "Shows AI agents can bypass Google Search, threatening its advertising monopoly")\n'
        '  "market_signal": one sentence on what market development this publication signals '
        '(e.g. "AI-powered search agents may erode the search advertising duopoly within 2-3 years")\n'
        '  "key_entities": list of up to 5 companies, laws, markets, or technologies mentioned\n'
        f'  "themes": list of 1-3 tags chosen from: {THEME_TAGS}\n\n'
        "Return ONLY valid JSON, no prose."
    )


def _build_synthesis_prompt(items: list[dict], week_id: str) -> str:
    lines: list[str] = []
    for i, it in enumerate(items[:60], 1):
        a = it.get("_analysis", {})
        plain_type = a.get("plain_english_type", it.get("item_type", "?"))
        lines.append(
            f"{i}. [{plain_type.upper()}] {it.get('title', '')}\n"
            f"   Source: {it.get('source_label', '?')} | Date: {it.get('date', '?')} | Score: {a.get('relevance_score', '?')}/5\n"
            f"   Summary: {a.get('summary', '')}\n"
            f"   Market signal: {a.get('market_signal', '')}\n"
            f"   Themes: {', '.join(a.get('themes', []))}\n"
            f"   Entities: {', '.join(a.get('key_entities', []))}"
        )
    return (
        f"You are a senior competition policy expert specialising in digital markets (week {week_id}).\n"
        f"You have reviewed {len(items)} recent publications.\n\n"
        "IMPORTANT DISTINCTIONS — these items come from different sources:\n"
        "- ACADEMIC PAPERS (arXiv): Research that may take years to influence policy. Mark as 'theory'.\n"
        "- EU REGULATIONS/DIRECTIVES: Binding law already in force or being passed.\n"
        "- COMMISSION DECISIONS/PROPOSALS: Active enforcement or proposed rules, not yet binding.\n"
        "- COMMISSION STAFF DOCUMENTS: Preparatory analysis, not policy itself.\n"
        "- COURT JUDGMENTS: Binding legal rulings that set precedent.\n"
        "Never describe a staff document as a law, or an academic paper as an enforcement action.\n\n"
        "Publications:\n" + "\n\n".join(lines) + "\n\n"
        "Generate a JSON digest. Write in plain English, defining jargon on first use "
        "(e.g. 'DMA (EU Digital Markets Act)' not just 'DMA'). "
        "Be specific — name companies, cases, and amounts where relevant.\n\n"
        '"headline": punchy 10-15 word newsletter subject line capturing the most important story\n\n'
        '"executive_summary": 4-5 sentence narrative. Distinguish between: (1) what regulators/courts '
        "are actually DOING now, (2) what researchers are PREDICTING, and (3) what legislation is PROPOSED. "
        "Explain why each matters for competition practitioners.\n\n"
        '"key_themes": array of up to 5 objects:\n'
        '  "theme": theme name in plain English\n'
        '  "description": 2-3 sentences. State what is happening, what type of evidence supports it '
        '(enforcement action / court ruling / academic finding / legislative proposal), and why it matters.\n'
        '  "item_indices": array of 1-based item numbers\n\n'
        '"connections": array of up to 5 objects linking publications across source types:\n'
        '  "description": 1-2 sentences connecting the dots — e.g. "An arXiv paper [#X] predicts '
        'AI agents will displace search, which aligns with the Commission\'s DMA investigation [#Y]"\n'
        '  "item_indices": array of 2-4 item numbers\n'
        '  "connection_type": one of [academic_supports_regulation, case_follows_theory, '
        "regulatory_gap, conflicting_approaches, enforcement_trend, "
        "academic_challenges_regulation, tech_signals_market_shift]\n\n"
        '"policy_implications": array of up to 5 objects:\n'
        '  "implication": 1-2 sentences for practitioners. Be concrete — name the company, rule, '
        "or market at stake. State whether this is based on actual enforcement, proposed law, or academic research.\n"
        '  "urgency": "immediate" | "medium_term" | "watch"\n\n'
        "Return ONLY valid JSON, no prose."
    )


def _summarize_item(client: _AIClient, item: dict) -> dict:
    abstract = item.get("abstract", "")
    _fallback = {
        "summary": abstract[:200] if abstract else item.get("title", ""),
        "relevance_score": 3,
        "relevance_explanation": "Relates to digital competition policy.",
        "market_signal": "",
        "key_entities": [],
        "themes": [],
    }
    if client._quota_zero:
        return _fallback
    try:
        text = client.complete(_build_item_prompt(item), max_tokens=600)
        result = _parse_json_response(text)
        assert isinstance(result, dict)
        return result
    except Exception as exc:
        if not client._quota_zero:
            log.debug("Summarise error for '%s': %s", item.get("title", "")[:60], exc)
        return _fallback


def _generate_faq(client: _AIClient, items: list[dict], synthesis: dict, week_id: str) -> list[dict]:
    """Generate 6 pre-answered Q&A pairs from high-relevance digest items."""
    if client._quota_zero:
        return []

    lines: list[str] = []
    for i, it in enumerate(items[:40], 1):
        a = it.get("_analysis", {})
        lines.append(
            f"{i}. {it.get('title', '')}\n"
            f"   Source: {it.get('source_label', '?')} | Score: {a.get('relevance_score', '?')}/5\n"
            f"   Summary: {a.get('summary', '')}\n"
            f"   Market signal: {a.get('market_signal', '')}\n"
            f"   Entities: {', '.join(a.get('key_entities', []))}"
        )

    # Pick a jargon term from synthesis themes or common terms in entities
    jargon_candidates: list[str] = []
    for theme in (synthesis.get("key_themes") or []):
        jargon_candidates.append(theme.get("theme", ""))
    for it in items[:20]:
        jargon_candidates.extend(it.get("_analysis", {}).get("key_entities", []))

    jargon_term = "DMA"
    known_jargon = ["DMA", "DSA", "gatekeeper", "self-preferencing", "interoperability",
                    "FRAND", "tying", "bundling", "foreclosure", "market power",
                    "dominant position", "abuse of dominance", "vertical restraints"]
    for term in known_jargon:
        for candidate in jargon_candidates:
            if term.lower() in candidate.lower():
                jargon_term = term
                break
        else:
            continue
        break

    prompt = (
        f"You are a competition policy expert. Week: {week_id}. "
        f"Below are {len(items)} high-relevance items from this week's digest.\n\n"
        + "\n\n".join(lines) + "\n\n"
        "Answer exactly these 6 questions in plain English (define any jargon on first use):\n"
        '1. "What are the 2-3 most important developments this week?"\n'
        '2. "Which companies or markets are most at risk from regulatory action?"\n'
        '3. "What should competition lawyers and economists do right now?"\n'
        '4. "What new AI or technology trends could change competition in digital markets?"\n'
        '5. "What legislation or regulation is being proposed or enforced this week?"\n'
        f'6. "What does {jargon_term} mean?" — explain in plain English for a non-specialist.\n\n'
        'Return a JSON array: [{"question": "...", "answer": "..."}]\n'
        "Each answer should be 3-5 sentences, plain English, concrete (name companies/cases/amounts). "
        "Return ONLY valid JSON, no prose."
    )

    try:
        text = client.complete_synthesis(prompt, max_tokens=2000)
        result = _parse_json_response(text)
        assert isinstance(result, list)
        return result
    except Exception as exc:
        log.error("FAQ generation error: %s", exc)
        return []


def _synthesize(client: _AIClient, items: list[dict], week_id: str) -> dict:
    if client._quota_zero:
        return {
            "headline": f"Digital Competition Digest — {week_id}",
            "executive_summary": (
                f"AI synthesis unavailable: the API quota was exhausted. "
                f"The digest contains {len(items)} items without AI analysis. "
                f"Check your usage at https://console.mistral.ai."
            ),
            "key_themes": [],
            "connections": [],
            "policy_implications": [],
        }
    try:
        text = client.complete_synthesis(_build_synthesis_prompt(items, week_id), max_tokens=2000)
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


def _build_ai_client() -> "_AIClient | None":
    """Detect available API key and return the right client, or None."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if anthropic_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            log.info("Using Anthropic API (Claude %s / %s)", MODEL_CLAUDE_SUMMARY, MODEL_CLAUDE_SYNTHESIS)
            return _AIClient("anthropic", client)
        except ImportError:
            log.error("anthropic package not installed. Run: pip install anthropic")
            return None

    mistral_key = os.environ.get("MISTRAL_API_KEY")
    if mistral_key:
        try:
            from mistralai import Mistral
            client = Mistral(api_key=mistral_key)
            log.info("Using Mistral API (%s) — free tier", MODEL_MISTRAL)
            return _AIClient("mistral", client)
        except ImportError as exc:
            log.error("Failed to import mistralai: %s. Run: pip install mistralai", exc)
            return None
        except Exception as exc:
            log.error("Failed to initialise Mistral client: %s", exc)
            return None

    return None


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
        provider_note = (
            "Set MISTRAL_API_KEY (free — console.mistral.ai) or ANTHROPIC_API_KEY "
            "to enable AI summaries and thematic analysis."
        )
        synthesis = {
            "headline": f"Digital Competition Digest — {week}",
            "executive_summary": (
                f"Free mode: {len(items)} items aggregated from ECN enforcement, arXiv, "
                f"EUR-Lex, and CJEU for the past {args.lookback} days. {provider_note}"
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
        ai_client = _build_ai_client()
        if ai_client is None:
            log.error(
                "No AI API key found or client failed to initialise. "
                "Set MISTRAL_API_KEY (free — console.mistral.ai) or ANTHROPIC_API_KEY, "
                "or run with --no-ai."
            )
            sys.exit(1)

        log.info("Analysing %d items …", len(items))
        for idx, item in enumerate(items, 1):
            log.debug("  [%d/%d] %s", idx, len(items), item.get("title", "")[:70])
            item["_analysis"] = _summarize_item(ai_client, item)

        # Only send high-relevance items to the synthesis call
        high = [it for it in items if it.get("_analysis", {}).get("relevance_score", 0) >= 3]
        log.info("High-relevance items: %d / %d", len(high), len(items))

        log.info("Generating weekly synthesis …")
        synthesis = _synthesize(ai_client, high, week)
        synthesis["ai_enabled"] = True
        synthesis["ai_provider"] = ai_client.provider

        faq: list[dict] = []
        if not ai_client._quota_zero:
            log.info("Generating FAQ …")
            faq_items = [it for it in items if it.get("_analysis", {}).get("relevance_score", 0) >= 4]
            faq = _generate_faq(ai_client, faq_items, synthesis, week)
            log.info("FAQ pairs generated: %d", len(faq))

        digest = {
            "week": week,
            "generated_at": now.isoformat(),
            "total_items": len(items),
            "high_relevance_items": len(high),
            "items": items,
            "synthesis": synthesis,
            "faq": faq,
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
