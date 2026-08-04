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
from datetime import datetime, timezone
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


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_settings() -> dict[str, Any]:
    path = CONFIG / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    selectors = [
        "h1",
        '[data-testid*="list-title"]',
        '[class*="listTitle"]',
        '[class*="title"] h1',
    ]
    for selector in selectors:
        try:
            text = normalize_text(page.locator(selector).first.inner_text(timeout=1200))
            if text and len(text) < 180 and text.lower() not in {"amazon", "amazon.com"}:
                return text
        except Exception:
            pass
    return fallback


def expected_count(page) -> int | None:
    try:
        text = page.locator("body").inner_text(timeout=5000)
        matches = re.findall(r"(?im)^\s*(\d{1,4})\s+Items\s*$", text)
        return int(matches[0]) if matches else None
    except Exception:
        return None


def trim_recommendations(html: str) -> str:
    cuts = []
    for pattern in [r"More\s+from\s+THE\s+HILLARY\s+STYLE", r"More\s+from\s+"]:
        match = re.search(pattern, html or "", re.IGNORECASE)
        if match:
            cuts.append(match.start())
    return (html or "")[: min(cuts)] if cuts else (html or "")


def extract_asins(html: str) -> set[str]:
    html = trim_recommendations(html)
    found: set[str] = set()
    patterns = [
        r"/dp/([A-Z0-9]{10})(?:[/?&#\"']|$)",
        r"/gp/product/([A-Z0-9]{10})(?:[/?&#\"']|$)",
        r"/product/([A-Z0-9]{10})(?:[/?&#\"']|$)",
        r"data-asin=[\"']([A-Z0-9]{10})[\"']",
        r"[\"'](?:asin|ASIN|productAsin|productASIN|productId)[\"']\s*[:=]\s*[\"']([A-Z0-9]{10})[\"']",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.IGNORECASE):
            found.add(match.group(1).upper())
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
            found[list_id] = {"list_id": list_id, "idea_list_url": url, "amazon_title": list_id}
    except Exception:
        pass

    try:
        anchors = page.locator('a[href*="/shop/"][href*="/list/"]')
        count = min(anchors.count(), 2000)
        for index in range(count):
            anchor = anchors.nth(index)
            href = anchor.get_attribute("href") or ""
            url = clean_url(urljoin(page.url, href), storefront_url)
            list_id = extract_list_id(url)
            if not list_id:
                continue
            try:
                title = normalize_text(anchor.inner_text(timeout=350))
            except Exception:
                title = ""
            found[list_id] = {
                "list_id": list_id,
                "idea_list_url": url,
                "amazon_title": title or found.get(list_id, {}).get("amazon_title") or list_id,
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
            },
        )

    print(f"Idea List discovery complete: {len(discovered)} lists found.")
    return [discovered[key] for key in sorted(discovered)]

