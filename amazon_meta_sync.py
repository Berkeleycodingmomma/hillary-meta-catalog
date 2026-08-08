#!/usr/bin/env python3
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import platform
import re
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
PUBLIC = ROOT / "public"
OUTPUT = ROOT / "output"
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
CACHE = ROOT / "cache"
LIST_CACHE = CACHE / "lists"
PRODUCT_CACHE = CACHE / "products.json"
RUN_STATE = CACHE / "run_state.json"

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
API_URL = "https://creatorsapi.amazon/catalog/v1/getItems"
MARKETPLACE = "www.amazon.com"

LIST_RE = re.compile(
    r"https?://(?:www\.)?amazon\.com/shop/[^/]+/list/([A-Z0-9]+)",
    re.IGNORECASE,
)
RELATIVE_LIST_RE = re.compile(
    r"/shop/[^/]+/list/([A-Z0-9]+)",
    re.IGNORECASE,
)

RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "images.variants.large",
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.contentInfo",
    "itemInfo.productInfo",
    "itemInfo.externalIds",
    "itemInfo.classifications",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "parentASIN",
]

META_FIELDS = [
    "id",
    "title",
    "description",
    "availability",
    "condition",
    "price",
    "link",
    "image_link",
    "additional_image_link",
    "brand",
    "gtin",
    "mpn",
    "google_product_category",
    "fb_product_category",
    "internal_label",
    "custom_label_0",
    "custom_label_1",
    "custom_label_2",
    "custom_label_3",
    "custom_label_4",
]

REGISTRY_FIELDS = [
    "list_id",
    "stable_meta_label",
    "current_title",
    "fallback_name",
    "idea_list_url",
    "include_in_meta",
    "first_seen",
    "last_seen",
    "last_changed",
    "last_product_count",
    "product_hash",
    "status",
]

TRUTHY = {"yes", "true", "1", "on", "y"}
LIST_CACHE_SCHEMA = 3  # v3 = safe whole-document Idea List product-card extraction
SUSPICIOUS_MEMBERSHIP_RATIO = 0.60
SUSPICIOUS_MEMBERSHIP_MIN_LISTS = 25


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_settings() -> dict[str, Any]:
    path = CONFIG / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()



GENERIC_TITLES = {
    "see all", "view all", "show more", "load more", "more", "idea lists",
    "next page", "previous page", "prev page", "back", "forward",
    "all", "amazon", "amazon.com", "shop", "products", "items"
}


def is_valid_list_title(value: Any) -> bool:
    text = normalize_text(value)
    if not text or len(text) > 180:
        return False
    lowered = text.casefold()
    if lowered in GENERIC_TITLES:
        return False
    if re.fullmatch(r"\d+\s*(items?|posts?)?", lowered):
        return False
    if lowered.startswith("see all") or lowered.startswith("view all"):
        return False
    return True


def clean_list_title(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"\s+\d{1,5}\s+Items\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+\d{1,5}\s+Posts\s*$", "", text, flags=re.IGNORECASE)
    return normalize_text(text)


def parse_title_and_count(text: str) -> tuple[str, int | None]:
    lines = [normalize_text(line) for line in str(text or "").splitlines() if normalize_text(line)]
    count = None
    for line in lines:
        match = re.search(r"\b(\d{1,5})\s+Items\b", line, re.IGNORECASE)
        if match:
            count = int(match.group(1))
            break
    candidates = []
    for line in lines:
        candidate = clean_list_title(line)
        if is_valid_list_title(candidate) and not re.search(r"\b\d{1,5}\s+Items\b", candidate, re.IGNORECASE):
            candidates.append(candidate)
    return (candidates[0] if candidates else "", count)


def atomic_write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def list_cache_path(list_id: str) -> Path:
    return LIST_CACHE / f"{list_id}.json"


def load_list_cache(list_id: str) -> dict[str, Any] | None:
    data = load_json(list_cache_path(list_id), None)
    return data if isinstance(data, dict) else None


def save_list_cache(list_id: str, title: str, asins: set[str], displayed_count: int | None) -> None:
    save_json(
        list_cache_path(list_id),
        {
            "list_id": list_id,
            "cache_schema": LIST_CACHE_SCHEMA,
            "title": title,
            "asins": sorted(asins),
            "displayed_count": displayed_count,
            "scraped_at": now_iso(),
            "product_hash": product_hash(asins),
        },
    )


def cache_is_fresh(cache: dict[str, Any], days: int) -> bool:
    try:
        if int(cache.get("cache_schema") or 0) != LIST_CACHE_SCHEMA:
            return False
        scraped = datetime.fromisoformat(str(cache.get("scraped_at")))
        if scraped.tzinfo is None:
            scraped = scraped.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - scraped.astimezone(timezone.utc) <= timedelta(days=days)
    except Exception:
        return False


def clean_url(url: str, storefront_url: str) -> str:
    text = normalize_text(url)
    match = LIST_RE.search(text)
    if not match:
        relative = RELATIVE_LIST_RE.search(text)
        if relative:
            match_id = relative.group(1).upper()
        else:
            return text
    else:
        match_id = match.group(1).upper()
    base_match = re.match(r"(https?://(?:www\.)?amazon\.com/shop/[^/]+)", storefront_url)
    base = base_match.group(1) if base_match else "https://www.amazon.com/shop/thehillarystyle"
    return f"{base}/list/{match_id}"


def extract_list_id(url: str) -> str:
    match = LIST_RE.search(url or "") or RELATIVE_LIST_RE.search(url or "")
    return match.group(1).upper() if match else ""


def stable_meta_label(list_id: str) -> str:
    return f"IL_{list_id.upper()}"[:100]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    atomic_write_csv(path, rows, fields)


def read_approved(storefront_url: str) -> list[dict[str, str]]:
    rows = []
    for row in read_csv(CONFIG / "approved_lists.csv"):
        if normalize_text(row.get("enabled")).lower() not in TRUTHY:
            continue
        list_id = normalize_text(row.get("list_id")).upper()
        url = clean_url(row.get("idea_list_url", ""), storefront_url)
        if not list_id:
            list_id = extract_list_id(url)
        if list_id and url:
            rows.append(
                {
                    "list_id": list_id,
                    "fallback_name": normalize_text(row.get("fallback_name")) or list_id,
                    "idea_list_url": url,
                }
            )
    return rows


