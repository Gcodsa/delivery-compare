import os, re, time, yaml, asyncio
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict
from flask import Flask, render_template, request
from cachetools import TTLCache
from pydantic import BaseModel
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from rapidfuzz import fuzz

# ---------- إعدادات ----------
DEFAULT_CONF = {
    "city": "الرياض",
    "lat": 24.7136,
    "lng": 46.6753,
    "timeout_ms": 25000,
    "currency_hint": "ريال|SAR|ر\\.س|﷼|SR",
}
if os.path.exists("config.yaml"):
    DEFAULT_CONF.update(yaml.safe_load(open("config.yaml", "r", encoding="utf-8")) or {})

PRICE_RE = re.compile(r"""
(?<!\d)
(?:SR|SAR|ر\.?س\.?|﷼|ر﷼|ريال)?\s*
(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)
\s*(?:SR|SAR|ر\.?س\.?|﷼|ر﷼|ريال)?
""", re.IGNORECASE | re.VERBOSE)

def parse_price(val: str) -> Optional[float]:
    if not val: return None
    m = PRICE_RE.search(val)
    if not m: return None
    num = m.group(1).replace(",", "").replace(" ", "")
    try:
        return float(num)
    except:
        return None

@dataclass
class Quote:
    app_name: str
    branch: str
    item_name: str
    item_price: Optional[float]
    delivery_fee: Optional[float]
    delivery_free: bool
    eta: Optional[str]
    url: Optional[str]

    @property
    def total(self) -> Optional[float]:
        if self.item_price is None:
            return None
        fee = 0.0 if self.delivery_free else (self.delivery_fee or 0.0)
        return round(self.item_price + fee, 2)

# ذاكرة مؤقتة 5 دقائق
CACHE = TTLCache(maxsize=256, ttl=300)

# ---------- أدوات عامة للقراءة ----------
async def ensure_page_ready(page, timeout_ms: int):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms//2)
        except:
            pass

async def text_content(el):
    try:
        return (await el.text_content()) or ""
    except:
        return ""

def best_like(target: str, candidates: List[str]) -> Optional[str]:
    # يختار أقرب نص للاسم المطلوب (مثلاً "هرفي")
    best = None; best_score = -1
    for c in candidates:
        s = fuzz.partial_ratio(target, c)
        if s > best_score:
            best_score = s; best = c
    return best

# ---------- محولات التطبيقات (هيوريستكس عامة + انتقائية) ----------
class BaseProvider:
    name = "BASE"
    base_url = ""
    search_url = ""
    def __init__(self, city: str, lat: float, lng: float, timeout_ms: int):
        self.city = city; self.lat = lat; self.lng = lng; self.timeout_ms = timeout_ms

    async def search(self, play, query: str) -> Optional[Quote]:
        raise NotImplementedError

    # مساعدات عامة لاستخراج السعر/التوصيل بمرونة
    def pick_min_item_price(self, soup: BeautifulSoup) -> Optional[Dict]:
        # يبحث عن أقل سعر واضح في بطاقات الأصناف داخل صفحة المطعم
        cards = soup.select("[class*='item'], [class*='menu'], [class*='product'], [data-testid*='item']")
        best_price = None; best_name = None
        for card in cards[:100]:
            txt = card.get_text(separator=" ", strip=True)
            p = parse_price(txt)
            if p is not None:
                if (best_price is None) or (p < best_price):
                    # اسم الصنف التقريبي
                    title_el = None
                    for sel in ["[class*='title']","h3","h4",".name","[data-testid*='title']"]:
                        te = card.select_one(sel)
                        if te: title_el = te; break
                    name = title_el.get_text(strip=True) if title_el else txt[:60]
                    best_price = p; best_name = name
        if best_price is not None:
            return {"name": best_name, "price": best_price}
        return None

    def pick_delivery_fee(self, soup: BeautifulSoup) -> (bool, Optional[float]):
        fee_texts = []
        for sel in [
            "[class*='delivery']", "[id*='delivery']",
            "[class*='fee']", "[id*='fee']",
            "[class*='charges']",
            "[class*='shipping']",
            "[class*='رسوم']", "[id*='رسوم']"
        ]:
            for el in soup.select(sel):
                t = el.get_text(separator=" ", strip=True)
                if t: fee_texts.append(t)
        fee_texts = list(dict.fromkeys(fee_texts))
        # مجاني؟
        for t in fee_texts:
            if "مجاني" in t or "Free" in t:
                return True, 0.0
        # قيمة؟
        for t in fee_texts:
            p = parse_price(t)
            if p is not None:
                return False, p
        # فحص النص الكامل كملاذ أخير
        full = soup.get_text(separator=" ", strip=True)
        if "مجاني" in full or "Free" in full:
            return True, 0.0
        p = parse_price(full)
        if p is not None:
            return False, p
        return False, None

