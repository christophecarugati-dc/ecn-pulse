# ECN Pulse — `christophecarugati-dc/ecn-pulse`

A daily-refreshed dashboard of competition-policy activity across the European Competition Network. The scraper pulls press releases from the 27 ECN member NCAs, the European Commission, EFTA Surveillance Authority, and the UK CMA. The output is a JSON file in `data/` and a GitHub Pages dashboard at:

> **https://christophecarugati-dc.github.io/ecn-pulse/**

## Repository layout

```
ecn-pulse/
├── index.html               # GitHub Pages dashboard (the user-facing surface)
├── ecn_scraper.py           # the scraper itself
├── requirements.txt         # python deps (requests, beautifulsoup4, dateutil)
├── data/
│   └── ecn_pulse.json       # latest scraper output, committed by the Action
└── .github/
    └── workflows/
        └── scrape.yml       # GitHub Action: runs Mon-Fri 04:30 UTC
```

## Setup (one-time)

1. **Create the repo** at `github.com/christophecarugati-dc/ecn-pulse`. Public so GitHub Pages and Actions are free.
2. **Push these files** to `main`.
3. **Enable Actions write permissions.** `Settings → Actions → General → Workflow permissions → Read and write permissions → Save.`
4. **Trigger the first run.** `Actions → ECN Pulse scrape → Run workflow → Run workflow.` Takes about a minute.
5. **Enable Pages.** `Settings → Pages → Source: Deploy from a branch → Branch: main → Folder: / (root) → Save.`
6. Wait a minute, then load **https://christophecarugati-dc.github.io/ecn-pulse/**.

From there, the Action runs itself every weekday morning. The dashboard refreshes when you reload it.

## How the dashboard works

`index.html` fetches `data/ecn_pulse.json` from the same origin (no CORS, no auth, no API keys). The data file is a JSON document with a stable schema (see "Output schema" below) so you can build other consumers — analytics, Slack bots, an in-house CMS, a custom-branded white-label dashboard — against the same file.

State is in `localStorage`: which jurisdictions are selected, which category is active, and the last selection of filters. No backend required.

The page is self-contained — no build step, no npm, no React. Edit `index.html` directly and push.

## Running the scraper manually

```
pip install -r requirements.txt
python ecn_scraper.py --output ecn_pulse.json
python ecn_scraper.py --only EU,DE,FR,IT,ES --max 8 --verbose
```

Flags:
- `--output` — JSON path (default `ecn_pulse.json`)
- `--only` — comma-separated authority codes
- `--max` — items per authority (default 12)
- `-v` — verbose logging

## Output schema

```json
{
  "generated_at": "2026-05-13T04:30:00+00:00",
  "total_items": 187,
  "total_errors": 2,
  "items": [
    {
      "authority_code": "DE",
      "authority_name": "Bundeskartellamt",
      "country": "DE",
      "title": "Bundeskartellamt designates Booking.com under Section 19a GWB",
      "url": "https://www.bundeskartellamt.de/...",
      "date": "2026-05-12",
      "snippet": "...",
      "category": "digital",
      "language": "en",
      "source_fetched_at": "2026-05-13T04:30:00+00:00"
    }
  ],
  "errors": [
    { "authority_code": "MT", "error": "no items found with selector ..." }
  ]
}
```

Categories: `merger`, `antitrust`, `cartel`, `state_aid`, `policy`, `digital`, `other`.

## Selectors will need tuning

The CSS selectors in `AUTHORITIES` are starting points from typical NCA page structures; not every NCA will parse correctly on the first run. After the first action run, open `data/ecn_pulse.json` and check `errors`. For each failing authority, open its press-release page in your browser, inspect the news-item structure, and update the `selectors` dict in `ecn_scraper.py`. Common fixes:

- `item` selector wrong → find the parent of each news entry; common patterns are `article`, `.news-item`, `.views-row`.
- `date` selector finds nothing → look for `<time datetime="…">` or a sibling `.meta` / `.date`.
- `title` grabs too much → tighten to the actual headline element.
- `link` is relative and breaks → set `base_url` on the Authority record.

Category inference uses keyword matching on title + snippet. The keyword set in `CATEGORY_KEYWORDS` is English-biased; add native-language keywords for better recall on local-language sources.

## Adding authorities

The `AUTHORITIES` list at the top of `ecn_scraper.py` is the single source of truth. Add a record, give it a parser strategy (`html_list` works for most), and run.

## Roadmap

- [ ] Selector tuning pass after first run for the smaller NCAs (CY, MT, RO, HR, SK)
- [ ] Add EUR-Lex feed for Commission legislative activity (separate parser)
- [ ] Add local-language scraping with Claude/Haiku translation summaries
- [ ] Add deadline detection (consultation closing dates, hearing dates)
- [ ] Custom domain (e.g. `pulse.digital-competition.com`)

## Licence

MIT. Be polite — the scraper rate-limits to one request per second and identifies itself in the User-Agent. Don't crank the schedule below daily.
