import os, re, asyncio, math
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from flask import Flask, render_template, request, redirect, url_for
from cachetools import TTLCache
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# ----------------- إعدادات افتراضية -----------------
DEFAULT_CITY = "الرياض"
DEFAULT_LAT = 24.7136
DEFAULT_LNG = 46.6753
TIMEOUT_MS = 25000

# ذاكرة مؤقتة
SEARCH_CACHE = TTLCache(maxsize=256, ttl=300)   # 5 دقائق
MENU_CACHE   = TTLCache(maxsize=256, ttl=600)   # 10 دقائق

# تعبير رقم السعر (ريال/SAR…)
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
    num = m.group(1).replace(",", "").replace(" ", "")
    try:
        return float(num)
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

# ----------------- Helpers -----------------
async def ensure_ready(page, timeout: int):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout//2)
        except:
            pass

def best_match(target: str, candidates: List[str]) -> Optional[str]:
    best, score = None, -1
    for c in candidates:
        s = fuzz.token_set_ratio(target, c)
        if s > score:
            score, best = s, c
    return best

def pick_delivery_fee_from_soup(soup: BeautifulSoup) -> Tuple[bool, Optional[float]]:
    texts = []
    for sel in ["[class*='delivery']", "[id*='delivery']", "[class*='رسوم']", "[id*='رسوم']", "[class*='fee']", "[id*='fee']"]:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                texts.append(t)
    texts = list(dict.fromkeys(texts))
    for t in texts:
        if "مجاني" in t or "Free" in t:
            return True, 0.0
    for t in texts:
        p = parse_price(t)
        if p is not None:
            return False, p
    full = soup.get_text(" ", strip=True)
    if "مجاني" in full or "Free" in full:
        return True, 0.0
    p = parse_price(full)
    if p is not None:
        return False, p
    return False, None

def extract_menu_items_generic(soup: BeautifulSoup, limit: int = 12) -> List[MenuItem]:
    """هيوريستك عامة: يلتقط أول N وجبات باسم + سعر + صورة (إن وجدت)"""
    items: List[MenuItem] = []
    cards = soup.select(
        "[class*='item'], [class*='menu'], [class*='product'], [data-testid*='item'], li, article"
    )
    for card in cards:
        text = card.get_text(" ", strip=True)
        price = parse_price(text)
        if price is None:
            continue
        # اسم
        name = None
        for sel in ["[class*='title']", ".name", "h3", "h4", "h5", "[data-testid*='title']"]:
            t = card.select_one(sel)
            if t:
                name = t.get_text(strip=True)
                break
        if not name:
            # fallback: أول 40 حرف قبل السعر
            name = text.split("\n")[0][:40]

        # صورة
        img_url = None
        img = card.select_one("img")
        if img:
            img_url = img.get("src") or img.get("data-src") or img.get("data-original")
        items.append(MenuItem(name=name, price=price, image=img_url))

        if len(items) >= limit:
            break
    return items

# ----------------- Providers -----------------
class BaseProvider:
    name = "BASE"
    base_url = ""
    def __init__(self, city: str, lat: float, lng: float, timeout_ms: int = TIMEOUT_MS):
        self.city = city
        self.lat = lat
        self.lng = lng
        self.timeout_ms = timeout_ms

    async def open(self, play):
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ar-SA",
            geolocation={"latitude": self.lat, "longitude": self.lng},
            permissions=["geolocation"],
        )
        page = await ctx.new_page()
        return browser, ctx, page

class HungerStation(BaseProvider):
    name = "هنقرستيشن"
    base_url = "https://www.hungerstation.com/sa-ar"

    async def fetch_menu(self, play, restaurant: str) -> Tuple[List[MenuItem], bool, Optional[float]]:
        """يرجع منيو (اسم + سعر + صورة)، والتوصيل"""
        browser, ctx, page = await self.open(play)
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", restaurant)
            await page.keyboard.press("Enter")
            await ensure_ready(page, self.timeout_ms)

            # التقط بطاقة المطعم الأقرب
            cards = await page.query_selector_all("a[href*='restaurant'], a[href*='rest']")
            options = []
            for c in cards[:30]:
                t = (await c.text_content()) or ""
                t = re.sub(r"\s+", " ", t).strip()
                if t:
                    options.append(t)
            pick = best_match(restaurant, options)
            if pick:
                for c in cards[:30]:
                    t = (await c.text_content()) or ""
                    t = re.sub(r"\s+", " ", t).strip()
                    if t == pick:
                        await c.click()
                        break
                await ensure_ready(page, self.timeout_ms)

            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            items = extract_menu_items_generic(soup, limit=12)
            free, fee = pick_delivery_fee_from_soup(soup)
            return items, free, fee
        except:
            return [], False, None
        finally:
            await ctx.close(); await browser.close()

    async def quote_item(self, play, restaurant: str, target_item_name: str) -> AppResult:
        """يعطي سعر نفس الوجبة (بالاسم) قدر الإمكان + التوصيل + الإجمالي"""
        items, free, fee = await self.fetch_menu(play, restaurant)
        # اختار أقرب اسم للصنف المطلوب
        cand = best_match(target_item_name, [i.name for i in items]) if items else None
        price = None
        if cand:
            for i in items:
                if i.name == cand:
                    price = i.price
                    break
        total = None
        if price is not None:
            total = price + (0.0 if free else (fee or 0.0))
        return AppResult(self.name, cand, price, free, fee, total)