# --------- مزود: هنقرستيشن (هيوريستك) ---------
class HungerStation(BaseProvider):
    name = "HungerStation"
    base_url = "https://www.hungerstation.com/sa-ar"
    async def search(self, play, query: str) -> Optional[Quote]:
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", geolocation={"latitude": self.lat, "longitude": self.lng}, permissions=["geolocation"])
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_page_ready(page, self.timeout_ms)
            # حقل البحث العام
            # إذا فيه مودال للمدينة، نحاول تجاوزه تلقائياً
            # البحث
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", query)
            await page.keyboard.press("Enter")
            await ensure_page_ready(page, self.timeout_ms)

            # التقاط بطاقة المطعم الأقرب للاسم
            cards = await page.query_selector_all("a[href*='restaurant'], a[href*='rest']")
            names = []
            for c in cards[:20]:
                t = (await c.text_content()) or ""
                t = re.sub(r"\s+", " ", t).strip()
                if t: names.append(t)
            best = best_like(query, names)
            if not best:
                await ctx.close(); await browser.close(); return None
            # انقر بطاقة المطعم المطابقة
            for c in cards[:20]:
                t = (await c.text_content()) or ""
                t2 = re.sub(r"\s+", " ", t).strip()
                if t2 == best:
                    await c.click()
                    break
            await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")

            item = self.pick_min_item_price(soup)
            free, fee = self.pick_delivery_fee(soup)
            return Quote(self.name, best, item["name"] if item else "", item["price"] if item else None, fee, free, None, page.url)
        except:
            return None
        finally:
            await ctx.close(); await browser.close()

# --------- مزود: جاهز (هيوريستك) ---------
class Jahez(BaseProvider):
    name = "Jahez"
    base_url = "https://www.jahez.net/ar"
    async def search(self, play, query: str) -> Optional[Quote]:
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", geolocation={"latitude": self.lat, "longitude": self.lng}, permissions=["geolocation"])
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_page_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            # غالباً فيه مربع بحث
            await page.fill("input[type='search'], input[role='searchbox']", query)
            await page.keyboard.press("Enter")
            await ensure_page_ready(page, self.timeout_ms)
            cards = await page.query_selector_all("a:has-text('"+query[:4]+"'), a[href*='restaurant']")
            names = []
            for c in cards[:20]:
                t = (await c.text_content()) or ""
                t = re.sub(r"\s+", " ", t).strip()
                if t: names.append(t)
            best = best_like(query, names)
            if not best: return None
            # افتح المطعم
            for c in cards[:20]:
                t = (await c.text_content()) or ""
                if re.sub(r"\s+"," ", (t or "")).strip() == best:
                    await c.click(); break
            await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            item = self.pick_min_item_price(soup)
            free, fee = self.pick_delivery_fee(soup)
            return Quote(self.name, best, item["name"] if item else "", item["price"] if item else None, fee, free, None, page.url)
        except:
            return None
        finally:
            await ctx.close(); await browser.close()

# --------- مزود: كيتا ---------
class Kieta(BaseProvider):
    name = "Kieta"
    base_url = "https://kieta.sa/"
    async def search(self, play, query: str) -> Optional[Quote]:
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", geolocation={"latitude": self.lat, "longitude": self.lng}, permissions=["geolocation"])
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_page_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", query)
            await page.keyboard.press("Enter")
            await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            # البحث عن بطاقة تحتوي اسم المطعم
            candidates = [a.get_text(" ", strip=True) for a in soup.select("a") if query[:3] in a.get_text(" ", strip=True)]
            best = best_like(query, candidates)
            if not best: return None
            # محاولة فتح أول رابط مطابق
            link = None
            for a in soup.select("a"):
                t = a.get_text(" ", strip=True)
                if t == best:
                    link = a.get("href"); break
            if link:
                await page.goto(link, timeout=self.timeout_ms)
                await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            item = self.pick_min_item_price(soup)
            free, fee = self.pick_delivery_fee(soup)
            return Quote(self.name, best, item["name"] if item else "", item["price"] if item else None, fee, free, None, page.url)
        except:
            return None
        finally:
            await ctx.close(); await browser.close()

