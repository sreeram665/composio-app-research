#!/usr/bin/env python3
"""
Composio App Research Agent — Free-Tier Stack
===============================================
Researches a list of apps to determine: category, auth method, self-serve vs
gated access, API surface, existing MCP support, and a buildability verdict
for an AI-agent toolkit.

Stack (all free / no-paid-key-required):
  - Search:      duckduckgo-search (no API key needed at all)
  - Fetch:       requests + BeautifulSoup (raw doc page text)
  - Extraction:  Groq API (free tier) — fast LLM inference for structured
                 extraction from doc text into JSON
  - Cross-check: Composio's own toolkit catalog API (uses your existing key)

Usage:
    export GROQ_API_KEY=your_key_here          # https://console.groq.com
    export COMPOSIO_API_KEY=your_key_here       # optional but recommended
    pip install -r requirements.txt
    python research_agent.py --input data/apps.csv --output output/results.json

requirements.txt:
    requests
    beautifulsoup4
    duckduckgo-search
    python-dotenv
"""

import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
try:
    from ddgs import DDGS          # current package name
except ImportError:                # fall back to the old name if only that is installed
    from duckduckgo_search import DDGS

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")   # llama-3.3 was decommissioned; overridable via env
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
COMPOSIO_API_BASE = "https://backend.composio.dev/api/v3"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AppResult:
    app_name: str
    category: str
    website_hint: str
    one_liner: str = ""
    auth_methods: list = field(default_factory=list)
    self_serve: str = "unknown"              # "self_serve" | "gated" | "unknown"
    gating_evidence: str = ""
    api_surface: str = ""
    has_mcp: Optional[bool] = None
    mcp_evidence: str = ""
    buildability_verdict: str = "unknown"    # "ready" | "blocked" | "partial"
    blocker: str = ""
    composio_toolkit_found: bool = False
    composio_toolkit_slug: Optional[str] = None
    evidence_urls: list = field(default_factory=list)
    raw_extraction_confidence: str = "low"   # set by extraction step
    notes: str = ""


# ---------------------------------------------------------------------------
# Step 1: Search — find the official docs page (free, no key needed)
# ---------------------------------------------------------------------------

# Ad / tracking / redirect hosts that occasionally show up in search results.
# Any candidate URL whose host contains one of these substrings is dropped.
AD_URL_BLOCKLIST = (
    "bing.com/aclick",
    "bing.com/ck/a",
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "google.com/aclk",
    "google.com/url",
    "duckduckgo.com/y.js",
    "adservice.google",
    "facebook.com/tr",
    "amazon-adsystem.com",
    "taboola.com",
    "outbrain.com",
)


def _is_ad_url(url: str) -> bool:
    low = url.lower()
    return any(bad in low for bad in AD_URL_BLOCKLIST)


def find_docs_url(app_name: str, website_hint: str, max_attempts: int = 3) -> list:
    """
    Search for the app's developer/API docs page.
    Returns a short list of candidate URLs, best guess first.
    Retries with exponential backoff if DuckDuckGo returns zero results
    (usually transient rate limiting), and filters out ad/tracking URLs.
    """
    query = f"{app_name} API documentation authentication developer"
    results = []
    for attempt in range(max_attempts):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
        except Exception as e:
            print(f"    [search error for {app_name} (attempt {attempt + 1})]: {e}")
            results = []
        if results:
            break
        if attempt < max_attempts - 1:
            backoff = 3 * (2 ** attempt)   # 3s, 6s, ...
            print(f"    [no results for {app_name}, retrying in {backoff}s]")
            time.sleep(backoff)

    urls = [r["href"] for r in results if r.get("href") and not _is_ad_url(r["href"])]
    # If the known website hint is a direct docs URL, prioritize it
    if website_hint and website_hint.startswith(("http", "docs.", "developer")):
        hint_url = website_hint if website_hint.startswith("http") else f"https://{website_hint}"
        urls.insert(0, hint_url)
    if not urls and website_hint:
        urls.append(website_hint if website_hint.startswith("http") else f"https://{website_hint}")
    return urls[:5]


# ---------------------------------------------------------------------------
# Step 2: Fetch — pull raw text from the docs page
# ---------------------------------------------------------------------------

def _extract_text_from_html(html: str, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:max_chars]


# Below this length, the static HTML is almost certainly a pre-JS empty shell
# (a JavaScript-rendered SPA), so we retry with a real headless browser.
JS_SHELL_THRESHOLD = 500