class Jahez(BaseProvider):
    name = "جاهز"
    base_url = "https://www.jahez.net/ar"

    async def quote_item(self, play, restaurant: str, target_item_name: str) -> AppResult:
        browser, ctx, page = await self.open(play)
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", restaurant)
            await page.keyboard.press("Enter")
            await ensure_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            items = extract_menu_items_generic(soup, limit=50)
            free, fee = pick_delivery_fee_from_soup(soup)
            cand = best_match(target_item_name, [i.name for i in items]) if items else None
            price = None
            if cand:
                for i in items:
                    if i.name == cand:
                        price = i.price
                        break
            total = None
            if price is not None:
                total = price + (0.0 if free else (fee or 0.0))
            return AppResult(self.name, cand, price, free, fee, total)
        except:
            return AppResult(self.name, None, None, False, None, None)
        finally:
            await ctx.close(); await browser.close()

class Kieta(BaseProvider):
    name = "كيتا"
    base_url = "https://kieta.sa/"

    async def quote_item(self, play, restaurant: str, target_item_name: str) -> AppResult:
        browser, ctx, page = await self.open(play)
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", restaurant)
            await page.keyboard.press("Enter")
            await ensure_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            items = extract_menu_items_generic(soup, limit=50)
            free, fee = pick_delivery_fee_from_soup(soup)
            cand = best_match(target_item_name, [i.name for i in items]) if items else None
            price = None
            if cand:
                for i in items:
                    if i.name == cand:
                        price = i.price
                        break
            total = None
            if price is not None:
                total = price + (0.0 if free else (fee or 0.0))
            return AppResult(self.name, cand, price, free, fee, total)
        except:
            return AppResult(self.name, None, None, False, None, None)
        finally:
            await ctx.close(); await browser.close()

class ToYou(BaseProvider):
    name = "تو يو"
    base_url = "https://www.toyou.com/ar"

    async def quote_item(self, play, restaurant: str, target_item_name: str) -> AppResult:
        browser, ctx, page = await self.open(play)
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", restaurant)
            await page.keyboard.press("Enter")
            await ensure_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            items = extract_menu_items_generic(soup, limit=50)
            free, fee = pick_delivery_fee_from_soup(soup)
            cand = best_match(target_item_name, [i.name for i in items]) if items else None
            price = None
            if cand:
                for i in items:
                    if i.name == cand:
                        price = i.price
                        break
            total = None
            if price is not None:
                total = price + (0.0 if free else (fee or 0.0))
            return AppResult(self.name, cand, price, free, fee, total)
        except:
            return AppResult(self.name, None, None, False, None, None)
        finally:
            await ctx.close(); await browser.close()

class Mandoub(BaseProvider):
    name = "مستر مندوب"
    base_url = "https://www.mandoubapp.com"

    async def quote_item(self, play, restaurant: str, target_item_name: str) -> AppResult:
        browser, ctx, page = await self.open(play)
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", restaurant)
            await page.keyboard.press("Enter")
            await ensure_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            items = extract_menu_items_generic(soup, limit=50)
            free, fee = pick_delivery_fee_from_soup(soup)
            cand = best_match(target_item_name, [i.name for i in items]) if items else None
            price = None
            if cand:
                for i in items:
                    if i.name == cand:
                        price = i.price
                        break
            total = None
            if price is not None:
                total = price + (0.0 if free else (fee or 0.0))
            return AppResult(self.name, cand, price, free, fee, total)
        except:
            return AppResult(self.name, None, None, False, None, None)
        finally:
            await ctx.close(); await browser.close()

PROVIDERS = [HungerStation, Jahez, Kieta, ToYou, Mandoub]

