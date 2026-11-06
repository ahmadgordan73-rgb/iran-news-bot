"""ربات خبری مجله نظامی ایران - هر ۱۵ دقیقه با GitHub Actions اجرا می‌شود."""
import os, calendar, json, re, time, html, hashlib, datetime as dt, concurrent.futures as cf
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
import requests, feedparser, trafilatura

# ---------------- تنظیمات ----------------
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
TG_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL      = os.environ.get("TELEGRAM_CHANNEL") or "@iranmilitarymag"
ADMIN_CHAT   = os.environ.get("ADMIN_CHAT_ID", "")          # آیدی عددی خودت برای پیام خطا (اختیاری)
DRY_RUN      = os.environ.get("DRY_RUN") == "1"             # فقط چاپ، بدون ارسال
POSTS_PER_RUN   = int(os.environ.get("POSTS_PER_RUN", "3"))
BATCH_SIZE      = 8          # چند خبر در یک درخواست ترجمه
MAX_WAIT_MIN    = 40         # حداکثر انتظار خبر قبل از ترجمه
MAX_AGE_HOURS   = 6          # خبرهای قدیمی‌تر از این نادیده گرفته می‌شوند
SIGNATURE = "🇮🇷مجله نظامی ایران\n\n@iranmilitarymag"
STATE_FILE = "state.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
API = "https://generativelanguage.googleapis.com/v1beta"

KEYWORDS = r"""iran|tehran|persian gulf|hormuz|irgc|khamenei|pezeshkian|araghchi|trump|white house|pentagon|
israel|idf|netanyahu|gaza|hamas|hezbollah|houthi|yemen|lebanon|syria|iraq|mossad|
military|army|navy|naval|air force|missile|drone|nuclear|weapon|defen[cs]e|war|strike|attack|troops|
fighter|jet|warship|carrier|submarine|sanction|ceasefire|soldier|marine|bomb|artillery|radar|f-35|f-16|
ایران|اسرائیل|ترامپ|ישראל|איראן|טראמפ|צה"ל|חמאס|חיזבאללה|إيران|إسرائيل|ترامب|عسكري|صاروخ"""
KW = re.compile(KEYWORDS.replace("\n", ""), re.I)

SELECT_PROMPT = """You are the news editor of a Persian Telegram channel about Iran, Trump, Israel and military affairs.
Below are candidate headlines (id | source | title | summary).
Pick ONLY items clearly related to: Iran, Donald Trump, Israel, or military/defense/war/security topics.
Drop duplicates (same event as another candidate OR as a recently posted headline) – keep the most informative one.
Drop opinion fluff, ads, podcasts, sports, lifestyle.
Return JSON: {"ids": [ids ordered from most to least important]}. Empty list if nothing qualifies.

Recently posted headlines (avoid repeating these events):
%s

Candidates:
%s"""

TRANSLATE_PROMPT = """تو سردبیر حرفه‌ای یک کانال خبری فارسی‌زبان (مجله نظامی ایران) هستی.
برای هر خبر زیر، بر اساس متن اصلی، یک پست خبری فارسی روان، دقیق و رسانه‌ای بنویس (نه ترجمه تحت‌اللفظی و ماشینی).
قواعد:
- title: تیتر کوتاه و گیرا، حداکثر ۹۰ کاراکتر، بدون نقطه پایانی.
- p1, p2, p3: تا سه بند؛ بند ۱ اصل خبر، بند ۲ جزئیات و نقل‌قول‌ها، بند ۳ زمینه یا پیامد خبر. اگر متن منبع کافی نیست، بندهای اضافه را خالی بگذار؛ برای کامل کردن آن‌ها چیزی حدس نزن.
- مجموع title و سه بند حداکثر ۸۵۰ کاراکتر.
- فقط از اطلاعات متن خبر استفاده کن؛ چیزی از خودت اضافه نکن. اعداد، نام‌ها و نقل‌قول‌ها دقیق باشند.
- نام‌های خاص را با املای رایج رسانه‌های فارسی بنویس. از ایموجی، هشتگ و لینک استفاده نکن.
- اگر خبر در واقع به ایران، ترامپ، اسرائیل یا امور نظامی/امنیتی ربط ندارد، relevant را false بگذار.
خروجی فقط JSON به این شکل:
{"posts":[{"id":"...","relevant":true,"title":"...","p1":"...","p2":"...","p3":"..."}]}

خبرها:
%s"""

# ---------------- ابزارها ----------------
def pacific_day():  # سقف روزانه Gemini نیمه‌شب به وقت آمریکا (Pacific) صفر می‌شود
    return dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

