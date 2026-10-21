"""Opens the running app in a headless browser and snapshots what a user could click or type."""
import time
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import sync_playwright

SNAPSHOT_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = t => (t || '').trim().replace(/\s+/g, ' ');
  const selector = el => {
    const tag = el.tagName.toLowerCase();
    if (el.id) return '#' + CSS.escape(el.id);
    const tid = el.getAttribute('data-testid');
    if (tid) return `[data-testid="${tid}"]`;
    for (const attr of ['name', 'placeholder', 'aria-label']) {
      const v = el.getAttribute(attr);
      if (v && !v.includes('"')) return `${tag}[${attr}="${v}"]`;
    }
    const t = clean(el.innerText);
    if (t && t.length <= 40 && !t.includes('"')) return `${tag}:has-text("${t}")`;
    return null;
  };
  const seen = new Set(), elements = [];
  for (const el of document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=tab],summary')) {
    if (!visible(el) || el.type === 'hidden') continue;
    const sel = selector(el);
    if (!sel || seen.has(sel)) continue;
    seen.add(sel);
    elements.push({
      tag: el.tagName.toLowerCase(), type: el.type || undefined, selector: sel,
      text: clean(el.innerText).slice(0, 60) || undefined,
      placeholder: el.getAttribute('placeholder') || undefined,
      href: el.tagName === 'A' ? el.getAttribute('href') : undefined,
    });
    if (elements.length >= 60) break;
  }
  return {
    title: document.title,
    page_height: document.documentElement.scrollHeight, viewport_height: innerHeight,
    headings: [...document.querySelectorAll('h1,h2,h3')].filter(visible).slice(0, 15).map(h => clean(h.innerText)),
    text: clean(document.body.innerText).slice(0, 500),
    elements,
  };
}
"""

SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".xml", ".json")


def _goto(page, url: str) -> None:
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
    except Exception:
        page.goto(url, wait_until="load", timeout=20000)
    time.sleep(0.8)


def snapshot_app(url: str, log, max_pages: int = 4) -> list[dict]:
    pages: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        try:
            _goto(page, url)
            first = page.evaluate(SNAPSHOT_JS)
            first["url"] = page.url
            pages.append(first)
            origin = urlparse(url).netloc
            queue = []
            for el in first["elements"]:
                href = el.get("href")
                if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                target, _ = urldefrag(urljoin(page.url, href))
                if urlparse(target).netloc != origin or target.lower().endswith(SKIP_EXT):
                    continue
                if target.rstrip("/") != page.url.rstrip("/") and target not in queue:
                    queue.append(target)
            for target in queue[: max_pages - 1]:
                try:
                    _goto(page, target)
                    snap = page.evaluate(SNAPSHOT_JS)
                    snap["url"] = page.url
                    pages.append(snap)
                except Exception as e:  # a broken link should not stop discovery
                    log(f"Skipped {target}: {str(e).splitlines()[0]}")
        finally:
            browser.close()
    log(f"Inspected {len(pages)} page(s), {sum(len(p['elements']) for p in pages)} interactive elements.")
    return pages
