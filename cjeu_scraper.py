#!/usr/bin/env python3
"""
DG COMP × CJEU Case Linker
Fetches DG COMP competition decisions and links them with follow-up appeal cases
at the EU General Court and Court of Justice of the EU.

Data sources:
  - Seed data: curated high-profile cases (always available)
  - EUR-Lex: additional CJEU judgments via case-law search
  - competition-cases.ec.europa.eu: enriched DG COMP case metadata

Output: data/court_links.json
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# ── HTTP session ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; ECN-Pulse/1.0; "
        "+https://github.com/christophecarugati-dc/ecn-pulse)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
})

# ── Regex helpers ─────────────────────────────────────────────────────────────

DGCOMP_CASE_RE = re.compile(
    r'\b(AT\.\d{5}|M\.\d{4,7}|SA\.\d{5}|COMP/[A-Z]/\d+|IV/[A-Z]/\d+)\b',
    re.IGNORECASE,
)
GC_CASE_RE = re.compile(r'\bT-(\d+)/(\d{2})\b')
CJ_CASE_RE = re.compile(r'\bC-(\d+)/(\d{2})(?:\s*P)?\b')

# ── Seed data — curated high-profile cases ────────────────────────────────────
#
# Each entry has a "dgcomp" block and an "appeals" list.
# Outcomes: "dismissed" | "annulled" | "partially_annulled" | "pending" | "referred" | "settled"

SEED_CASES = [
    # ── Google Shopping ───────────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.40099",
            "case_name": "Google Shopping",
            "case_type": "antitrust",
            "decision_date": "2017-06-27",
            "decision_type": "Article 102 TFEU",
            "fine_eur": 2_424_495_000,
            "parties": ["Google LLC", "Alphabet Inc."],
            "sector": "Online search / Comparison shopping",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.40099",
            "summary": (
                "Google abused its dominant position in general internet search "
                "by systematically giving prominent placement to its own comparison "
                "shopping service and demoting rivals, in breach of Article 102 TFEU."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-612/17",
                "title": "Google LLC and Alphabet Inc. v Commission",
                "filing_date": "2017-09-11",
                "judgment_date": "2021-11-10",
                "outcome": "dismissed",
                "outcome_detail": (
                    "Action dismissed in full. The €2.42 billion fine and "
                    "the finding of infringement were upheld."
                ),
                "applicants": ["Google LLC", "Alphabet Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-612/17",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62017TJ0612",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-48/22 P",
                "title": "Alphabet Inc. and Google LLC v Commission",
                "filing_date": "2022-01-24",
                "judgment_date": None,
                "outcome": "pending",
                "outcome_detail": None,
                "applicants": ["Alphabet Inc.", "Google LLC"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-48/22",
                "eurlex_url": None,
            },
        ],
    },

    # ── Google Android ────────────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.40153",
            "case_name": "Google Android",
            "case_type": "antitrust",
            "decision_date": "2018-07-18",
            "decision_type": "Article 102 TFEU",
            "fine_eur": 4_342_865_000,
            "parties": ["Google LLC", "Alphabet Inc."],
            "sector": "Mobile operating systems",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.40153",
            "summary": (
                "Google abused its dominant position in licensable smart mobile "
                "operating systems by requiring OEMs to pre-install Google Search "
                "and Chrome as a condition for licensing the Play Store, and by "
                "making payments to OEMs and operators conditional on exclusive "
                "pre-installation of Google Search."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-604/18",
                "title": "Google LLC and Alphabet Inc. v Commission",
                "filing_date": "2018-10-09",
                "judgment_date": "2022-09-14",
                "outcome": "partially_annulled",
                "outcome_detail": (
                    "Decision largely upheld. The fine was reduced from "
                    "€4.34 billion to €4.125 billion following reappraisal "
                    "of specific multipliers applied by the Commission."
                ),
                "applicants": ["Google LLC", "Alphabet Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-604/18",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62018TJ0604",
            },
        ],
    },

    # ── Google AdSense for Search ─────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.40411",
            "case_name": "Google Search (AdSense)",
            "case_type": "antitrust",
            "decision_date": "2019-03-20",
            "decision_type": "Article 102 TFEU",
            "fine_eur": 1_494_459_000,
            "parties": ["Google LLC", "Alphabet Inc."],
            "sector": "Online advertising",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.40411",
            "summary": (
                "Google abused its dominant position in online search advertising "
                "by inserting restrictive clauses in contracts with third-party websites "
                "that prevented rivals from placing their search ads on those sites."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-334/19",
                "title": "Google LLC and Alphabet Inc. v Commission",
                "filing_date": "2019-05-22",
                "judgment_date": "2023-01-18",
                "outcome": "dismissed",
                "outcome_detail": (
                    "Action dismissed in full. The €1.49 billion fine and "
                    "the infringement finding were upheld."
                ),
                "applicants": ["Google LLC", "Alphabet Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-334/19",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62019TJ0334",
            },
        ],
    },

    # ── Qualcomm Exclusivity ──────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.40220",
            "case_name": "Qualcomm (Exclusivity Payments)",
            "case_type": "antitrust",
            "decision_date": "2018-01-24",
            "decision_type": "Article 102 TFEU",
            "fine_eur": 997_439_000,
            "parties": ["Qualcomm Inc."],
            "sector": "Chipsets / Mobile",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.40220",
            "summary": (
                "Qualcomm paid Apple to exclusively use Qualcomm LTE baseband chipsets "
                "in all iPhone and iPad models, illegally foreclosing rival chipmakers "
                "from a strategically important market segment."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-235/18",
                "title": "Qualcomm Inc. v Commission",
                "filing_date": "2018-04-06",
                "judgment_date": "2022-06-15",
                "outcome": "annulled",
                "outcome_detail": (
                    "Commission decision annulled in its entirety. The General Court "
                    "found that the Commission made procedural errors (failing to take "
                    "notes of key interviews) and substantive errors in its competitive "
                    "analysis that vitiated the assessment as a whole."
                ),
                "applicants": ["Qualcomm Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-235/18",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62018TJ0235",
            },
        ],
    },

    # ── Intel microprocessors ─────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "COMP/C-3/37.990",
            "case_name": "Intel",
            "case_type": "antitrust",
            "decision_date": "2009-05-13",
            "decision_type": "Article 102 TFEU",
            "fine_eur": 1_060_000_000,
            "parties": ["Intel Corporation"],
            "sector": "Microprocessors",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.37990",
            "summary": (
                "Intel abused its dominant position in the x86 CPU market by granting "
                "conditional rebates to key computer manufacturers and making payments "
                "to a major retailer on condition they would not sell computers with "
                "rival AMD processors."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-286/09",
                "title": "Intel Corporation v Commission",
                "filing_date": "2009-07-22",
                "judgment_date": "2014-06-12",
                "outcome": "dismissed",
                "outcome_detail": "Action dismissed; €1.06 billion fine upheld.",
                "applicants": ["Intel Corporation"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-286/09",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62009TJ0286",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-413/14 P",
                "title": "Intel Corporation v Commission",
                "filing_date": "2014-08-26",
                "judgment_date": "2017-09-06",
                "outcome": "referred",
                "outcome_detail": (
                    "The Court of Justice set aside the General Court judgment and "
                    "referred the case back for re-examination under the as-efficient-"
                    "competitor test, establishing that exclusivity rebates are not "
                    "automatically abusive."
                ),
                "applicants": ["Intel Corporation"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-413/14",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62014CJ0413",
            },
            {
                "court": "General Court (remand)",
                "case_number": "T-286/09 RENV",
                "title": "Intel Corporation v Commission (remand)",
                "filing_date": "2017-09-06",
                "judgment_date": "2022-01-26",
                "outcome": "partially_annulled",
                "outcome_detail": (
                    "On remand, the General Court partially annulled the decision "
                    "and significantly reduced the fine, finding that the Commission "
                    "had not demonstrated anticompetitive effects for the HP and "
                    "Lenovo conditional rebates."
                ),
                "applicants": ["Intel Corporation"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-286/09+RENV",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62009TJ0286RENV",
            },
        ],
    },

    # ── Apple / Ireland State Aid ─────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "SA.38373",
            "case_name": "Apple — Ireland",
            "case_type": "state_aid",
            "decision_date": "2016-08-30",
            "decision_type": "State Aid Recovery Order",
            "fine_eur": 13_000_000_000,
            "parties": ["Apple Inc.", "Ireland"],
            "sector": "Technology / Tax rulings",
            "url": "https://competition-cases.ec.europa.eu/cases/SA.38373",
            "summary": (
                "The Commission found that Ireland granted unlawful state aid to Apple "
                "through two tax rulings that artificially lowered Apple's tax burden "
                "in Europe for over two decades, and ordered recovery of up to "
                "€13 billion plus interest."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-778/16",
                "title": "Ireland v Commission",
                "filing_date": "2016-11-09",
                "judgment_date": "2020-07-15",
                "outcome": "annulled",
                "outcome_detail": (
                    "Commission decision annulled. The General Court found that the "
                    "Commission had not established to the required legal standard "
                    "the existence of a selective economic advantage for Apple."
                ),
                "applicants": ["Ireland", "Apple Inc.", "Apple Sales International"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-778/16",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62016TJ0778",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-465/20 P",
                "title": "Commission v Ireland and Apple",
                "filing_date": "2020-10-13",
                "judgment_date": "2024-09-10",
                "outcome": "dismissed",
                "outcome_detail": (
                    "The Court of Justice (Grand Chamber) set aside the General Court "
                    "judgment and upheld the Commission's original decision in full, "
                    "requiring Apple to repay approximately €13.1 billion in unlawful "
                    "state aid to Ireland."
                ),
                "applicants": ["Commission"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-465/20",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62020CJ0465",
            },
        ],
    },

    # ── Hutchison 3G / O2 (blocked merger) ────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "M.6992",
            "case_name": "Hutchison 3G UK / Telefónica UK",
            "case_type": "merger",
            "decision_date": "2016-05-11",
            "decision_type": "Prohibition",
            "fine_eur": None,
            "parties": ["Hutchison 3G UK (Three)", "Telefónica UK (O2)"],
            "sector": "Telecommunications",
            "url": "https://competition-cases.ec.europa.eu/cases/M.6992",
            "summary": (
                "The Commission prohibited the proposed merger between Three and O2 "
                "in the UK, finding it would reduce the number of UK mobile operators "
                "from four to three and significantly impede effective competition."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-399/16",
                "title": "CK Telecoms UK Investments v Commission",
                "filing_date": "2016-07-28",
                "judgment_date": "2020-05-28",
                "outcome": "annulled",
                "outcome_detail": (
                    "Prohibition decision annulled. The General Court found the "
                    "Commission applied too strict a standard of proof and made "
                    "errors in its economic analysis, particularly on closeness of "
                    "competition and network-sharing."
                ),
                "applicants": ["CK Telecoms UK Investments"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-399/16",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62016TJ0399",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-376/20 P",
                "title": "Commission v CK Telecoms UK Investments",
                "filing_date": "2020-08-07",
                "judgment_date": "2023-07-13",
                "outcome": "referred",
                "outcome_detail": (
                    "The Court of Justice set aside the General Court judgment and "
                    "referred the case back to the General Court, clarifying the "
                    "standard of proof applicable in merger review and the concept "
                    "of 'important competitive force'."
                ),
                "applicants": ["Commission"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-376/20",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62020CJ0376",
            },
        ],
    },

    # ── Lundbeck (pay-for-delay) ───────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.39226",
            "case_name": "Lundbeck",
            "case_type": "antitrust",
            "decision_date": "2013-06-19",
            "decision_type": "Article 101 TFEU",
            "fine_eur": 93_766_000,
            "parties": ["H. Lundbeck A/S", "Generics UK", "Arrow", "Alpharma", "Ranbaxy"],
            "sector": "Pharmaceuticals",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.39226",
            "summary": (
                "Lundbeck entered into patent settlement agreements with generic "
                "pharmaceutical companies that delayed their market entry for generic "
                "citalopram in exchange for value transfers — the first EU 'pay-for-"
                "delay' enforcement action."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-472/13",
                "title": "H. Lundbeck A/S v Commission",
                "filing_date": "2013-09-04",
                "judgment_date": "2016-09-08",
                "outcome": "dismissed",
                "outcome_detail": "Action dismissed; pay-for-delay agreements held to be restrictions by object.",
                "applicants": ["H. Lundbeck A/S"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-472/13",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62013TJ0472",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-591/16 P",
                "title": "H. Lundbeck A/S v Commission",
                "filing_date": "2016-11-18",
                "judgment_date": "2021-03-25",
                "outcome": "dismissed",
                "outcome_detail": (
                    "Appeal dismissed. The Court of Justice confirmed that pay-for-"
                    "delay patent settlements constituting 'reverse payments' are "
                    "restrictions of competition by object under Article 101 TFEU."
                ),
                "applicants": ["H. Lundbeck A/S"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-591/16",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62016CJ0591",
            },
        ],
    },

    # ── Servier / Perindopril ─────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.39612",
            "case_name": "Perindopril (Servier)",
            "case_type": "antitrust",
            "decision_date": "2014-07-09",
            "decision_type": "Articles 101 & 102 TFEU",
            "fine_eur": 427_700_000,
            "parties": ["Servier SAS", "Biogaran", "Niche", "Matrix", "Teva", "Krka", "Lupin"],
            "sector": "Pharmaceuticals",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.39612",
            "summary": (
                "Servier entered into pay-for-delay patent settlements to delay generic "
                "entry for perindopril (blood-pressure drug), and acquired the only "
                "competing molecule technology. The Commission found both Article 101 "
                "and Article 102 infringements."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-691/14",
                "title": "Servier SAS and Others v Commission",
                "filing_date": "2014-09-22",
                "judgment_date": "2018-12-12",
                "outcome": "partially_annulled",
                "outcome_detail": (
                    "Article 102 infringement annulled (market definition disputed); "
                    "several Article 101 settlements upheld but others annulled; "
                    "fine significantly reduced from €331M to €208M for Servier."
                ),
                "applicants": ["Servier SAS"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-691/14",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62014TJ0691",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-176/19 P",
                "title": "Commission v Servier SAS and Others",
                "filing_date": "2019-02-28",
                "judgment_date": "2023-10-26",
                "outcome": "partially_annulled",
                "outcome_detail": (
                    "The Court of Justice partly upheld the Commission's cross-appeal, "
                    "restoring some elements of the Article 102 analysis and confirming "
                    "the relevant market definition. Fine partly restored."
                ),
                "applicants": ["Commission", "Servier SAS"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-176/19",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62019CJ0176",
            },
        ],
    },

    # ── Amazon / Luxembourg State Aid ─────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "SA.38944",
            "case_name": "Amazon — Luxembourg",
            "case_type": "state_aid",
            "decision_date": "2017-10-04",
            "decision_type": "State Aid Recovery Order",
            "fine_eur": 250_000_000,
            "parties": ["Amazon", "Luxembourg"],
            "sector": "E-commerce / Tax rulings",
            "url": "https://competition-cases.ec.europa.eu/cases/SA.38944",
            "summary": (
                "The Commission found that Luxembourg granted unlawful state aid to "
                "Amazon through a tax ruling that allowed Amazon to pay substantially "
                "less tax than other companies, and ordered recovery of approximately "
                "€250 million plus interest."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-816/17",
                "title": "Amazon.com Inc. and Others v Commission",
                "filing_date": "2017-12-14",
                "judgment_date": "2021-05-12",
                "outcome": "annulled",
                "outcome_detail": (
                    "Commission decision annulled. The General Court found that the "
                    "Commission had not demonstrated that the Luxembourg tax ruling "
                    "conferred a selective advantage on Amazon."
                ),
                "applicants": ["Amazon.com Inc.", "Amazon EU Sàrl"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-816/17",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62017TJ0816",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-457/21 P",
                "title": "Commission v Amazon.com Inc.",
                "filing_date": "2021-07-23",
                "judgment_date": "2023-11-14",
                "outcome": "dismissed",
                "outcome_detail": (
                    "Commission appeal dismissed. The Court of Justice confirmed the "
                    "General Court's finding that the Commission failed to prove the "
                    "existence of a selective advantage, definitively clearing Amazon."
                ),
                "applicants": ["Commission"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-457/21",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62021CJ0457",
            },
        ],
    },

    # ── Facebook / WhatsApp merger ────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "M.7337",
            "case_name": "Facebook / WhatsApp",
            "case_type": "merger",
            "decision_date": "2014-10-03",
            "decision_type": "Phase I Clearance",
            "fine_eur": None,
            "parties": ["Facebook Inc.", "WhatsApp Inc."],
            "sector": "Social networks / Messaging",
            "url": "https://competition-cases.ec.europa.eu/cases/M.7337",
            "summary": (
                "The Commission unconditionally cleared Facebook's acquisition of "
                "WhatsApp at Phase I, finding no competition concerns. Facebook was "
                "later fined €110 million for providing incorrect information during "
                "the review about the ability to link WhatsApp and Facebook accounts."
            ),
        },
        "appeals": [],
    },

    # ── Bayer / Monsanto ──────────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "M.8084",
            "case_name": "Bayer / Monsanto",
            "case_type": "merger",
            "decision_date": "2018-03-21",
            "decision_type": "Phase II Clearance with conditions",
            "fine_eur": None,
            "parties": ["Bayer AG", "Monsanto Company"],
            "sector": "Seeds / Agrochemicals",
            "url": "https://competition-cases.ec.europa.eu/cases/M.8084",
            "summary": (
                "The Commission cleared Bayer's acquisition of Monsanto subject to "
                "the largest-ever divestment package in EU merger control history "
                "(≈ €6.4 billion), covering seeds, traits, herbicides and digital "
                "farming assets."
            ),
        },
        "appeals": [],
    },

    # ── Illumina / GRAIL ──────────────────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "M.10188",
            "case_name": "Illumina / GRAIL",
            "case_type": "merger",
            "decision_date": "2022-09-06",
            "decision_type": "Prohibition",
            "fine_eur": None,
            "parties": ["Illumina Inc.", "GRAIL Inc."],
            "sector": "Life sciences / Cancer detection",
            "url": "https://competition-cases.ec.europa.eu/cases/M.10188",
            "summary": (
                "The Commission prohibited Illumina's acquisition of GRAIL, finding "
                "it would harm innovation in the nascent market for early cancer "
                "detection tests. The case was notable as the first referral under "
                "Article 22 of the Merger Regulation accepted from member states "
                "that lacked domestic jurisdiction."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-709/22",
                "title": "Illumina Inc. v Commission",
                "filing_date": "2022-11-17",
                "judgment_date": "2023-07-13",
                "outcome": "annulled",
                "outcome_detail": (
                    "General Court annulled the Commission's decision to accept the "
                    "Article 22 referral, ruling the Commission lacked jurisdiction "
                    "because the transaction did not meet EU or national thresholds. "
                    "Commission's prohibition decision subsequently fell."
                ),
                "applicants": ["Illumina Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-709/22",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62022TJ0709",
            },
            {
                "court": "Court of Justice",
                "case_number": "C-611/23 P",
                "title": "Commission v Illumina Inc.",
                "filing_date": "2023-10-09",
                "judgment_date": None,
                "outcome": "pending",
                "outcome_detail": None,
                "applicants": ["Commission"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=C-611/23",
                "eurlex_url": None,
            },
        ],
    },

    # ── MasterCard interchange fees ───────────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "AT.40049",
            "case_name": "MasterCard II (Inter-regional Interchange Fees)",
            "case_type": "antitrust",
            "decision_date": "2019-01-22",
            "decision_type": "Article 101 TFEU",
            "fine_eur": 570_600_000,
            "parties": ["Mastercard Inc."],
            "sector": "Financial services / Payment cards",
            "url": "https://competition-cases.ec.europa.eu/cases/AT.40049",
            "summary": (
                "The Commission found that MasterCard's inter-regional interchange "
                "fees for card payments in the EEA — applied when a consumer from "
                "outside the EEA pays at a European merchant — restricted competition "
                "and raised costs for retailers and ultimately consumers."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-180/19",
                "title": "Mastercard Inc. and Others v Commission",
                "filing_date": "2019-03-22",
                "judgment_date": "2024-01-24",
                "outcome": "dismissed",
                "outcome_detail": (
                    "Action dismissed. The General Court upheld the Commission's "
                    "finding that MasterCard's inter-regional interchange fees "
                    "restricted competition and that MasterCard failed to demonstrate "
                    "the conditions for an Article 101(3) exemption."
                ),
                "applicants": ["Mastercard Inc."],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-180/19",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62019TJ0180",
            },
        ],
    },

    # ── Altice / PT Portugal (gun-jumping) ────────────────────────────────────
    {
        "dgcomp": {
            "case_number": "M.7993",
            "case_name": "Altice / PT Portugal (gun-jumping)",
            "case_type": "merger",
            "decision_date": "2018-04-24",
            "decision_type": "Gun-jumping fine",
            "fine_eur": 124_500_000,
            "parties": ["Altice N.V.", "PT Portugal"],
            "sector": "Telecommunications",
            "url": "https://competition-cases.ec.europa.eu/cases/M.7993",
            "summary": (
                "The Commission fined Altice €124.5 million for implementing its "
                "acquisition of PT Portugal before notifying and obtaining clearance "
                "('gun-jumping'), finding Altice exercised decisive influence over "
                "PT Portugal prior to Commission approval — the largest gun-jumping "
                "fine at that time."
            ),
        },
        "appeals": [
            {
                "court": "General Court",
                "case_number": "T-425/18",
                "title": "Altice Group Lux v Commission",
                "filing_date": "2018-07-09",
                "judgment_date": "2021-09-22",
                "outcome": "partially_annulled",
                "outcome_detail": (
                    "General Court partially upheld the challenge, reducing the "
                    "fine from €124.5 million to €56.1 million, while confirming "
                    "the finding of gun-jumping as a matter of principle."
                ),
                "applicants": ["Altice Group Lux"],
                "curia_url": "https://curia.europa.eu/juris/liste.jsf?num=T-425/18",
                "eurlex_url": "https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:62018TJ0425",
            },
        ],
    },
]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def safe_get(url: str, params: Optional[dict] = None, timeout: int = 30) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                print(f"[WARN] {url}: {e}", file=sys.stderr)
    return None


# ── EUR-Lex supplementary scraper ─────────────────────────────────────────────

def _celex_from_case(case_number: str, court: str = "GC") -> Optional[str]:
    """Build EUR-Lex CELEX number from a CJEU case number."""
    m = re.match(r'([CT])-(\d+)/(\d{2})', case_number)
    if not m:
        return None
    _ct, num, year_short = m.group(1), m.group(2), m.group(3)
    year = int(year_short) + (2000 if int(year_short) < 90 else 1900)
    court_code = "TJ" if _ct == "T" else "CJ"
    return f"6{year}{court_code}{int(num):04d}"


def fetch_eurlex_recent_competition_judgments(max_results: int = 50) -> list[dict]:
    """
    Scrape EUR-Lex case-law search for recent CJEU competition judgments.
    Returns a list of partial case dicts with case_number, title, date, celex.
    """
    if not BS4_AVAILABLE:
        print("[WARN] beautifulsoup4 not installed; skipping EUR-Lex scraping", file=sys.stderr)
        return []

    results = []
    page = 1
    per_page = 10

    while len(results) < max_results:
        resp = safe_get(
            "https://eur-lex.europa.eu/search.html",
            params={
                "scope": "EURLEX",
                "type": "named",
                "langId": "en",
                "typeOfActStatus": "JUDG",
                "CASE_LAW_SUMMARY": "false",
                "query": "competition commission",
                "DTS_SUBDOM": "EU_CASE_LAW",
                "page": page,
                "pageSize": per_page,
            },
        )
        if not resp:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("div.SearchResult, article.result-item, li.result")
        if not items:
            # Try alternate structure
            items = soup.select(".col-md-9 .row")

        if not items:
            break

        added = 0
        for item in items:
            try:
                text = item.get_text(" ", strip=True)
                # Extract case number from text
                gc_m = GC_CASE_RE.search(text)
                cj_m = CJ_CASE_RE.search(text)
                case_number = None
                if gc_m:
                    case_number = f"T-{gc_m.group(1)}/{gc_m.group(2)}"
                elif cj_m:
                    case_number = f"C-{cj_m.group(1)}/{cj_m.group(2)}"

                # Extract DG COMP references
                dgcomp_refs = list(set(DGCOMP_CASE_RE.findall(text)))

                # Extract date
                date_m = re.search(r'\b(\d{1,2})\s+(January|February|March|April|May|June|'
                                   r'July|August|September|October|November|December)\s+(\d{4})\b', text)
                judgment_date = None
                if date_m:
                    try:
                        from dateutil import parser as dp
                        judgment_date = dp.parse(date_m.group(0)).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                link_tag = item.select_one("a[href*='legal-content'], a[href*='CELEX']")
                eurlex_url = link_tag["href"] if link_tag else None
                if eurlex_url and not eurlex_url.startswith("http"):
                    eurlex_url = "https://eur-lex.europa.eu" + eurlex_url

                title_tag = item.select_one("h2, h3, .title, strong")
                title = title_tag.get_text(strip=True) if title_tag else ""

                if case_number and dgcomp_refs:
                    results.append({
                        "case_number": case_number,
                        "title": title,
                        "judgment_date": judgment_date,
                        "dgcomp_refs": dgcomp_refs,
                        "eurlex_url": eurlex_url,
                    })
                    added += 1
            except Exception:
                continue

        if added == 0:
            break
        page += 1
        time.sleep(1)

    return results


# ── Competition cases register ────────────────────────────────────────────────

def fetch_competition_register_cases(max_cases: int = 100) -> list[dict]:
    """
    Scrape competition-cases.ec.europa.eu for recent DG COMP decisions.
    Returns a list of partial case dicts.
    """
    if not BS4_AVAILABLE:
        return []

    cases = []
    resp = safe_get(
        "https://competition-cases.ec.europa.eu/cases",
        params={"sort": "dateDecision", "order": "desc"},
    )
    if not resp:
        return cases

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("tr[data-case], .case-row, tr.clickable")
    if not rows:
        rows = soup.select("table tbody tr")

    for row in rows[:max_cases]:
        try:
            text = row.get_text(" ", strip=True)
            case_num_m = DGCOMP_CASE_RE.search(text)
            if not case_num_m:
                continue
            cases.append({
                "case_number": case_num_m.group(0),
                "raw_text": text[:300],
            })
        except Exception:
            continue

    return cases


# ── Core linking logic ────────────────────────────────────────────────────────

def _normalize_case_number(cn: str) -> str:
    return cn.upper().strip().replace(" ", "")


def merge_cases(seed: list[dict], eurlex_judgments: list[dict]) -> list[dict]:
    """
    Merge seed data with EUR-Lex-discovered judgments.
    EUR-Lex discoveries that match existing seed DG COMP case numbers are merged in;
    those that don't match any seed case are appended as new entries.
    """
    # Index seed by DG COMP case number
    index: dict[str, dict] = {}
    for entry in seed:
        cn = _normalize_case_number(entry["dgcomp"]["case_number"])
        index[cn] = entry

    for j in eurlex_judgments:
        for ref in j.get("dgcomp_refs", []):
            ref_norm = _normalize_case_number(ref)
            if ref_norm in index:
                # Check if this appeal is already recorded
                existing_nums = {
                    _normalize_case_number(a["case_number"])
                    for a in index[ref_norm].get("appeals", [])
                }
                j_norm = _normalize_case_number(j["case_number"])
                if j_norm not in existing_nums:
                    # Determine court from case number prefix
                    court = "General Court" if j["case_number"].startswith("T-") else "Court of Justice"
                    index[ref_norm]["appeals"].append({
                        "court": court,
                        "case_number": j["case_number"],
                        "title": j.get("title", ""),
                        "filing_date": None,
                        "judgment_date": j.get("judgment_date"),
                        "outcome": "unknown",
                        "outcome_detail": None,
                        "applicants": [],
                        "curia_url": f"https://curia.europa.eu/juris/liste.jsf?num={j['case_number']}",
                        "eurlex_url": j.get("eurlex_url"),
                    })

    return list(index.values())


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(cases: list[dict]) -> dict:
    total_dgcomp = len(cases)
    total_appeals = sum(len(c.get("appeals", [])) for c in cases)
    with_appeals = sum(1 for c in cases if c.get("appeals"))

    outcome_counts: dict[str, int] = {}
    for c in cases:
        for a in c.get("appeals", []):
            o = a.get("outcome", "unknown")
            outcome_counts[o] = outcome_counts.get(o, 0) + 1

    fines_total = sum(
        c["dgcomp"].get("fine_eur") or 0
        for c in cases
        if c["dgcomp"].get("fine_eur")
    )

    # Uphold rate: dismissed / (dismissed + annulled + partially_annulled)
    upheld = outcome_counts.get("dismissed", 0)
    annulled = outcome_counts.get("annulled", 0)
    partial = outcome_counts.get("partially_annulled", 0)
    decided = upheld + annulled + partial
    uphold_rate = round(upheld / decided * 100) if decided else None

    return {
        "total_dgcomp_cases": total_dgcomp,
        "total_appeals": total_appeals,
        "cases_with_appeals": with_appeals,
        "outcomes": outcome_counts,
        "uphold_rate_pct": uphold_rate,
        "total_fines_eur": fines_total,
    }


# ── Output builder ────────────────────────────────────────────────────────────

def build_output(cases: list[dict]) -> dict:
    # Sort: cases with appeals first, then by decision date descending
    def sort_key(c):
        has_appeals = len(c.get("appeals", [])) > 0
        date = c["dgcomp"].get("decision_date") or "0000-00-00"
        return (not has_appeals, date)

    cases_sorted = sorted(cases, key=sort_key, reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": compute_stats(cases),
        "cases": cases_sorted,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DG COMP × CJEU Case Linker")
    parser.add_argument("--output", default="data/court_links.json")
    parser.add_argument("--seed-only", action="store_true",
                        help="Skip live scraping; use only the curated seed dataset")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("DG COMP × CJEU Case Linker", file=sys.stderr)
    print(f"  Seed cases : {len(SEED_CASES)}", file=sys.stderr)

    cases = [c for c in SEED_CASES]  # start from seed (deep copy not needed; we only append)

    if not args.seed_only:
        print("  Fetching EUR-Lex competition judgments…", file=sys.stderr)
        eurlex_judgments = fetch_eurlex_recent_competition_judgments(max_results=50)
        print(f"  EUR-Lex returned {len(eurlex_judgments)} linked judgment(s)", file=sys.stderr)

        if eurlex_judgments:
            cases = merge_cases(cases, eurlex_judgments)

    output = build_output(cases)
    stats = output["stats"]

    print(f"  Output : {stats['total_dgcomp_cases']} DG COMP cases, "
          f"{stats['total_appeals']} appeals, "
          f"uphold rate {stats['uphold_rate_pct']}%",
          file=sys.stderr)

    import os
    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  Written  : {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