def now():
    return time.time()

def fresh_today(item):
    """فقط خبر دارای تاریخ انتشار مشخص، مربوط به امروز تهران و شش ساعت اخیر."""
    if not item.get("date_verified"):
        return False
    try:
        timestamp = float(item["ts"])
        current = now()
        age = current - timestamp
        tz = ZoneInfo("Asia/Tehran")
        return (0 <= age <= MAX_AGE_HOURS * 3600 and
                dt.datetime.fromtimestamp(timestamp, tz).date() ==
                dt.datetime.fromtimestamp(current, tz).date())
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return False

def load_state():
    try:
        s = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        s = {}
    for k, v in {"seen": {}, "pending": [], "ready": [], "posted_titles": [], "usage": {}, "exhausted": {}, "models": {}, "last_alert": 0}.items():
        s.setdefault(k, v)
    if s["usage"].get("day") != pacific_day():
        s["usage"] = {"day": pacific_day()}
        s["exhausted"] = {}
    return s

def save_state(s):
    cutoff = now() - 3 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    s["posted_titles"] = s["posted_titles"][-60:]
    json.dump(s, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

def alert(s, msg):
    print("ALERT:", msg)
    if ADMIN_CHAT and TG_TOKEN and not DRY_RUN and now() - s["last_alert"] > 3 * 3600:
        s["last_alert"] = now()
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": ADMIN_CHAT, "text": "⚠️ ربات خبری: " + msg[:3500]}, timeout=20)
        except Exception:
            pass

def uid(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]

def clean(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(t or ""))).strip()

# ---------------- مدل‌های Gemini ----------------
def discover_models(s):
    """مدل‌های در دسترس را از گوگل می‌گیرد تا اگر نام مدل‌ها عوض شد، ربات خراب نشود."""
    if s["models"].get("day") == pacific_day():
        return s["models"]
    r = requests.get(f"{API}/models", params={"key": GEMINI_KEY, "pageSize": 200}, timeout=30)
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    bad = re.compile(r"tts|image|audio|live|embedding|thinking-exp|robotics|computer|lyria|veo|imagen|learnlm|gemma", re.I)
    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n); return float(m.group(1)) if m else 0
    def rank(n):  # نسخه بالاتر، و نسخه پایدار قبل از preview
        return (-ver(n), "preview" in n or "exp" in n, n.endswith("latest"), len(n))
    flash = sorted([n for n in names if "flash" in n and "lite" not in n and not bad.search(n)], key=rank)
    lite  = sorted([n for n in names if "flash-lite" in n and not bad.search(n)], key=rank)
    # از هر نسخه فقط یکی (سقف رایگان برای هر مدل جداست)
    def uniq(lst):
        out, vs = [], set()
        for n in lst:
            if ver(n) not in vs: vs.add(ver(n)); out.append(n)
        return out
    s["models"] = {"day": pacific_day(), "flash": uniq(flash)[:4], "lite": uniq(lite)[:3]}
    print("models:", s["models"])
    return s["models"]

def gemini(s, kind, prompt, max_tokens):
    """kind = flash یا lite. مدل‌ها را به ترتیب امتحان می‌کند؛ اگر سقف یکی پر شد، سراغ بعدی می‌رود."""
    models = discover_models(s)[kind]
    errors = []
    for m in models:
        if s["exhausted"].get(m):
            continue
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens,
                                     "responseMimeType": "application/json"}}
        if "gemini-3" in m:
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
        for attempt in range(2):
            t0 = time.time()
            try:
                r = requests.post(f"{API}/models/{m}:generateContent", params={"key": GEMINI_KEY}, json=body, timeout=150)
            except Exception as e:
                print(f"gemini {m}: {str(e)[:120]} ({time.time()-t0:.0f}s)")
                errors.append(f"{m}: {e}"); time.sleep(5); continue
            print(f"gemini {m}: HTTP {r.status_code} ({time.time()-t0:.0f}s)")
            if r.status_code == 400 and "thinking" in r.text.lower():
                body["generationConfig"].pop("thinkingConfig", None); continue
            if r.status_code == 429:
                txt = r.text.lower()
                if "per_day" in txt or "perday" in txt or "daily" in txt or attempt == 1:
                    s["exhausted"][m] = True; errors.append(f"{m}: سقف روزانه پر شد"); break
                time.sleep(30); continue  # سقف دقیقه‌ای؛ کمی صبر
            if r.status_code in (400, 404) and "not found" in r.text.lower():
                s["exhausted"][m] = True; errors.append(f"{m}: پیدا نشد"); break
            if r.status_code >= 400:
                errors.append(f"{m}: HTTP {r.status_code} {r.text[:200]}"); time.sleep(5); continue
            s["usage"][m] = s["usage"].get(m, 0) + 1
            try:
                cand = r.json()["candidates"][0]
                text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                return json.loads(text)
            except Exception as e:
                errors.append(f"{m}: پاسخ نامعتبر ({e})"); break
    raise RuntimeError(" | ".join(errors) or "هیچ مدلی در دسترس نیست")