def load_registry(approved: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    path = CONFIG / "idea_list_registry.csv"
    registry: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        list_id = normalize_text(row.get("list_id")).upper()
        if not list_id:
            continue
        normalized = {field: normalize_text(row.get(field)) for field in REGISTRY_FIELDS}
        normalized["list_id"] = list_id
        normalized["stable_meta_label"] = normalized["stable_meta_label"] or stable_meta_label(list_id)
        normalized["include_in_meta"] = "yes"  # Every discovered Idea List is automatically included
        registry[list_id] = normalized

    # Seed/migrate every currently approved list into the registry.
    stamp = now_iso()
    for entry in approved:
        list_id = entry["list_id"]
        existing = registry.get(list_id, {})
        registry[list_id] = {
            "list_id": list_id,
            "stable_meta_label": existing.get("stable_meta_label") or stable_meta_label(list_id),
            "current_title": existing.get("current_title") or entry["fallback_name"],
            "fallback_name": existing.get("fallback_name") or entry["fallback_name"],
            "idea_list_url": existing.get("idea_list_url") or entry["idea_list_url"],
            "include_in_meta": "yes",
            "first_seen": existing.get("first_seen") or stamp,
            "last_seen": existing.get("last_seen") or "",
            "last_changed": existing.get("last_changed") or "",
            "last_product_count": existing.get("last_product_count") or "",
            "product_hash": existing.get("product_hash") or "",
            "status": existing.get("status") or "approved",
        }
    return registry


def save_registry(registry: dict[str, dict[str, str]]) -> None:
    rows = [registry[key] for key in sorted(registry)]
    write_csv(CONFIG / "idea_list_registry.csv", rows, REGISTRY_FIELDS)


def get_title(page, fallback: str) -> str:
    """Return the real Idea List name; never replace it with UI text such as 'See all'."""
    selectors = [
        '[data-testid*="list-title"]',
        '[class*="listTitle"]',
        'main h1',
        'main h2',
        'h1',
        'h2',
    ]
    candidates: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 12)):
                text = clean_list_title(locator.nth(index).inner_text(timeout=700))
                if is_valid_list_title(text):
                    candidates.append(text)
        except Exception:
            pass
    # Browser title often contains the list name even when the visible heading is virtualized.
    try:
        browser_title = clean_list_title(page.title().split(":")[0].split("|")[0])
        if is_valid_list_title(browser_title):
            candidates.append(browser_title)
    except Exception:
        pass
    fallback = clean_list_title(fallback)
    # Prefer a candidate that resembles the known registry title. Otherwise preserve the
    # registry title instead of trusting a generic Amazon control.
    if is_valid_list_title(fallback):
        for candidate in candidates:
            if candidate.casefold() == fallback.casefold() or fallback.casefold() in candidate.casefold() or candidate.casefold() in fallback.casefold():
                return candidate
        return fallback
    return candidates[0] if candidates else fallback

def expected_count(page, fallback_count: int | None = None) -> int | None:
    """Read a count only when it is attached to the current list heading.

    A broad body search previously reused an unrelated 244-item card on almost every list.
    """
    selectors = [
        '[data-testid*="list-title"]',
        '[class*="listTitle"]',
        'main h1',
        'main h2',
        'h1',
        'h2',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 12)):
                node = locator.nth(index)
                texts = [normalize_text(node.inner_text(timeout=500))]
                try:
                    texts.append(normalize_text(node.locator("xpath=..").inner_text(timeout=500)))
                except Exception:
                    pass
                for text in texts:
                    match = re.search(r"\b(\d{1,5})\s+Items\b", text, re.IGNORECASE)
                    if match:
                        return int(match.group(1))
        except Exception:
            pass
    return fallback_count