def _fetch_rendered_text(url: str, max_chars: int) -> str:
    """Render the page in headless Chromium (Playwright) and extract text."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "[playwright not installed: pip install playwright && playwright install chromium]"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent="Mozilla/5.0 (research-agent; educational-project)")
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
            finally:
                browser.close()
        return _extract_text_from_html(html, max_chars)
    except Exception as e:
        return f"[playwright render failed: {e}]"


def fetch_page_text(url: str, max_chars: int = 6000) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (research-agent; educational-project)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = _extract_text_from_html(resp.text, max_chars)
    except Exception as e:
        text = f"[fetch failed: {e}]"

    # If static fetch yielded almost nothing, the page is likely a JS-rendered
    # SPA shell — retry the same URL with a headless browser.
    if text.startswith("[fetch failed") or len(text) < JS_SHELL_THRESHOLD:
        rendered = _fetch_rendered_text(url, max_chars)
        if not rendered.startswith("[playwright") and len(rendered) > len(text):
            return rendered
    return text


# ---------------------------------------------------------------------------
# Step 3: Extract — ask Groq to pull structured fields from the doc text
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are a research analyst extracting structured facts about a
software app's developer API from raw documentation text. Base your answer ONLY on the
text provided. If something is not mentioned, say "not found" rather than guessing.

App: {app_name}
Doc text:
\"\"\"
{doc_text}
\"\"\"

Return ONLY valid JSON (no markdown fences, no preamble) matching this schema:
{{
  "one_liner": "one sentence describing what the app does",
  "auth_methods": ["OAuth2", "API Key", etc — list what's mentioned],
  "self_serve": "self_serve" | "gated" | "unknown",
  "gating_evidence": "short quote or paraphrase supporting the self_serve verdict",
  "api_surface": "short description of API breadth (REST/GraphQL, how broad)",
  "has_mcp": true | false | null,
  "mcp_evidence": "short note if MCP is mentioned, else empty string",
  "confidence": "high" | "medium" | "low"
}}
"""


def extract_fields(app_name: str, doc_text: str) -> dict:
    if not GROQ_API_KEY:
        return {"confidence": "low", "self_serve": "unknown",
                "notes": "GROQ_API_KEY not set — extraction skipped"}

    prompt = EXTRACTION_PROMPT.format(app_name=app_name, doc_text=doc_text[:5000])
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        return {"confidence": "low", "self_serve": "unknown", "notes": f"extraction error: {e}"}


# ---------------------------------------------------------------------------
# Step 4: Cross-check against Composio's own toolkit catalog
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetch_composio_catalog() -> dict:
    if not COMPOSIO_API_KEY:
        return {}
    headers = {"x-api-key": COMPOSIO_API_KEY}
    catalog = {}
    cursor = None
    try:
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(f"{COMPOSIO_API_BASE}/toolkits", headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                catalog[normalize_name(item.get("name", ""))] = item
            cursor = data.get("next_cursor")
            if not cursor:
                break
            time.sleep(0.2)
    except Exception as e:
        print(f"  [Composio catalog fetch failed: {e}]")
    return catalog


def match_composio_toolkit(app_name: str, catalog: dict) -> Optional[dict]:
    key = normalize_name(app_name)
    if key in catalog:
        return catalog[key]
    for cat_key, item in catalog.items():
        if key and (key in cat_key or cat_key in key):
            return item
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def research_app(app_row: dict, composio_catalog: dict) -> AppResult:
    name = app_row["app_name"]
    result = AppResult(app_name=name, category=app_row.get("category", ""),
                        website_hint=app_row.get("website_hint", ""))

    # Composio cross-check (fast, no LLM needed)
    match = match_composio_toolkit(name, composio_catalog)
    if match:
        result.composio_toolkit_found = True
        result.composio_toolkit_slug = match.get("slug")
        result.notes += "Matched in Composio's toolkit catalog. "

    # Search + fetch + extract
    urls = find_docs_url(name, app_row.get("website_hint", ""))
    doc_text = ""
    used_url = ""
    for url in urls:
        text = fetch_page_text(url)
        if text and not text.startswith("[fetch failed"):
            doc_text = text
            used_url = url
            break
        time.sleep(0.3)

    if doc_text:
        result.evidence_urls.append(used_url)
        extracted = extract_fields(name, doc_text)
        result.one_liner = extracted.get("one_liner", "")
        result.auth_methods = extracted.get("auth_methods", [])
        result.self_serve = extracted.get("self_serve", "unknown")
        result.gating_evidence = extracted.get("gating_evidence", "")
        result.api_surface = extracted.get("api_surface", "")
        result.has_mcp = extracted.get("has_mcp")
        result.mcp_evidence = extracted.get("mcp_evidence", "")
        result.raw_extraction_confidence = extracted.get("confidence", "low")
    else:
        result.notes += "Could not fetch any candidate docs page. "

    # Simple buildability heuristic — refine by hand during verification pass
    if result.composio_toolkit_found or result.self_serve == "self_serve":
        result.buildability_verdict = "ready"
    elif result.self_serve == "gated":
        result.buildability_verdict = "blocked"
        result.blocker = "Access appears gated (sales contact / partnership / existing customer)."
    else:
        result.buildability_verdict = "partial"
        result.blocker = "Could not confirm access model from available docs — needs manual check."

    return result


def load_apps(csv_path: str) -> list:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/apps.csv")
    parser.add_argument("--output", default="output/results.json")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N apps (for testing)")
    args = parser.parse_args()

    print("Fetching Composio toolkit catalog...")
    catalog = fetch_composio_catalog()
    print(f"  -> {len(catalog)} toolkits loaded")

    apps = load_apps(args.input)
    if args.limit:
        apps = apps[: args.limit]
    print(f"Researching {len(apps)} apps...")

    results = []
    for i, app_row in enumerate(apps, 1):
        print(f"  [{i}/{len(apps)}] {app_row['app_name']}")
        results.append(asdict(research_app(app_row, catalog)))
        time.sleep(2.5)  # be polite to DDG + Groq rate limits (raised to reduce search rate-limiting)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Done. Wrote {len(results)} results to {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