# ---------------- جمع‌آوری خبر ----------------
def fetch_arab_via_feedly(f):
    """Use Feedly's public cached copy of the same Arab News RSS when blocked."""
    try:
        r = requests.get("https://cloud.feedly.com/v3/streams/contents", params={
            "streamId": "feed/https://www.arabnews.com/rss.xml", "count": 100}, timeout=25)
        r.raise_for_status()
        data = r.json()
        out = []
        for entry in data.get("items", [])[:100]:
            links = entry.get("alternate") or []
            link = next((x.get("href", "") for x in links if isinstance(x, dict)), "")
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https") or parsed.hostname not in ("arabnews.com", "www.arabnews.com"):
                continue
            published = entry.get("published")
            if not isinstance(published, (int, float)) or published <= 0:
                continue
            title = clean(entry.get("title"))[:300]
            if not title:
                continue
            summary = entry.get("summary") or {}
            content = summary.get("content", "") if isinstance(summary, dict) else ""
            out.append({"id": uid(link), "url": link, "title": title,
                        "summary": clean(content)[:1000], "ts": published / 1000,
                        "date_verified": True, "img": "", "source": f["name"],
                        "country": f["country"], "lang": f["lang"]})
        print("Arab News Feedly fallback:", len(out))
        return out
    except Exception as e:
        print("Arab News Feedly fallback error:", str(e)[:120])
        return []

def fetch_feed(f):
    try:
        r = requests.get(f["url"], headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        d = feedparser.parse(r.content)
        if not d.entries and f["url"] == "https://www.arabnews.com/rss.xml":
            print("Arab News RSS empty; trying Feedly")
            return fetch_arab_via_feedly(f)
    except Exception as e:
        print("feed error:", f["name"], str(e)[:80])
        if f["url"] == "https://www.arabnews.com/rss.xml":
            return fetch_arab_via_feedly(f)
        return []
    out = []
    for e in d.entries[:40]:
        link = e.get("link") or ""
        if not link: continue
        t = e.get("published_parsed")
        if not t: continue  # تاریخ نامعلوم را تازه فرض نکن
        try:
            ts = calendar.timegm(t)
        except (ValueError, TypeError, OverflowError):
            continue
        img = ""
        for key in ("media_content", "media_thumbnail"):
            if e.get(key): img = e[key][0].get("url", ""); break
        if not img:
            for l in e.get("links", []):
                if l.get("type", "").startswith("image"): img = l.get("href", ""); break
        out.append({"id": uid(link), "url": link, "title": clean(e.get("title"))[:300],
                    "summary": clean(e.get("summary"))[:400], "ts": ts, "date_verified": True, "img": img,
                    "source": f["name"], "country": f["country"], "lang": f["lang"]})
    return out

def collect(s, feeds):
    with cf.ThreadPoolExecutor(16) as p:
        items = [i for lst in p.map(fetch_feed, feeds) for i in lst]
    fresh, ids = [], set()
    for i in items:
        if i["id"] in s["seen"] or i["id"] in ids: continue
        ids.add(i["id"])
        if not fresh_today(i):
            s["seen"][i["id"]] = now(); continue
        fresh.append(i)
    print(f"items={len(items)} new={len(fresh)}")
    return fresh

def get_article(item):
    """متن کامل و عکس اصلی خبر."""
    try:
        r = requests.get(item["url"], headers={"User-Agent": UA}, timeout=25)
        page = r.text
        text = trafilatura.extract(page, include_comments=False, favor_precision=True) or ""
        if not item["img"]:
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page) or \
                re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page)
            if m: item["img"] = html.unescape(m.group(1))
    except Exception:
        text = ""
    if len(text) < 300:
        text = item["title"] + "\n" + item["summary"] + "\n" + text
    return text[:5000]