def _asin_from_href(href: str) -> str:
    """Extract one ASIN from a normal Amazon product URL."""
    href = normalize_text(href)
    for pattern in (
        r"/dp/([A-Z0-9]{10})(?:[/?&#]|$)",
        r"/gp/product/([A-Z0-9]{10})(?:[/?&#]|$)",
        r"/product/([A-Z0-9]{10})(?:[/?&#]|$)",
    ):
        match = re.search(pattern, href, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return ""


def extract_list_asins(page) -> set[str]:
    """Extract ASINs from the current Idea List while excluding recommendation UI.

    Amazon's Influencer storefront does not consistently wrap Idea List cards in a
    semantic ``<main>`` element.  The previous strict extractor therefore returned
    zero products on valid lists.  This version searches the whole document for
    product-card evidence, but rejects navigation, sponsored/recommended sections,
    and anything below an explicit recommendation heading.

    It intentionally supports several Amazon card shapes: normal product links,
    ``data-asin`` attributes, and common product-id data attributes.
    """
    try:
        raw = page.evaluate(
            r"""() => {
                const badHeading = /^(more\s+from|you\s+might\s+also|customers?\s+also|related\s+products?|recommended(?:\s+for\s+you)?|recommendations|inspired\s+by|similar\s+items?|shop\s+more)\b/i;
                const badMeta = /(recommend|carousel|sponsored|similar|related|more-from|more_from|shop-more|shop_more|adplacements?|desktop-dp-sims)/i;
                const asinRe = /^[A-Z0-9]{10}$/i;

                // Recommendation modules are normally rendered after the real list.
                // Find the first clearly labelled recommendation heading and reject
                // product evidence below it.
                let cutoffY = Infinity;
                const headingSelectors = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
                for (const el of Array.from(document.querySelectorAll(headingSelectors))) {
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt && txt.length < 220 && badHeading.test(txt)) {
                        const r = el.getBoundingClientRect();
                        cutoffY = Math.min(cutoffY, r.top + window.scrollY);
                    }
                }

                const isVisibleEnough = (el) => {
                    try {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return st.display !== 'none' && st.visibility !== 'hidden' && r.width >= 1 && r.height >= 1;
                    } catch (_) {
                        return true;
                    }
                };

                const isBlocked = (el) => {
                    let node = el;
                    for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
                        const tag = (node.tagName || '').toUpperCase();
                        if (['HEADER', 'NAV', 'FOOTER', 'ASIDE'].includes(tag)) return true;

                        const meta = [
                            node.id || '',
                            typeof node.className === 'string' ? node.className : '',
                            node.getAttribute && node.getAttribute('data-testid') || '',
                            node.getAttribute && node.getAttribute('data-component-type') || '',
                            node.getAttribute && node.getAttribute('data-cel-widget') || '',
                            node.getAttribute && node.getAttribute('aria-label') || ''
                        ].join(' ');
                        if (badMeta.test(meta)) return true;

                        const txt = (node.innerText || '').trim();
                        if (txt && txt.length < 260 && badHeading.test(txt)) return true;
                    }
                    return false;
                };

                const beforeCutoff = (el) => {
                    try {
                        const r = el.getBoundingClientRect();
                        return (r.top + window.scrollY) < cutoffY;
                    } catch (_) {
                        return true;
                    }
                };

                const values = [];
                const push = (value) => {
                    const v = String(value || '').trim();
                    if (v) values.push(v);
                };

                // 1) Product links.  Search the entire document instead of `main`,
                // because current Influencer list pages often omit a semantic main tag.
                const anchors = Array.from(document.querySelectorAll(
                    'a[href*="/dp/"], a[href*="/gp/product/"], a[href*="/product/"]'
                ));
                for (const a of anchors) {
                    if (!isVisibleEnough(a) || !beforeCutoff(a) || isBlocked(a)) continue;
                    push(a.getAttribute('href'));
                }

                // 2) Product-card attributes. Some Amazon list layouts expose the ASIN
                // only as a data attribute and use JavaScript navigation instead of a
                // conventional /dp/ link.
                const attrSelectors = [
                    '[data-asin]', '[data-product-asin]', '[data-productasin]',
                    '[data-product-id]', '[data-productid]', '[data-item-id]'
                ].join(',');
                for (const el of Array.from(document.querySelectorAll(attrSelectors))) {
                    if (!isVisibleEnough(el) || !beforeCutoff(el) || isBlocked(el)) continue;
                    for (const name of ['data-asin','data-product-asin','data-productasin','data-product-id','data-productid','data-item-id']) {
                        const value = el.getAttribute && el.getAttribute(name);
                        if (value && asinRe.test(value.trim())) push(value.trim().toUpperCase());
                    }
                }

                return values;
            }"""
        )
    except Exception:
        return set()

    found: set[str] = set()
    for value in raw or []:
        text = normalize_text(value)
        if re.fullmatch(r"[A-Z0-9]{10}", text, re.IGNORECASE):
            found.add(text.upper())
            continue
        asin = _asin_from_href(text)
        if asin:
            found.add(asin)
    return found

def extract_list_links_from_html(html: str, storefront_url: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(r"(?:https?://(?:www\.)?amazon\.com)?(/shop/[^/\"'<>]+/list/([A-Z0-9]+))", html or "", re.IGNORECASE):
        list_id = match.group(2).upper()
        url = clean_url(urljoin("https://www.amazon.com", match.group(1)), storefront_url)
        found[list_id] = url
    return found


def collect_list_links(page, storefront_url: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    try:
        for list_id, url in extract_list_links_from_html(page.content(), storefront_url).items():
            found[list_id] = {"list_id": list_id, "idea_list_url": url, "amazon_title": "", "displayed_count": ""}
    except Exception:
        pass

    try:
        anchors = page.locator('a[href*="/shop/"][href*="/list/"]')
        count = min(anchors.count(), 2500)
        for index in range(count):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            url = clean_url(urljoin(page.url, href), storefront_url)
            list_id = extract_list_id(url)
            if not list_id:
                continue
            samples: list[str] = []
            try:
                samples.append(anchor.inner_text(timeout=250))
            except Exception:
                pass
            # The link itself often says only 'See all'. Walk upward until the containing
            # Idea List card provides the real title and item count.
            for depth in range(1, 7):
                try:
                    parent = anchor.locator("xpath=" + "/.." * depth)
                    text = parent.inner_text(timeout=250)
                    if text and len(text) < 1200:
                        samples.append(text)
                except Exception:
                    pass
            title = ""
            displayed_count: int | None = None
            for sample in samples:
                parsed_title, parsed_count = parse_title_and_count(sample)
                if not title and is_valid_list_title(parsed_title):
                    title = parsed_title
                if displayed_count is None and parsed_count is not None:
                    displayed_count = parsed_count
                if title and displayed_count is not None:
                    break
            previous = found.get(list_id, {})
            found[list_id] = {
                "list_id": list_id,
                "idea_list_url": url,
                "amazon_title": title or previous.get("amazon_title", ""),
                "displayed_count": str(displayed_count or previous.get("displayed_count") or ""),
            }
    except Exception:
        pass
    return found

def open_idea_lists_view(page) -> None:
    """Open the storefront's dedicated Idea Lists view without following other Amazon content."""
    patterns = [
        re.compile(r"^idea\s*lists?$", re.IGNORECASE),
        re.compile(r"idea\s*lists?", re.IGNORECASE),
    ]
    for role in ("button", "link", "tab"):
        for pattern in patterns:
            try:
                locator = page.get_by_role(role, name=pattern)
                total = min(locator.count(), 10)
                for index in range(total):
                    node = locator.nth(index)
                    if node.is_visible():
                        node.click(timeout=2500)
                        page.wait_for_timeout(1500)
                        return
            except Exception:
                pass

    # Fallback for storefront layouts where the tab is not exposed with an ARIA role.
    try:
        locator = page.locator(r"text=/^Idea\s*Lists?$/i")
        total = min(locator.count(), 10)
        for index in range(total):
            node = locator.nth(index)
            if node.is_visible():
                node.click(timeout=2500)
                page.wait_for_timeout(1500)
                return
    except Exception:
        pass

    raise RuntimeError("Could not find the Idea Lists tab on the storefront.")


def click_list_view_load_more(page) -> bool:
    """Click only controls that reveal more cards on the current Idea Lists view."""
    clicked = False
    patterns = [
        re.compile(r"^see\s+all$", re.IGNORECASE),
        re.compile(r"^view\s+all$", re.IGNORECASE),
        re.compile(r"^show\s+more$", re.IGNORECASE),
        re.compile(r"^load\s+more$", re.IGNORECASE),
    ]
    for role in ("button", "link"):
        for pattern in patterns:
            try:
                locator = page.get_by_role(role, name=pattern)
                total = min(locator.count(), 5)
                for index in range(total):
                    node = locator.nth(index)
                    if node.is_visible():
                        node.click(timeout=1500)
                        page.wait_for_timeout(900)
                        clicked = True
            except Exception:
                pass
    return clicked


def discover_all_lists(context, settings: dict[str, Any], approved: list[dict[str, str]]) -> list[dict[str, str]]:
    """Discover Idea Lists only from the storefront's dedicated Idea Lists tab.

    This deliberately does not recursively open list pages or follow unrelated Amazon links.
    """
    storefront_url = settings["storefront_url"]
    max_scroll_steps = int(settings.get("discovery_scroll_steps", 180))
    discovered: dict[str, dict[str, str]] = {}
    page = context.new_page()

    print("\nScanning the storefront Idea Lists tab only...")
    try:
        page.goto(storefront_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1800)
        open_idea_lists_view(page)

        no_new = 0
        for step in range(1, max_scroll_steps + 1):
            before = len(discovered)
            for list_id, entry in collect_list_links(page, storefront_url).items():
                if list_id not in discovered or discovered[list_id].get("amazon_title") == list_id:
                    discovered[list_id] = entry

            no_new = no_new + 1 if len(discovered) == before else 0
            if step == 1 or len(discovered) != before or step % 20 == 0:
                print(f"  Idea List discovery step {step}: {len(discovered)} lists found")

            try:
                y, max_y = page.evaluate(
                    """() => {
                        const h = window.innerHeight || 900;
                        window.scrollBy(0, Math.max(650, Math.floor(h * 0.85)));
                        const maxY = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - h;
                        return [window.scrollY, maxY];
                    }"""
                )
                page.wait_for_timeout(550)
                bottom = int(y) >= int(max_y) - 60
            except Exception:
                bottom = False

            if no_new in {5, 10}:
                click_list_view_load_more(page)
            if (bottom and no_new >= 8) or no_new >= 16:
                break
    finally:
        page.close()

    # Approved lists must remain known even if Amazon temporarily hides them from the tab.
    for entry in approved:
        discovered.setdefault(
            entry["list_id"],
            {
                "list_id": entry["list_id"],
                "idea_list_url": entry["idea_list_url"],
                "amazon_title": entry["fallback_name"],
                "displayed_count": "",
            },
        )

    print(f"Idea List discovery complete: {len(discovered)} lists found.")
    return [discovered[key] for key in sorted(discovered)]

def scrape_list(
    context,
    entry: dict[str, str],
    quiet: bool = False,
    *,
    force_refresh: bool = False,
    cache_days: int = 7,
) -> tuple[str, set[str], int | None, bool]:
    """Collect one list with checkpointing and fast incremental reuse.

    Returns title, ASINs, trusted displayed count and whether the browser was used.
    """
    list_id = entry["list_id"]
    fallback = entry.get("current_title") or entry.get("fallback_name") or list_id
    card_count = None
    try:
        card_count = int(entry.get("displayed_count") or 0) or None
    except Exception:
        card_count = None
    cached = load_list_cache(list_id)
    if cached and not force_refresh and cache_is_fresh(cached, cache_days):
        cached_asins = {normalize_text(value).upper() for value in cached.get("asins", []) if normalize_text(value)}
        cached_count = cached.get("displayed_count") or card_count
        # Re-scrape only when Amazon's current card count disagrees with the checkpoint.
        if not card_count or card_count == len(cached_asins) or card_count == cached_count:
            title = clean_list_title(cached.get("title")) or fallback
            if not quiet:
                print(f"Using checkpoint for {title}: {len(cached_asins)} products")
            return title, cached_asins, int(cached_count) if str(cached_count or "").isdigit() else card_count, False

    page = context.new_page()
    try:
        if not quiet:
            print(f"\nOpening {fallback}")
        page.goto(entry["idea_list_url"], wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(1300)
        title = get_title(page, fallback)
        expected = expected_count(page, card_count)
        if not quiet:
            print(f"Amazon title: {title}")
            if expected:
                print(f"Amazon displays {expected} items.")

        found: set[str] = set()
        stable_rounds = 0
        last_height = -1
        last_count = -1
        max_steps = 260
        for step in range(1, max_steps + 1):
            try:
                found.update(extract_list_asins(page))
            except Exception:
                pass
            if expected and len(found) >= expected:
                break

            # Fast wheel scrolling triggers Amazon's lazy loader without an 850ms wait
            # on every movement. Pause longer only after repeated stable bottom passes.
            try:
                metrics = page.evaluate(
                    """() => {
                        const root = document.scrollingElement || document.documentElement;
                        const h = window.innerHeight || 900;
                        const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                        const y = root.scrollTop || window.scrollY || 0;
                        return {y, h, height};
                    }"""
                )
            except Exception:
                metrics = {"y": 0, "h": 900, "height": last_height}
            current_count = len(found)
            current_height = int(metrics.get("height") or 0)
            at_bottom = int(metrics.get("y") or 0) + int(metrics.get("h") or 900) >= current_height - 100
            unchanged = current_count == last_count and current_height == last_height
            stable_rounds = stable_rounds + 1 if unchanged and at_bottom else 0
            if not quiet and (step == 1 or current_count != last_count or step % 30 == 0):
                print(f"  Step {step}: {current_count} unique ASINs")

            # Click only genuine product-list expansion controls, never the storefront's
            # generic 'See all' navigation links.
            for pattern in (r"^show more$", r"^load more$", r"^view more$"):
                try:
                    locator = page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
                    for idx in range(min(locator.count(), 3)):
                        node = locator.nth(idx)
                        if node.is_visible(timeout=100):
                            node.click(timeout=700)
                except Exception:
                    pass
            try:
                page.mouse.wheel(0, max(1100, int(metrics.get("h") or 900)))
                page.wait_for_timeout(220 if stable_rounds < 3 else 700)
                if step % 18 == 0:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
            except Exception:
                pass
            last_count = current_count
            last_height = current_height
            if stable_rounds >= 8:
                break

        # A compact recovery sweep is safer than the old 120 x 500ms sweep.
        if expected and len(found) < expected:
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(350)
                for _ in range(50):
                    found.update(extract_list_asins(page))
                    if len(found) >= expected:
                        break
                    page.mouse.wheel(0, 1300)
                    page.wait_for_timeout(180)
            except Exception:
                pass

        save_list_cache(list_id, title, found, expected)
        if not quiet:
            suffix = f" — Amazon shows {expected}" if expected else ""
            print(f"Finished {title}: {len(found)} unique ASINs{suffix}")
        return title, found, expected, True
    finally:
        page.close()

def product_hash(asins: set[str]) -> str:
    joined = "\n".join(sorted(asins)).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def update_registry_from_discovery(
    registry: dict[str, dict[str, str]],
    discovered: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    stamp = now_iso()
    new_rows: list[dict[str, str]] = []
    renamed_rows: list[dict[str, str]] = []
    for entry in discovered:
        list_id = entry["list_id"]
        title = clean_list_title(entry.get("amazon_title"))
        if not is_valid_list_title(title):
            title = clean_list_title(registry.get(list_id, {}).get("current_title")) or clean_list_title(registry.get(list_id, {}).get("fallback_name")) or list_id
        url = entry["idea_list_url"]
        if list_id not in registry:
            registry[list_id] = {
                "list_id": list_id,
                "stable_meta_label": stable_meta_label(list_id),
                "current_title": title,
                "fallback_name": title,
                "idea_list_url": url,
                "include_in_meta": "yes",
                "first_seen": stamp,
                "last_seen": stamp,
                "last_changed": "",
                "last_product_count": "",
                "product_hash": "",
                "status": "new_auto_added",
            }
            new_rows.append(
                {
                    "list_id": list_id,
                    "current_title": title,
                    "idea_list_url": url,
                    "include_in_meta": "yes",
                    "action": "Automatically included in the Meta catalog.",
                }
            )
            continue

        record = registry[list_id]
        record["include_in_meta"] = "yes"
        old_title = normalize_text(record.get("current_title"))
        record["last_seen"] = stamp
        record["idea_list_url"] = url
        if title and title != list_id and old_title and old_title != title:
            renamed_rows.append(
                {
                    "list_id": list_id,
                    "old_title": old_title,
                    "new_title": title,
                    "stable_meta_label": record["stable_meta_label"],
                    "include_in_meta": record["include_in_meta"],
                }
            )
            record["current_title"] = title
            record["status"] = "renamed"
        elif title and title != list_id:
            record["current_title"] = title
    return new_rows, renamed_rows


def registry_entry(record: dict[str, str], discovered_entry: dict[str, str] | None = None) -> dict[str, str]:
    discovered_entry = discovered_entry or {}
    return {
        "list_id": record["list_id"],
        "fallback_name": record.get("fallback_name") or record.get("current_title") or record["list_id"],
        "current_title": record.get("current_title") or record.get("fallback_name") or record["list_id"],
        "idea_list_url": record["idea_list_url"],
        "stable_meta_label": record.get("stable_meta_label") or stable_meta_label(record["list_id"]),
        "displayed_count": discovered_entry.get("displayed_count", ""),
    }


def chunks(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def nested(obj: Any, *keys: str, default: Any = None) -> Any:
    current = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def display_value(obj: Any) -> str:
    return obj.get("displayValue", "").strip() if isinstance(obj, dict) and isinstance(obj.get("displayValue"), str) else ""


def display_values(obj: Any) -> list[str]:
    if isinstance(obj, dict) and isinstance(obj.get("displayValues"), list):
        return [normalize_text(value) for value in obj["displayValues"] if normalize_text(value)]
    return []


def token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/json"},
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "creatorsapi::default",
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Token request failed ({response.status_code}): {response.text[:1000]}")
    access_token = response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Amazon returned no access token.")
    return access_token


def get_items(access_token: str, partner_tag: str, asins: list[str]):
    response = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "x-marketplace": MARKETPLACE,
        },
        json={
            "itemIds": asins,
            "itemIdType": "ASIN",
            "marketplace": MARKETPLACE,
            "partnerTag": partner_tag,
            "resources": RESOURCES,
        },
        timeout=(20, 90),
    )
    if response.status_code == 429:
        raise RuntimeError("Amazon rate limit reached")
    if not response.ok:
        raise RuntimeError(f"GetItems failed ({response.status_code}): {response.text[:1500]}")
    data = response.json()
    return (data.get("itemsResult") or {}).get("items") or [], data.get("errors") or []


def get_items_with_retry(
    access_token: str,
    partner_tag: str,
    asins: list[str],
    *,
    max_attempts: int = 7,
):
    """Call GetItems without aborting the full run on a temporary network reset."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return get_items(access_token, partner_tag, asins)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            wait = min(60, 4 * (2 ** (attempt - 1)))
            print(
                f"Temporary Amazon connection problem. "
                f"Retrying this batch in {wait} seconds "
                f"({attempt}/{max_attempts})..."
            )
            time.sleep(wait)
        except RuntimeError as exc:
            last_error = exc
            message = str(exc).lower()
            if "rate limit" in message or "failed (500)" in message or "failed (502)" in message or "failed (503)" in message or "failed (504)" in message:
                wait = min(75, 10 * attempt)
                print(
                    f"Amazon API temporarily unavailable or rate limited. "
                    f"Retrying this batch in {wait} seconds "
                    f"({attempt}/{max_attempts})..."
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(
        f"Amazon API batch failed after {max_attempts} attempts: {last_error}"
    )


def parse_offer(item: dict[str, Any]) -> tuple[str, str]:
    listings = (item.get("offersV2") or {}).get("listings") or []
    if not listings:
        return "", "out of stock"
    listing = listings[0] or {}
    price_data = listing.get("price") or {}
    money = price_data.get("money") or price_data.get("currentPrice") or price_data
    amount = money.get("amount") if isinstance(money, dict) else None
    currency = money.get("currency") if isinstance(money, dict) else None
    if amount is not None:
        try:
            price = f"{float(amount):.2f} {(currency or 'USD').upper()}"
        except (TypeError, ValueError):
            price = ""
    else:
        price = ""
    availability_text = json.dumps(listing.get("availability") or {}).lower()
    availability = "in stock" if any(value in availability_text for value in ["in_stock", "instock", "available", "now"]) else "out of stock"
    return price, availability


def brand(item: dict[str, Any]) -> str:
    byline = nested(item, "itemInfo", "byLineInfo", default={}) or {}
    for key in ("brand", "manufacturer", "contributors"):
        value = display_value(byline.get(key))
        if value:
            return value
    return "Amazon"


def description(item: dict[str, Any]) -> str:
    values = display_values(nested(item, "itemInfo", "features", default={}))
    if values:
        return " • ".join(values)[:4999]
    for value in (nested(item, "itemInfo", "contentInfo", default={}) or {}).values():
        values = display_values(value)
        if values:
            return " • ".join(values)[:4999]
        text = display_value(value)
        if text:
            return text[:4999]
    return display_value(nested(item, "itemInfo", "title", default={}))[:4999]


def image(item: dict[str, Any]) -> str:
    return nested(item, "images", "primary", "large", "url") or nested(item, "images", "primary", "medium", "url") or ""


def extra_images(item: dict[str, Any]) -> str:
    urls: list[str] = []
    for variant in nested(item, "images", "variants", default=[]) or []:
        url = nested(variant, "large", "url") or nested(variant, "medium", "url") or nested(variant, "small", "url")
        if url and url not in urls:
            urls.append(url)
    return ",".join(urls[:20])


def format_internal_labels(readable_titles: list[str]) -> str:
    """Format all Idea List names for Meta's multi-value internal_label field."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in readable_titles:
        label = normalize_text(value)[:110]
        if not label or label.lower() in {"see all", "view all", "show more", "load more", "next page", "previous page", "prev page"}:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label)
    escaped = [label.replace("\\", "\\\\").replace("'", "\\'") for label in cleaned]
    return "[" + ",".join(f"'{label}'" for label in escaped) + "]"


def meta_row(
    item: dict[str, Any],
    stable_labels: list[str],
    readable_titles: list[str],
) -> dict[str, str]:
    asin = normalize_text(item.get("asin")).upper()
    title = display_value(nested(item, "itemInfo", "title", default={})) or asin
    price, availability = parse_offer(item)
    external = nested(item, "itemInfo", "externalIds", default={}) or {}
    gtin = ""
    for key in ("upcs", "eans", "isbns"):
        values = display_values(external.get(key))
        if values:
            gtin = values[0]
            break
    labels = [normalize_text(value)[:100] for value in stable_labels if normalize_text(value)][:5]
    labels.extend([""] * (5 - len(labels)))
    return {
        "id": asin,
        "title": title[:200],
        "description": description(item),
        "availability": availability,
        "condition": "new",
        "price": price,
        "link": item.get("detailPageURL") or f"https://www.amazon.com/dp/{asin}",
        "image_link": image(item),
        "additional_image_link": extra_images(item),
        "brand": brand(item)[:100],
        "gtin": gtin,
        "mpn": asin,
        "google_product_category": "",
        "fb_product_category": "",
        "internal_label": format_internal_labels(readable_titles),
        "custom_label_0": labels[0],
        "custom_label_1": labels[1],
        "custom_label_2": labels[2],
        "custom_label_3": labels[3],
        "custom_label_4": labels[4],
    }


def refresh_cached_row_labels(
    row: dict[str, str],
    stable_labels: list[str],
    readable_titles: list[str],
) -> dict[str, str]:
    updated = {field: normalize_text(row.get(field)) for field in META_FIELDS}
    labels = [normalize_text(value)[:100] for value in stable_labels if normalize_text(value)][:5]
    labels.extend([""] * (5 - len(labels)))
    updated["internal_label"] = format_internal_labels(readable_titles)
    for index in range(5):
        updated[f"custom_label_{index}"] = labels[index]
    return updated


def publish(settings: dict[str, Any]) -> str:
    if not settings.get("auto_git_publish", False):
        return "Git publishing is OFF. Verify the new registry and labels first, then enable it in config/settings.json."
    paths = ["public", "reports"]
    if settings.get("publish_registry_to_git", False):
        paths.append("config/idea_list_registry.csv")
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", *paths], check=True)
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not status:
            return "No GitHub changes to publish."
        message = time.strftime("Update Meta catalog %Y-%m-%d %H:%M")
        subprocess.run(["git", "-C", str(ROOT), "commit", "-m", message], check=True)
        subprocess.run(["git", "-C", str(ROOT), "push", "origin", settings.get("github_branch", "main")], check=True)
        return "Published updated feed to GitHub."
    except Exception as exc:
        return f"Git publishing failed: {exc}"


def maybe_open_report(settings: dict[str, Any], path: Path) -> None:
    if not settings.get("open_review_report_after_run", True) or not path.exists():
        return
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif platform.system() == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", str(path)])
    except Exception:
        pass


def main() -> int:
    for directory in (CONFIG, PUBLIC, OUTPUT, REPORTS, LOGS, CACHE, LIST_CACHE):
        directory.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    storefront_url = settings["storefront_url"]
    approved = read_approved(storefront_url)
    registry = load_registry(approved)

    partner_tag = input(f"Amazon Store/Partner Tag [{settings.get('partner_tag', 'hillarypeil-20')}]: ").strip() or settings.get("partner_tag", "hillarypeil-20")
    client_id = input("Creators API Credential ID: ").strip()
    client_secret = getpass.getpass("Creators API Secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("Credential ID and secret are required.")
        return 1

    memberships_titles: dict[str, set[str]] = defaultdict(set)
    memberships_keys: dict[str, set[str]] = defaultdict(set)
    resolved: list[dict[str, Any]] = []
    changed_unapproved: list[dict[str, Any]] = []
    all_discovered: list[dict[str, str]] = []
    new_lists: list[dict[str, str]] = []
    renamed_lists: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=bool(settings.get("headless", True)))
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US")

        if settings.get("discover_storefront_lists", True):
            all_discovered = discover_all_lists(context, settings, approved)
            new_lists, renamed_lists = update_registry_from_discovery(registry, all_discovered)

        discovered_by_id = {entry["list_id"]: entry for entry in all_discovered}
        approved_records = [record for record in registry.values() if record.get("idea_list_url")]
        approved_records.sort(key=lambda record: (normalize_text(record.get("current_title")).lower(), record["list_id"]))

        print(f"\nAll discovered Idea Lists to publish: {len(approved_records)}")
        for record in approved_records:
            entry = registry_entry(record, discovered_by_id.get(record["list_id"]))
            try:
                title, asins, expected, used_browser = scrape_list(
                    context,
                    entry,
                    force_refresh=bool(settings.get("force_full_list_rescan", False)),
                    cache_days=int(settings.get("list_cache_days", 7)),
                )
                hash_value = product_hash(asins)
                old_hash = record.get("product_hash", "")
                changed = bool(old_hash and old_hash != hash_value)
                stamp = now_iso()
                record["current_title"] = title
                record["last_seen"] = stamp
                if changed or not old_hash:
                    record["last_changed"] = stamp
                record["last_product_count"] = str(len(asins))
                record["product_hash"] = hash_value
                record["status"] = "approved_active"
                resolved.append(
                    {
                        "list_id": record["list_id"],
                        "stable_meta_label": record["stable_meta_label"],
                        "current_title": title,
                        "idea_list_url": record["idea_list_url"],
                        "displayed_count": expected or "",
                        "unique_asins": len(asins),
                        "changed_since_previous_run": "yes" if changed else "no",
                        "count_status": "complete" if not expected or len(asins) >= expected else "incomplete",
                        "missing_from_displayed_count": max(0, (expected or 0) - len(asins)) if expected else "",
                        "source": "browser" if used_browser else "checkpoint",
                    }
                )
                for asin in asins:
                    memberships_titles[asin].add(title)
                    memberships_keys[asin].add(record["stable_meta_label"])
            except PlaywrightTimeoutError:
                print(f"Timed out: {entry['current_title']}")
            except Exception as exc:
                print(f"Skipped {entry['current_title']}: {exc}")


        # Every discovered list is active, so there is no separate approval scan.

        browser.close()

    save_registry(registry)

    # Build the authoritative product universe from every Idea List membership.
    # Never reuse the loop variable `asins`, which contains only the final list processed.
    all_asins = sorted(memberships_titles.keys())

    write_csv(
        REPORTS / "new_lists_found.csv",
        new_lists,
        ["list_id", "current_title", "idea_list_url", "include_in_meta", "action"],
    )
    write_csv(
        REPORTS / "renamed_lists.csv",
        renamed_lists,
        ["list_id", "old_title", "new_title", "stable_meta_label", "include_in_meta"],
    )
    write_csv(
        REPORTS / "changed_unapproved_lists.csv",
        changed_unapproved,
        [
            "list_id",
            "current_title",
            "stable_meta_label",
            "previous_product_count",
            "current_product_count",
            "idea_list_url",
            "include_in_meta",
            "action",
        ],
    )
    write_csv(
        REPORTS / "approved_lists_resolved.csv",
        resolved,
        [
            "list_id",
            "stable_meta_label",
            "current_title",
            "idea_list_url",
            "displayed_count",
            "unique_asins",
            "changed_since_previous_run",
            "count_status",
            "missing_from_displayed_count",
            "source",
        ],
    )

    count_mismatches = [row for row in resolved if row.get("count_status") == "incomplete"]
    write_csv(
        REPORTS / "list_count_mismatches.csv",
        count_mismatches,
        [
            "list_id",
            "current_title",
            "idea_list_url",
            "displayed_count",
            "unique_asins",
            "missing_from_displayed_count",
        ],
    )

    set_guide = []
    for record in sorted(
        (record for record in registry.values() if normalize_text(record.get("include_in_meta")).lower() in TRUTHY),
        key=lambda record: normalize_text(record.get("current_title")).lower(),
    ):
        key = record["stable_meta_label"]
        readable_name = record.get("current_title") or record.get("fallback_name") or record["list_id"]
        set_guide.append(
            {
                "meta_product_set_name": readable_name,
                "recommended_attribute": "Internal label",
                "recommended_value": readable_name,
                "recommended_rule": f"Internal label is any of these: {readable_name}",
                "stable_meta_label_backup": key,
            }
        )
    write_csv(
        REPORTS / "meta_product_set_guide.csv",
        set_guide,
        [
            "meta_product_set_name",
            "recommended_attribute",
            "recommended_value",
            "recommended_rule",
            "stable_meta_label_backup",
        ],
    )

    if not memberships_titles:
        print("No approved products were extracted. Review reports and config/idea_list_registry.csv.")
        maybe_open_report(settings, REPORTS / "new_lists_found.csv")
        return 1

    # Cross-list contamination guard. A product appearing in most of the storefront's
    # Idea Lists is almost certainly a recommendation/navigation product rather than
    # a real member of every list. Never silently publish that pattern again.
    processed_list_count = max(1, len(resolved))
    suspicious_threshold = max(
        SUSPICIOUS_MEMBERSHIP_MIN_LISTS,
        int(processed_list_count * SUSPICIOUS_MEMBERSHIP_RATIO),
    )
    suspicious_rows = []
    for asin, titles_set in memberships_titles.items():
        if len(titles_set) >= suspicious_threshold:
            suspicious_rows.append(
                {
                    "asin": asin,
                    "idea_list_count": len(titles_set),
                    "idea_list_titles": "|".join(sorted(titles_set)),
                    "reason": f"Appears in {len(titles_set)} of {processed_list_count} processed Idea Lists",
                }
            )
    write_csv(
        REPORTS / "suspicious_cross_list_products.csv",
        suspicious_rows,
        ["asin", "idea_list_count", "idea_list_titles", "reason"],
    )
    if suspicious_rows:
        sample = ", ".join(row["asin"] for row in suspicious_rows[:10])
        raise RuntimeError(
            f"SAFETY STOP: {len(suspicious_rows)} products still appear in an implausibly high number "
            f"of Idea Lists (threshold {suspicious_threshold}/{processed_list_count}). "
            f"Examples: {sample}. The public feed was not replaced."
        )

    # Reuse product details already obtained in prior runs. This avoids re-requesting
    # thousands of stable titles/images every day; only new ASINs are sent to Amazon.
    cached_rows_list = read_csv(OUTPUT / "meta_catalog_all_returned.csv") or read_csv(PUBLIC / "meta_catalog.csv")
    cached_rows = {normalize_text(row.get("id")).upper(): row for row in cached_rows_list if normalize_text(row.get("id"))}
    refresh_all = bool(settings.get("refresh_all_product_data", False))
    reusable_asins = set() if refresh_all else {asin for asin in all_asins if asin in cached_rows}
    fetch_asins = [asin for asin in all_asins if asin not in reusable_asins]
    print(f"\nProduct cache: reusing {len(reusable_asins)} products; fetching {len(fetch_asins)} new/uncached products.")

    items_by_asin: dict[str, dict[str, Any]] = {}
    errors: list[Any] = []
    if fetch_asins:
        access_token = token(client_id, client_secret)
        print("Amazon Creators API authentication succeeded.")
        api_delay = float(settings.get("api_delay_seconds", 0.35))
        for batch_number, batch in enumerate(chunks(fetch_asins, 10), start=1):
            print(f"Getting new product data: batch {batch_number}/{(len(fetch_asins) + 9) // 10}")
            try:
                items, batch_errors = get_items_with_retry(access_token, partner_tag, batch, max_attempts=4)
                for item in items:
                    asin = normalize_text(item.get("asin")).upper()
                    if asin:
                        items_by_asin[asin] = item
                errors.extend(batch_errors)
            except Exception as exc:
                errors.append({"asins": batch, "message": str(exc)})
            time.sleep(api_delay)

        # Retry omissions in batches, not one ASIN at a time. Failed products are left
        # for the next run instead of turning a catalog refresh into an hours-long loop.
        for retry_round in range(1, 3):
            omitted = [asin for asin in fetch_asins if asin not in items_by_asin]
            if not omitted:
                break
            print(f"Batch retry round {retry_round}: {len(omitted)} products")
            for batch in chunks(omitted, 10):
                try:
                    items, retry_errors = get_items_with_retry(access_token, partner_tag, batch, max_attempts=3)
                    errors.extend(retry_errors)
                    for item in items:
                        returned_asin = normalize_text(item.get("asin")).upper()
                        if returned_asin:
                            items_by_asin[returned_asin] = item
                except Exception as exc:
                    errors.append({"asins": batch, "message": str(exc)})
                time.sleep(api_delay)

    rows_by_asin: dict[str, dict[str, str]] = {}
    for asin in reusable_asins:
        rows_by_asin[asin] = refresh_cached_row_labels(
            cached_rows[asin],
            sorted(memberships_keys.get(asin, set())),
            sorted(memberships_titles.get(asin, set())),
        )
    for asin, item in items_by_asin.items():
        rows_by_asin[asin] = meta_row(
            item,
            sorted(memberships_keys.get(asin, set())),
            sorted(memberships_titles.get(asin, set())),
        )

    rows = [rows_by_asin[asin] for asin in sorted(rows_by_asin)]
    ready = [
        row for row in rows
        if row["id"] and row["title"] and row["link"] and row["image_link"] and row["price"]
    ]

    # Safety validation: never publish navigation text as an Internal Label.
    invalid_label_terms = {"next page", "previous page", "prev page", "see all", "view all", "show more", "load more"}
    bad_rows = []
    for row in ready:
        label_text = normalize_text(row.get("internal_label")).casefold()
        if any(term in label_text for term in invalid_label_terms):
            bad_rows.append(row.get("id", ""))
    if bad_rows:
        raise RuntimeError(
            f"SAFETY STOP: {len(bad_rows)} products contain navigation text in internal_label. "
            "The public feed was not replaced."
        )

    write_csv(OUTPUT / "meta_catalog_all_returned.csv", rows, META_FIELDS)
    write_csv(PUBLIC / "meta_catalog.csv", ready, META_FIELDS)

    membership_rows = []
    for asin in sorted(memberships_titles):
        titles = sorted(memberships_titles[asin])
        keys = sorted(memberships_keys[asin])
        membership_rows.append(
            {
                "asin": asin,
                "idea_list_titles": "|".join(titles),
                "stable_meta_labels": "|".join(keys),
                "idea_list_count": len(titles),
                "warning": "" if len(keys) <= 5 else "All memberships are included in internal_label; only the first 5 stable backup labels appear in custom_label_0-4.",
            }
        )
    write_csv(
        OUTPUT / "product_memberships.csv",
        membership_rows,
        ["asin", "idea_list_titles", "stable_meta_labels", "idea_list_count", "warning"],
    )

    review = []
    for asin in all_asins:
        titles = " | ".join(sorted(memberships_titles[asin]))
        if asin not in rows_by_asin:
            review.append(
                {
                    "asin": asin,
                    "amazon_url": f"https://www.amazon.com/dp/{asin}?tag={partner_tag}",
                    "idea_lists": titles,
                    "issue": "Amazon Creators API returned no product record after retry",
                }
            )
    for row in rows:
        titles = " | ".join(sorted(memberships_titles[row["id"]]))
        if not row["price"]:
            review.append(
                {
                    "asin": row["id"],
                    "amazon_url": f"https://www.amazon.com/dp/{row['id']}?tag={partner_tag}",
                    "idea_lists": titles,
                    "issue": "Amazon Creators API returned the product but no price",
                }
            )
        if not row["image_link"]:
            review.append(
                {
                    "asin": row["id"],
                    "amazon_url": f"https://www.amazon.com/dp/{row['id']}?tag={partner_tag}",
                    "idea_lists": titles,
                    "issue": "Amazon Creators API returned the product but no main image",
                }
            )

    write_csv(
        OUTPUT / "products_needing_review.csv",
        review,
        ["asin", "amazon_url", "idea_lists", "issue"],
    )
    (OUTPUT / "api_errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")

    report = [
        "HILLARY STYLE META CATALOG RUN REPORT",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        f"Storefront/linked lists discovered: {len(all_discovered)}",
        f"New Idea Lists automatically added: {len(new_lists)}",
        f"Renamed lists found: {len(renamed_lists)}",
        f"Lists changed since previous run: {sum(1 for item in resolved if item.get('changed_since_previous_run') == 'yes')}",
        f"Idea Lists processed: {len(resolved)}",
        f"Idea Lists with incomplete item counts: {len(count_mismatches)}",
        f"Unique ASINs extracted from all lists: {len(all_asins)}",
        f"Products returned by Amazon API: {len(rows)}",
        f"Meta-ready products: {len(ready)}",
        f"Products needing review: {len(review)}",
        "Product-set method: Internal label (all Idea List memberships, not limited to five)",
        f"List extraction cache schema: {LIST_CACHE_SCHEMA} (strict product-card DOM extraction)",
        "",
        f"PUBLIC FEED: {PUBLIC / 'meta_catalog.csv'}",
        f"REGISTRY FILE: {CONFIG / 'idea_list_registry.csv'}",
        f"SET GUIDE: {REPORTS / 'meta_product_set_guide.csv'}",
    ]
    (REPORTS / "latest_run_report.txt").write_text("\n".join(report), encoding="utf-8")

    print("\nDONE")
    for line in report[3:]:
        print(line)
    if new_lists or renamed_lists:
        print("\nINFORMATION: The registry was updated automatically.")
        print("New and renamed Idea Lists were included in the Meta catalog without approval.")
        maybe_open_report(settings, REPORTS / "new_lists_found.csv" if new_lists else REPORTS / "renamed_lists.csv")
    print(publish(settings))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
