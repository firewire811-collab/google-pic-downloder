from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


ASSET_PREFIX = "https://artsandculture.google.com/asset/"
ASSET_PREFIX_NO_TRAILING = "https://artsandculture.google.com/asset"


_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")


_TRANSLATION_CACHE: dict[str, str] = {}


def is_asset_url(url: str) -> bool:
    u = (url or "").strip()
    return u.startswith(ASSET_PREFIX) or u.startswith(ASSET_PREFIX_NO_TRAILING)


@dataclass(frozen=True)
class AssetMetadata:
    asset_url: str
    title: str
    creator: str
    year: str
    description: str
    thumbnail_url: str


def _clean_text(s: object | None) -> str:
    if s is None:
        return ""
    if isinstance(s, (list, tuple)):
        s = " ".join(str(x) for x in s)
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _extract_year(s: str) -> str:
    s = _clean_text(s)
    # Match 4 digits (YYYY). If we can't find a year, return empty string.
    # This prevents accidentally treating creator/title text as a year.
    m = re.search(r"(\d{4})", s)
    return m.group(1) if m else ""


def _has_hangul(s: str) -> bool:
    return bool(_HANGUL_RE.search(s or ""))


def _translate_to_korean(text: str, *, timeout_s: float) -> str:
    text = _clean_text(text)
    if not text or _has_hangul(text):
        return text
    cached = _TRANSLATION_CACHE.get(text)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "ko",
                "dt": "t",
                "q": text,
            },
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
        translated = "".join(seg[0] for seg in payload[0] if seg and seg[0])
        translated = _clean_text(translated)
        if translated:
            _TRANSLATION_CACHE[text] = translated
            return translated
    except Exception:
        pass
    return text


def _extract_bracketed_json(s: str, start: int) -> str:
    # Extract a JSON array literal starting at `start` (s[start] == '[') using
    # bracket depth tracking while respecting JSON string escaping.
    if start < 0 or start >= len(s) or s[start] != "[":
        return ""

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return ""


def _find_list_with_head(obj: object, head: str) -> list[object] | None:
    # BFS over nested lists to find a list whose first element == head.
    queue: list[object] = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, list) and cur:
            if cur[0] == head:
                return cur
            queue.extend(cur)
    return None


def _strip_html_fragment(s: str) -> str:
    # INIT_data often stores rich text as escaped HTML.
    s = html_lib.unescape(s or "")
    if not s:
        return ""
    soup = BeautifulSoup(s, "lxml")
    return _clean_text(soup.get_text(" ", strip=True))