# ----------------- تشغيل المجمع -----------------
async def gather_menu_from_hunger(restaurant: str, city: str, lat: float, lng: float) -> Tuple[List[MenuItem], bool, Optional[float]]:
    """نبدأ بالقائمة (مع الصور) من هنقرستيشن لأنه أوضح عادةً"""
    cache_key = f"MENU|{restaurant}|{lat:.4f},{lng:.4f}"
    if cache_key in MENU_CACHE:
        return MENU_CACHE[cache_key]
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        hs = HungerStation(city, lat, lng, TIMEOUT_MS)
        menu, free, fee = await hs.fetch_menu(p, restaurant)
    MENU_CACHE[cache_key] = (menu, free, fee)
    return menu, free, fee

async def compare_across_apps(restaurant: str, meal_name: str, city: str, lat: float, lng: float) -> List[AppResult]:
    cache_key = f"CMP|{restaurant}|{meal_name}|{lat:.4f},{lng:.4f}"
    if cache_key in SEARCH_CACHE:
        return SEARCH_CACHE[cache_key]
    from playwright.async_api import async_playwright
    results: List[AppResult] = []
    async with async_playwright() as p:
        tasks = [prov(city, lat, lng).quote_item(p, restaurant, meal_name) for prov in PROVIDERS]
        out = await asyncio.gather(*tasks, return_exceptions=True)
        for r in out:
            if isinstance(r, AppResult):
                results.append(r)
            elif hasattr(r, "__dict__") and "app" in r.__dict__:
                results.append(r)  # rarely
    # رتب حسب الإجمالي (المعرف فقط)
    results = sorted(results, key=lambda x: (x.total is None, x.total if x.total is not None else math.inf))
    SEARCH_CACHE[cache_key] = results
    return results

# ----------------- Flask -----------------
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def index():
    ctx = {"results": None, "menu": None, "query": None, "error": None}
    if request.method == "POST":
        restaurant = (request.form.get("restaurant") or "").strip()
        if not restaurant:
            ctx["error"] = "اكتب اسم المطعم (مثلاً: هرفي)"
            return render_template("index.html", **ctx)

        city = DEFAULT_CITY
        lat, lng = DEFAULT_LAT, DEFAULT_LNG

        # اجلب منيو بالصور من هنقرستيشن
        try:
            menu, free, fee = asyncio.run(gather_menu_from_hunger(restaurant, city, lat, lng))
        except Exception as e:
            ctx["error"] = "تعذّر جلب قائمة الوجبات. حاول مرة أخرى."
            return render_template("index.html", **ctx)

        ctx["menu"] = menu
        ctx["query"] = {"restaurant": restaurant, "city": city}
        return render_template("menu.html", **ctx)
    return render_template("index.html", **ctx)

@app.route("/compare", methods=["POST"])
def compare():
    restaurant = (request.form.get("restaurant") or "").strip()
    meal_name = (request.form.get("meal_name") or "").strip()
    if not restaurant or not meal_name:
        return redirect(url_for("index"))

    city = DEFAULT_CITY
    lat, lng = DEFAULT_LAT, DEFAULT_LNG

    try:
        results = asyncio.run(compare_across_apps(restaurant, meal_name, city, lat, lng))
    except Exception:
        results = []

    return render_template("compare.html",
                           restaurant=restaurant,
                           meal_name=meal_name,
                           results=results)

from flask import jsonify
import asyncio

@app.route("/autocomplete")
def autocomplete():
    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify([])

    async def fetch_names():
        import re
        names = set()
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(locale="ar-SA")
                page = await ctx.new_page()
                await page.goto("https://www.hungerstation.com/sa-ar", timeout=15000)
                await page.keyboard.press("Escape")
                await page.fill("input[type='search']", query)
                await page.wait_for_timeout(3000)

                html = await page.content()
                text = re.sub(r"<[^>]+>", " ", html)
                matches = re.findall(r"[\u0621-\u064A]{3,}(?:\s[\u0621-\u064A]{2,})?", text)
                for name in matches:
                    if name.strip().startswith(query) and len(name) > 3:
                        names.add(name.strip())
        except Exception as e:
            print("❌ Error in autocomplete:", e)
        return sorted(names)[:10]

    try:
        results = asyncio.run(fetch_names())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(fetch_names())

    print("🔎 نتائج البحث:", results)
    return jsonify(results)


if __name__ == "__main__":
    # للتجربة محليًا
    app.run(host="0.0.0.0", port=5000, debug=True)