# --------- مزود: تو يو ---------
class ToYou(BaseProvider):
    name = "ToYou"
    base_url = "https://www.toyou.com/ar"
    async def search(self, play, query: str) -> Optional[Quote]:
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", geolocation={"latitude": self.lat, "longitude": self.lng}, permissions=["geolocation"])
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_page_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", query)
            await page.keyboard.press("Enter")
            await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            candidates = [a.get_text(" ", strip=True) for a in soup.select("a") if query[:3] in a.get_text(" ", strip=True)]
            best = best_like(query, candidates)
            if not best: return None
            # افتح صفحة المطعم إن توفر الرابط
            link = None
            for a in soup.select("a"):
                if a.get_text(" ", strip=True) == best:
                    link = a.get("href"); break
            if link:
                await page.goto(link, timeout=self.timeout_ms)
                await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            item = self.pick_min_item_price(soup)
            free, fee = self.pick_delivery_fee(soup)
            return Quote(self.name, best, item["name"] if item else "", item["price"] if item else None, fee, free, None, page.url)
        except:
            return None
        finally:
            await ctx.close(); await browser.close()

# --------- مزود: مستر مندوب (Mrsool/مستر مندوب) ---------
class MisterMandoub(BaseProvider):
    name = "Mister Mandoub"
    base_url = "https://www.mandoubapp.com"  # قد يختلف الدومين، الهيوريستك عام
    async def search(self, play, query: str) -> Optional[Quote]:
        browser = await play.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", geolocation={"latitude": self.lat, "longitude": self.lng}, permissions=["geolocation"])
        page = await ctx.new_page()
        try:
            await page.goto(self.base_url, timeout=self.timeout_ms)
            await ensure_page_ready(page, self.timeout_ms)
            await page.keyboard.press("Escape")
            await page.fill("input[type='search'], input[role='searchbox']", query)
            await page.keyboard.press("Enter")
            await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            candidates = [a.get_text(" ", strip=True) for a in soup.select("a") if query[:3] in a.get_text(" ", strip=True)]
            best = best_like(query, candidates)
            if not best: return None
            # افتح المطعم
            link = None
            for a in soup.select("a"):
                if a.get_text(" ", strip=True) == best:
                    link = a.get("href"); break
            if link:
                await page.goto(link, timeout=self.timeout_ms)
                await ensure_page_ready(page, self.timeout_ms)
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            item = self.pick_min_item_price(soup)
            free, fee = self.pick_delivery_fee(soup)
            return Quote(self.name, best, item["name"] if item else "", item["price"] if item else None, fee, free, None, page.url)
        except:
            return None
        finally:
            await ctx.close(); await browser.close()

PROVIDERS = [HungerStation, Jahez, Kieta, ToYou, MisterMandoub]

# ---------- التنفيذ الموازي ----------
async def run_search(query: str, city: str, lat: float, lng: float, timeout_ms: int) -> List[Quote]:
    async with async_playwright() as play:
        tasks = []
        for P in PROVIDERS:
            tasks.append(P(city, lat, lng, timeout_ms).search(play, query))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quotes: List[Quote] = []
        for r in results:
            if isinstance(r, Quote):
                quotes.append(r)
            elif isinstance(r, Exception):
                # ممكن تسجّل الأخطاء إن رغبت
                pass
            elif r is not None:
                quotes.append(r)
        return quotes

# ---------- موقع الويب ----------
app = Flask(__name__)

class SearchForm(BaseModel):
    restaurant: str
    city: str = DEFAULT_CONF["city"]
    lat: float = DEFAULT_CONF["lat"]
    lng: float = DEFAULT_CONF["lng"]

def sort_quotes(qs: List[Quote]) -> List[Quote]:
    # ترتيب حسب الإجمالي (None في الأخير)
    return sorted(qs, key=lambda q: (q.total is None, q.total if q.total is not None else 1e9))

@app.route("/", methods=["GET", "POST"])
def index():
    ctx = {"conf": DEFAULT_CONF, "results": None, "query": None}
    if request.method == "POST":
        restaurant = (request.form.get("restaurant") or "").strip()
        city = (request.form.get("city") or DEFAULT_CONF["city"]).strip()
        lat = float(request.form.get("lat") or DEFAULT_CONF["lat"])
        lng = float(request.form.get("lng") or DEFAULT_CONF["lng"])
        if not restaurant:
            ctx["error"] = "اكتب اسم المطعم (مثلاً: هرفي)"
            return render_template("index.html", **ctx)
        cache_key = f"{restaurant}|{city}|{lat:.4f},{lng:.4f}"
        if cache_key in CACHE:
            quotes = CACHE[cache_key]
        else:
            quotes = asyncio.run(run_search(restaurant, city, lat, lng, int(DEFAULT_CONF["timeout_ms"])))
            CACHE[cache_key] = quotes
        ctx["results"] = sort_quotes(quotes)
        ctx["query"] = {"restaurant": restaurant, "city": city, "lat": lat, "lng": lng}
    return render_template("index.html", **ctx)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