# ---------------- مراحل ----------------
def select(s, fresh):
    cands = [i for i in fresh if KW.search(i["title"] + " " + i["summary"])]
    for i in fresh: s["seen"][i["id"]] = now()
    if not cands: return
    cands = sorted(cands, key=lambda x: -x["ts"])[:80]
    lines = "\n".join(f'{i["id"]} | {i["source"]} | {i["title"]} | {i["summary"][:200]}' for i in cands)
    recent = "\n".join(s["posted_titles"][-40:] + [p["title"] for p in s["pending"]]) or "-"
    try:
        res = gemini(s, "lite", SELECT_PROMPT % (recent, lines), 2000)
        chosen = [str(x) for x in res.get("ids", [])]
    except Exception as e:
        alert(s, "انتخاب خبر انجام نشد: " + str(e)); chosen = [i["id"] for i in cands[:6]]
    by = {i["id"]: i for i in cands}
    for rank, cid in enumerate(chosen):
        if cid in by:
            it = by[cid]; it["added"] = now(); it["rank"] = rank
            s["pending"].append(it)
    print("selected:", len(chosen))

def translate(s):
    s["pending"] = [p for p in s["pending"] if fresh_today(p)]
    if not s["pending"]: return
    oldest = min(p["added"] for p in s["pending"])
    need = len(s["ready"]) < POSTS_PER_RUN
    if os.environ.get("FORCE_TRANSLATE") != "1" and len(s["pending"]) < BATCH_SIZE and now() - oldest < MAX_WAIT_MIN * 60 and not (need and len(s["pending"]) >= POSTS_PER_RUN):
        print("waiting to batch:", len(s["pending"])); return
    s["pending"].sort(key=lambda p: (p.get("rank", 99), -p["ts"]))
    batch, s["pending"] = s["pending"][:BATCH_SIZE], s["pending"][BATCH_SIZE:]
    with cf.ThreadPoolExecutor(8) as p:
        texts = list(p.map(get_article, batch))
    blocks = "\n\n".join(f'=== id: {b["id"]} | منبع: {b["source"]}\nTITLE: {b["title"]}\nTEXT:\n{t}' for b, t in zip(batch, texts))
    try:
        res = gemini(s, "flash", TRANSLATE_PROMPT % blocks, 16000)
    except Exception as e:
        s["pending"] = batch + s["pending"]  # دوباره بعداً
        alert(s, "ترجمه با مدل Flash انجام نشد (احتمالاً سقف رایگان امروز پر شده). " + str(e)); return
    by = {b["id"]: b for b in batch}
    for p in res.get("posts", []):
        b = by.get(str(p.get("id")))
        if not b or not p.get("relevant", True) or not p.get("title"): continue
        b.update(fa_title=p["title"], paras=[p.get("p1", ""), p.get("p2", ""), p.get("p3", "")])
        s["ready"].append(b)
    print("translated:", len(res.get("posts", [])))

def build(b):
    e = html.escape
    lines = [f'🔸 رسانه <a href="{e(b["url"], quote=True)}">\u2068{e(b["source"])}\u2069</a> / {b["country"]} / {b["lang"]}',
             f'🔻 <b>{e(b["fa_title"])}</b>']
    lines += [f"🔹 {e(p.strip())}" for p in b["paras"] if p.strip()]
    lines.append(SIGNATURE)
    return "\n\n".join(lines)

def visible_len(h):
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)).encode("utf-16-le")) // 2

def tg(method, data):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data, timeout=60)
    return r.json()

def post(s):
    s["ready"] = [b for b in s["ready"] if fresh_today(b)]
    s["ready"].sort(key=lambda b: (b.get("rank", 99), -b["ts"]))
    sent = 0
    while s["ready"] and sent < POSTS_PER_RUN:
        b = s["ready"].pop(0)
        text = build(b)
        if DRY_RUN:
            print("-----\n" + text); ok = True
        else:
            print("sending text only")
            r = tg("sendMessage", {"chat_id": CHANNEL, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
            ok = r.get("ok")
            if not ok:
                alert(s, "ارسال به تلگرام نشد: " + json.dumps(r, ensure_ascii=False)[:300])
                s["ready"].insert(0, b); break
        if ok:
            sent += 1; s["posted_titles"].append(b["title"]); print("-----\n" + text)
        time.sleep(2)
    print("posted:", sent, "| ready left:", len(s["ready"]), "| pending:", len(s["pending"]))

def main():
    s = load_state()
    if TG_TOKEN and not DRY_RUN:
        me = tg("getMe", {})
        print("telegram bot:", "@" + me.get("result", {}).get("username", "?"), "| channel:", CHANNEL)
    feeds = json.load(open("feeds.json", encoding="utf-8"))
    try:
        select(s, collect(s, feeds))
        translate(s)
        post(s)
    finally:
        print("usage today:", s["usage"])
        save_state(s)

if __name__ == "__main__":
    main()