def scrape_list(context, entry: dict[str, str], quiet: bool = False) -> tuple[str, set[str], int | None]:
    page = context.new_page()
    try:
        if not quiet:
            print(f"\nOpening {entry.get('current_title') or entry.get('fallback_name') or entry['list_id']}")
        page.goto(entry["idea_list_url"], wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(1800)
        fallback = entry.get("current_title") or entry.get("fallback_name") or entry["list_id"]
        title = get_title(page, fallback)
        expected = expected_count(page)
        if not quiet:
            print(f"Amazon title: {title}")
            if expected:
                print(f"Amazon displays {expected} items.")

        found: set[str] = set()
        no_new = 0
        for step in range(1, 301):
            before = len(found)
            try:
                found.update(extract_asins(page.content()))
            except Exception:
                pass
            added = len(found) - before
            if not quiet and (step == 1 or added or step % 20 == 0):
                print(f"  Step {step}: {len(found)} unique ASINs (+{added})")
            if expected and len(found) >= expected:
                break
            try:
                body = page.locator("body").inner_text(timeout=1200)
                if re.search(r"More\s+from\s+THE\s+HILLARY\s+STYLE", body, re.IGNORECASE):
                    break
            except Exception:
                pass
            no_new = no_new + 1 if added == 0 else 0
            try:
                y, max_y = page.evaluate(
                    """() => {
                        const h = window.innerHeight || 900;
                        window.scrollBy(0, Math.max(500, Math.floor(h * 0.75)));
                        const maxY = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - h;
                        return [window.scrollY, maxY];
                    }"""
                )
                page.wait_for_timeout(700)
                bottom = int(y) >= int(max_y) - 50
            except Exception:
                bottom = False
            if (bottom and no_new >= 8) or no_new >= 20:
                break
        if not quiet:
            print(f"Finished {title}: {len(found)} unique ASINs")
        return title, found, expected
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
        title = normalize_text(entry.get("amazon_title")) or list_id
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


def registry_entry(record: dict[str, str]) -> dict[str, str]:
    return {
        "list_id": record["list_id"],
        "fallback_name": record.get("fallback_name") or record.get("current_title") or record["list_id"],
        "current_title": record.get("current_title") or record.get("fallback_name") or record["list_id"],
        "idea_list_url": record["idea_list_url"],
        "stable_meta_label": record.get("stable_meta_label") or stable_meta_label(record["list_id"]),
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
        timeout=90,
    )
    if response.status_code == 429:
        raise RuntimeError("Amazon rate limit reached")
    if not response.ok:
        raise RuntimeError(f"GetItems failed ({response.status_code}): {response.text[:1500]}")
    data = response.json()
    return (data.get("itemsResult") or {}).get("items") or [], data.get("errors") or []


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


def meta_row(item: dict[str, Any], stable_labels: list[str]) -> dict[str, str]:
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
        "custom_label_0": labels[0],
        "custom_label_1": labels[1],
        "custom_label_2": labels[2],
        "custom_label_3": labels[3],
        "custom_label_4": labels[4],
    }


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
    for directory in (CONFIG, PUBLIC, OUTPUT, REPORTS, LOGS):
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
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-US")

        if settings.get("discover_storefront_lists", True):
            all_discovered = discover_all_lists(context, settings, approved)
            new_lists, renamed_lists = update_registry_from_discovery(registry, all_discovered)

        approved_records = [record for record in registry.values() if record.get("idea_list_url")]
        approved_records.sort(key=lambda record: (normalize_text(record.get("current_title")).lower(), record["list_id"]))

        print(f"\nAll discovered Idea Lists to publish: {len(approved_records)}")
        for record in approved_records:
            entry = registry_entry(record)
            try:
                title, asins, expected = scrape_list(context, entry)
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
        ],
    )

    set_guide = []
    for record in sorted(
        (record for record in registry.values() if normalize_text(record.get("include_in_meta")).lower() in TRUTHY),
        key=lambda record: normalize_text(record.get("current_title")).lower(),
    ):
        key = record["stable_meta_label"]
        set_guide.append(
            {
                "meta_product_set_name": record.get("current_title") or record.get("fallback_name") or record["list_id"],
                "stable_meta_label": key,
                "rule_1": f"custom_label_0 equals {key}",
                "rule_2": f"OR custom_label_1 equals {key}",
                "rule_3": f"OR custom_label_2 equals {key}",
                "rule_4": f"OR custom_label_3 equals {key}",
                "rule_5": f"OR custom_label_4 equals {key}",
            }
        )
    write_csv(
        REPORTS / "meta_product_set_guide.csv",
        set_guide,
        ["meta_product_set_name", "stable_meta_label", "rule_1", "rule_2", "rule_3", "rule_4", "rule_5"],
    )

    if not memberships_titles:
        print("No approved products were extracted. Review reports and config/idea_list_registry.csv.")
        maybe_open_report(settings, REPORTS / "new_lists_found.csv")
        return 1

    access_token = token(client_id, client_secret)
    print("\nAmazon Creators API authentication succeeded.")
    asins = sorted(memberships_titles)
    items_by_asin: dict[str, dict[str, Any]] = {}
    errors: list[Any] = []

    for batch_number, batch in enumerate(chunks(asins, 10), start=1):
        print(f"Getting product data: batch {batch_number}/{(len(asins) + 9) // 10}")
        try:
            items, batch_errors = get_items(access_token, partner_tag, batch)
        except RuntimeError as exc:
            if "rate limit" in str(exc).lower():
                time.sleep(35)
                items, batch_errors = get_items(access_token, partner_tag, batch)
            else:
                raise
        for item in items:
            asin = normalize_text(item.get("asin")).upper()
            if asin:
                items_by_asin[asin] = item
        errors.extend(batch_errors)
        time.sleep(1.1)

    omitted = [asin for asin in asins if asin not in items_by_asin]
    for index, asin in enumerate(omitted, start=1):
        print(f"Retry {index}/{len(omitted)}: {asin}")
        try:
            items, retry_errors = get_items(access_token, partner_tag, [asin])
            errors.extend(retry_errors)
            for item in items:
                returned_asin = normalize_text(item.get("asin")).upper()
                if returned_asin:
                    items_by_asin[returned_asin] = item
        except Exception as exc:
            errors.append({"asin": asin, "message": str(exc)})
        time.sleep(1.1)

    rows = [
        meta_row(item, sorted(memberships_keys.get(asin, set())))
        for asin, item in items_by_asin.items()
    ]
    rows.sort(key=lambda row: row["id"])
    ready = [
        row
        for row in rows
        if row["id"] and row["title"] and row["link"] and row["image_link"] and row["price"]
    ]

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
                "warning": "More than 5 memberships; Meta receives only the first 5 stable labels." if len(keys) > 5 else "",
            }
        )
    write_csv(
        OUTPUT / "product_memberships.csv",
        membership_rows,
        ["asin", "idea_list_titles", "stable_meta_labels", "idea_list_count", "warning"],
    )

    review = []
    for asin in asins:
        titles = " | ".join(sorted(memberships_titles[asin]))
        if asin not in items_by_asin:
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
        f"Unique ASINs extracted from all lists: {len(asins)}",
        f"Products returned by Amazon API: {len(rows)}",
        f"Meta-ready products: {len(ready)}",
        f"Products needing review: {len(review)}",
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
