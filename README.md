# Composio App Research — AI Product Ops Take-Home

An automated research agent that audits 100 real apps to answer one question:
**can an AI agent build a toolkit for this app today, or does it need a human
to pick up the phone first?**

**Live case study:** [add your deployed link here]
**Full dataset:** [`output/results_publish.json`](./output/results_publish.json)

---

## What this is

For each of 100 apps (CRM, support, ecommerce, fintech, dev infra, and more),
the agent determines:

- **Auth method** — OAuth2, API key, or something else
- **Self-serve vs. gated** — can a developer sign up and get credentials
  themselves, or does it require sales contact / partnership / an existing
  paid account?
- **API surface** — how broad is the documented API?
- **Buildability verdict** — ready to build an agent toolkit today, blocked,
  or genuinely unclear

The full write-up, patterns, and verification results are in the case study
page. This repo is the agent and data behind it.

## How it works

```
search  →  fetch  →  extract  →  cross-check
```

1. **Search** — finds each app's official developer docs (DuckDuckGo,
   no API key needed)
2. **Fetch** — pulls the raw text off the docs page, with a headless-browser
   fallback for JavaScript-rendered sites that return an empty shell on a
   plain fetch
3. **Extract** — an LLM (Groq, free tier) reads the fetched text and fills
   in structured fields, citing evidence for each
4. **Cross-check** — checks Composio's own toolkit catalog as an
   independent second signal

Runs entirely on free-tier APIs — no paid keys required to reproduce it.

## Running it yourself

```bash
cd scripts
pip install -r requirements.txt
playwright install chromium   # needed for the JS-rendering fallback

export GROQ_API_KEY=your_key_here        # free at console.groq.com
export COMPOSIO_API_KEY=your_key_here    # optional, enables the cross-check step

# test on a handful of apps first
python research_agent.py --input ../data/apps.csv --output ../output/results_test.json --limit 5

# full run
python research_agent.py --input ../data/apps.csv --output ../output/results.json
```

## Repo structure

```
scripts/
  research_agent.py       the agent (search -> fetch -> extract -> cross-check)
  requirements.txt

data/
  apps.csv                the 100-app input list

output/
  results_full.json           first raw run - 35/100 apps came back empty
                               (kept intentionally, see "What went wrong" below)
  results_retry.json          re-run of the 35 failed apps after fixing the
                               JS-rendering + ad-link bugs
  results_final_v2.json       after a targeted fix for a self-serve/gated
                               misclassification pattern found in verification
  results_publish.json        final, submitted dataset (100/100 apps resolved
                               except 2 genuinely ambiguous ones)
  verification_report.json    accuracy check, run 1: 60% (20-app sample)
  verification_report_v2.json accuracy check, run 3: 85% (fresh, non-
                               overlapping 20-app sample - the real number)

case_study.html            the deliverable page
```

## What went wrong, and how it was caught

Being upfront about this because it's the more interesting part than the
happy path:

- **First pass produced complete-looking but silently broken data.** 35 apps
  - including some of the best-documented APIs on the internet (Shopify,
  Discord, Notion) - came back with empty extractions. Root cause: their docs
  are JavaScript-rendered single-page apps, and a plain HTTP fetch only sees
  the pre-JS empty HTML shell. Fixed with a headless-browser fallback.
- **A search-result ad link leaked into the data** for one app (Airtable),
  pointing to an unrelated product's paid ad instead of Airtable's real docs.
  Fixed by adding an ad/tracking-domain blocklist to the search step.
- **First verification pass (20-app manual sample) found only 60% accuracy**,
  with two identifiable error patterns: confidently mislabeling gated
  enterprise apps as self-serve (because the docs describe how an *existing*
  customer mints a token, which reads like self-serve to an LLM if it isn't
  also checking the pricing/signup page), and unnecessary hedging into
  "unknown" on apps that were actually determinable.
- **A targeted fix + fresh, non-overlapping verification sample brought
  accuracy to 85%** - the honest, non-circular number, since retesting
  against the same 20 apps the fix was built for would have been circular.
- **That same fresh sample caught a scoping bug**: the fix only re-verified
  apps marked self-serve/unknown, never apps already marked gated. Two of
  those turned out to be wrong (one was a fully open-source CLI tool with no
  auth model at all, incorrectly given an "OAuth2, gated" verdict). Fixed by
  hand in the final dataset.

## Stack

- Python 3
- [DuckDuckGo Search](https://pypi.org/project/ddgs/) - free, no API key
- [Groq](https://console.groq.com) - free-tier LLM inference for extraction
- [Playwright](https://playwright.dev) - headless-browser fallback for
  JS-rendered docs sites
- [Composio](https://composio.dev) toolkit catalog API - independent
  cross-check signal
