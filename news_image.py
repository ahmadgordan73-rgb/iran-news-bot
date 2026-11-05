"""Capture an article page and add the channel logo; fail closed to text-only."""
import io
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright

LOGO = os.path.join(os.path.dirname(__file__), "logo.png")


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        return False
    if os.environ.get("NEWS_ALLOW_LOCAL_TEST") == "1" and parsed.hostname in ("127.0.0.1", "localhost"):
        return True
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(addr[4][0]).is_global for addr in addresses)
    except (OSError, ValueError):
        return False


def make_image(url, article_title):
    try:
        if not public_url(url):
            return None
        with sync_playwright() as p:
            kwargs = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            executable = os.environ.get("NEWS_BROWSER_EXECUTABLE")
            if executable:
                kwargs["executable_path"] = executable
            browser = p.chromium.launch(**kwargs)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 1100}, device_scale_factor=1, java_script_enabled=False)
                # Block requests to local/private network targets and non-web schemes.
                def guard(route):
                    if public_url(route.request.url): route.continue_()
                    else: route.abort()
                context.route("**/*", guard)
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                if not response or response.status >= 400 or not public_url(page.url):
                    return None
                page.wait_for_timeout(2500)
                heading = page.locator("h1:visible").first
                if heading.count() == 0:
                    return None
                h1 = heading.inner_text(timeout=3000).strip()
                words = lambda text: {w for w in re.findall(r"[^\W_]{4,}", text.lower())}
                source_words = words(article_title)
                if len(h1) < 12 or (source_words and not (words(h1) & source_words)):
                    return None
                box = heading.bounding_box()
                if not box or box['y'] < 0 or box['y'] + box['height'] > 1100:
                    return None
                # Require a substantial article image visible in the screenshot.
                found_image = False
                for image in page.locator("img:visible").all()[:60]:
                    b = image.bounding_box()
                    if b and b['width'] >= 250 and b['height'] >= 150 and b['y'] >= 0 and b['y'] + min(b['height'], 150) <= 1100:
                        found_image = True
                        break
                if not found_image:
                    return None
                # Do not publish a cookie/consent overlay instead of the news page.
                for selector in ['[role="dialog"]', '[aria-modal="true"]', '[id*="cookie" i]', '[class*="cookie-banner" i]', '[id*="consent" i]']:
                    for element in page.locator(selector).all()[:8]:
                        if element.is_visible():
                            b = element.bounding_box()
                            if b and b['width'] > 250 and b['height'] > 60:
                                return None
                raw = page.screenshot(type="png", timeout=12000)
            finally:
                browser.close()
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        logo = Image.open(LOGO).convert("RGBA")
        logo = logo.crop(logo.getchannel("A").getbbox())
        logo.thumbnail((int(image.width * .35), int(image.height * .26)), Image.Resampling.LANCZOS)
        margin = int(image.width * .025)
        logo_x = image.width - logo.width - margin
        logo_y = image.height - logo.height - margin
        # Never cover the page's actual headline with the channel logo.
        if (box['x'] < logo_x + logo.width and box['x'] + box['width'] > logo_x and
                box['y'] < logo_y + logo.height and box['y'] + box['height'] > logo_y):
            return None
        image.alpha_composite(logo, (logo_x, logo_y))
        out = io.BytesIO()
        image.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception as e:
        print("screenshot unavailable:", type(e).__name__, str(e)[:180])
        return None
