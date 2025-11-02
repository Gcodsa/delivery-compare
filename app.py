import os, asyncio, math, re
from flask import Flask, render_template, request, redirect, url_for
from dataclasses import dataclass
from typing import List, Optional, Tuple
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from cachetools import TTLCache

# ================== إعدادات عامة ==================
DEFAULT_CITY = "الرياض"
DEFAULT_LAT = 24.7136
DEFAULT_LNG = 46.6753
TIMEOUT_MS = 25000

# ذاكرة مؤقتة
SEARCH_CACHE = TTLCache(maxsize=256, ttl=300)
MENU_CACHE = TTLCache(maxsize=256, ttl=600)

PRICE_RE = re.compile(r"""
(?<!\d)
(?:SR|SAR|ر\.?س\.?|﷼|ر﷼|ريال)?\s*
(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)
\s*(?:SR|SAR|ر\.?س\.?|﷼|ر﷼|ريال)?
""", re.IGNORECASE | re.VERBOSE)

def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace(" ", ""))
    except:
        return None

@dataclass
class MenuItem:
    name: str
    price: Optional[float]
    image: Optional[str] = None

@dataclass
class AppResult:
    app: str
    item_name: Optional[str]
    item_price: Optional[float]
    delivery_free: bool
    delivery_fee: Optional[float]
    total: Optional[float]

# ================== المساعدات ==================
def best_match(target: str, candidates: List[str]) -> Optional[str]:
    best, score = None, -1
    for c in candidates:
        s = fuzz.token_set_ratio(target, c)
        if s > score:
            score, best = s, c
    return best

def pick_delivery_fee_from_soup(soup: BeautifulSoup) -> Tuple[bool, Optional[float]]:
    texts = [el.get_text(" ", strip=True) for el in soup.find_all(text=True)]
    for t in texts:
        if "مجاني" in t or "Free" in t:
            return True, 0.0
        p = parse_price(t)
        if p:
            return False, p
    return False, None

def extract_menu_items_generic(soup: BeautifulSoup, limit=12):
    items = []
    for card in soup.select("[class*='item'], [class*='menu'], [class*='product'], li, article"):
        text = card.get_text(" ", strip=True)
        price = parse_price(text)
        if not price:
            continue
        name = card.select_one("[class*='title'], .name, h3, h4, h5")
        name = name.get_text(strip=True) if name else text[:40]
        img = card.select_one("img")
        image = img.get("src") if img else None
        items.append(MenuItem(name, price, image))
        if len(items) >= limit:
            break
    return items

# ================== Flask ==================
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    ctx = {"results": None, "menu": None, "query": None, "error": None}
    if request.method == "POST":
        restaurant = (request.form.get("restaurant") or "").strip()
        if not restaurant:
            ctx["error"] = "اكتب اسم المطعم (مثلاً: هرفي)"
            return render_template("index.html", **ctx)

        # الآن نستعمل فقط هنقرستيشن كعرض أولي (تجريبي)
        from playwright.async_api import async_playwright
        async def fetch_menu():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx_page = await browser.new_context(locale="ar-SA")
                page = await ctx_page.new_page()
                await page.goto("https://www.hungerstation.com/sa-ar", timeout=25000)
                await page.fill("input[type='search']", restaurant)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(4000)
                html = await page.content()
                await browser.close()
                soup = BeautifulSoup(html, "lxml")
                return extract_menu_items_generic(soup, limit=12)

        try:
            menu = asyncio.run(fetch_menu())
        except Exception:
            ctx["error"] = "تعذر جلب قائمة الوجبات."
            return render_template("index.html", **ctx)

        ctx["menu"] = menu
        ctx["query"] = {"restaurant": restaurant}
    return render_template("index.html", **ctx)

@app.route("/autocomplete")
def autocomplete():
    query = (request.args.get("query") or "").strip()
    if len(query) < 2:
        return []
    # مؤقتًا نستخدم قائمة وهمية
    suggestions = [r for r in ["هرفي", "ماكدونالدز", "برجر كنج", "البيك", "فاير جريل"] if r.startswith(query)]
    return {"results": suggestions}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