def _with_query(url: str, **updates: str) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update({k: v for k, v in updates.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _pick_jsonld(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.get_text(strip=True) or "{}")
        except json.JSONDecodeError:
            continue
        # Sometimes it's a list/graph.
        if isinstance(payload, dict):
            if payload.get("@type"):
                return payload
            if "@graph" in payload and isinstance(payload["@graph"], list):
                for node in payload["@graph"]:
                    if isinstance(node, dict) and node.get("@type"):
                        return node
        if isinstance(payload, list):
            for node in payload:
                if isinstance(node, dict) and node.get("@type"):
                    return node
    return None


def _extract_from_jsonld(asset_url: str, obj: dict) -> AssetMetadata:
    title = _clean_text(obj.get("name") or obj.get("headline"))

    creator = ""
    cr = obj.get("creator") or obj.get("author")
    if isinstance(cr, dict):
        creator = _clean_text(cr.get("name"))
    elif isinstance(cr, list) and cr:
        if isinstance(cr[0], dict):
            creator = _clean_text(cr[0].get("name"))
        else:
            creator = _clean_text(str(cr[0]))
    elif isinstance(cr, str):
        creator = _clean_text(cr)

    year = _extract_year(str(obj.get("dateCreated") or obj.get("datePublished") or ""))
    description = _clean_text(obj.get("description"))

    thumbnail_url = ""
    img = obj.get("image")
    if isinstance(img, dict):
        thumbnail_url = _clean_text(img.get("url"))
    elif isinstance(img, list) and img:
        if isinstance(img[0], dict):
            thumbnail_url = _clean_text(img[0].get("url"))
        else:
            thumbnail_url = _clean_text(str(img[0]))
    elif isinstance(img, str):
        thumbnail_url = _clean_text(img)

    return AssetMetadata(
        asset_url=asset_url,
        title=title,
        creator=creator,
        year=year,
        description=description,
        thumbnail_url=thumbnail_url,
    )


def _extract_from_og(asset_url: str, soup: BeautifulSoup) -> AssetMetadata:
    def og(prop: str) -> str:
        t = soup.find("meta", attrs={"property": prop})
        return _clean_text(t.get("content") if t else "")

    title = og("og:title")
    description = og("og:description")
    thumbnail_url = og("og:image")
    # Creator/year are often not present in OG tags.
    return AssetMetadata(
        asset_url=asset_url,
        title=title,
        creator="",
        year="",
        description=description,
        thumbnail_url=thumbnail_url,
    )


def _extract_year_from_selector(soup: BeautifulSoup) -> str:
    selectors = [
        # Selector provided by user (most specific).
        "#yDmH0d > div.PbVjBf.uFenQ.rmC28b.YHVov > div.f9CV0 > div:nth-child(1) > div > div.xzkhqe > div > header > div.j0qD7 > div > div.PugfHe > h2 > span.QtzOu",
        # More resilient fallbacks.
        "header h2 span",
        "header h2",
    ]
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            year = _extract_year(el.get_text(strip=True))
            if year:
                return year
    return ""


def _extract_text_from_selectors(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = _clean_text(el.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _extract_labeled_value(soup: BeautifulSoup, labels: list[str]) -> str:
    # Try to find a label (dt/div/span) and return its paired value.
    label_set = {lbl.lower() for lbl in labels}
    for tag in soup.find_all(["dt", "div", "span", "strong"]):
        txt = _clean_text(tag.get_text(strip=True))
        if txt.lower() not in label_set:
            continue
        if tag.name == "dt":
            sib = tag.find_next_sibling("dd")
            if sib:
                return _clean_text(sib.get_text(" ", strip=True))
        # Fallback: try the next element in the same parent.
        parent = tag.parent
        if parent:
            for child in parent.find_all(["dd", "div", "span"], recursive=False):
                if child is tag:
                    continue
                val = _clean_text(child.get_text(" ", strip=True))
                if val:
                    return val
    return ""


def _extract_creator_from_selector(soup: BeautifulSoup) -> str:
    # Try header link(s) first, then labeled metadata.
    selectors = [
        "header a[href*='/entity/']",
        "header a[href*='/category/artist']",
        "header a[href*='/artist']",
        "header [data-entity-id]",
        "header h2 a",
    ]
    creator = _extract_text_from_selectors(soup, selectors)
    if creator:
        return creator
    return _extract_labeled_value(
        soup,
        ["작가", "Artist", "Creator", "Author"],
    )


def _extract_description_from_selector(soup: BeautifulSoup) -> str:
    selectors = [
        # Selector provided by user (most specific).
        "#yDmH0d > div.PbVjBf.uFenQ.rmC28b.YHVov > div.f9CV0 > div:nth-child(1) > div > div.xzkhqe > section.WDSAyb.QwmCXd > div",
        # More resilient fallbacks.
        "section.WDSAyb.QwmCXd div",
        "section.WDSAyb div",
        "main section div",
    ]
    for selector in selectors:
        el = soup.select_one(selector)
        if not el:
            continue
        text = _clean_text(el.get_text(" ", strip=True))
        if text:
            return text
    return ""


def _extract_description_from_init_data(html: str) -> str:
    # Parse window.INIT_data['Asset:...'] and extract the canonical description
    # from the embedded "stella.av" record (usually stella_av[5][1]).
    if not html:
        return ""

    idx = html.find("window.INIT_data['Asset:")
    if idx == -1:
        return ""

    eq = html.find("=", idx)
    if eq == -1:
        return ""
    start = html.find("[", eq)
    if start == -1:
        return ""
    json_text = _extract_bracketed_json(html, start)
    if not json_text:
        return ""

    try:
        data = json.loads(json_text)
    except Exception:
        return ""

    stella_av = _find_list_with_head(data, "stella.av")
    if not stella_av:
        return ""

    try:
        slot = stella_av[5]
        if isinstance(slot, list) and len(slot) > 1 and isinstance(slot[1], str):
            return _strip_html_fragment(slot[1])
    except Exception:
        pass

    # Fallback: find the first HTML-ish string in stella.av.
    for item in stella_av:
        if not isinstance(item, str):
            continue
        if "<" in item and ">" in item:
            text = _strip_html_fragment(item)
            if text:
                return text
    return ""


def _extract_korean_description_with_playwright(asset_url: str, *, timeout_s: float) -> str:
    try:
        import importlib

        sync_api = importlib.import_module("playwright.sync_api")
        sync_playwright = getattr(sync_api, "sync_playwright")
    except Exception:
        return ""

    url = _with_query(asset_url, hl="ko")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="ko-KR",
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=int(timeout_s * 1000))
            for selector in [
                "section.WDSAyb.QwmCXd div",
                "section.WDSAyb div",
                "main section div",
            ]:
                try:
                    loc = page.locator(selector).first
                    txt = _clean_text(loc.inner_text(timeout=2000))
                except Exception:
                    continue
                if txt and _has_hangul(txt):
                    return txt
            return ""
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()


def fetch_asset_metadata(asset_url: str, *, timeout_s: float = 15.0) -> AssetMetadata:
    # Note: This performs a normal HTTP fetch and parses page metadata.
    # Some pages may require JS rendering; in that case, consider adding an optional Playwright fallback.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(
        asset_url,
        headers=headers,
        params={"hl": "ko", "gl": "KR"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Use selectors first, as they are more specific.
    creator_from_selector = _extract_creator_from_selector(soup)
    year_from_selector = _extract_year_from_selector(soup)
    if not year_from_selector:
        labeled_year = _extract_labeled_value(
            soup,
            ["연도", "제작 연도", "Year", "Date", "Date Created", "Created"],
        )
        year_from_selector = _extract_year(labeled_year)
    desc_from_selector = _extract_description_from_selector(soup)
    desc_from_init_data = _extract_description_from_init_data(resp.text)
    desc = desc_from_init_data or desc_from_selector
    if not desc:
        desc = _extract_korean_description_with_playwright(
            asset_url,
            timeout_s=timeout_s,
        )

    obj = _pick_jsonld(soup)
    if obj:
        md = _extract_from_jsonld(asset_url, obj)
        # If JSON-LD is present but sparse, patch with OG tags.
        og_md = _extract_from_og(asset_url, soup)
        final_desc = desc or md.description or og_md.description
        final_desc = _translate_to_korean(final_desc, timeout_s=timeout_s)
        return AssetMetadata(
            asset_url=asset_url,
            title=md.title or og_md.title,
            creator=creator_from_selector or md.creator,
            year=year_from_selector or md.year,
            description=final_desc,
            thumbnail_url=md.thumbnail_url or og_md.thumbnail_url,
        )

    og_md = _extract_from_og(asset_url, soup)
    final_desc = _translate_to_korean(desc or og_md.description, timeout_s=timeout_s)
    return AssetMetadata(
        asset_url=asset_url,
        title=og_md.title,
        creator=creator_from_selector,
        year=year_from_selector,
        description=final_desc,
        thumbnail_url=og_md.thumbnail_url,
    )
