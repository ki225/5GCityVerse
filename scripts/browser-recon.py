import json
from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "https://dlf3ts9zkseda.cloudfront.net"
OUT = Path("artifacts/browser-recon")
OUT.mkdir(parents=True, exist_ok=True)


def visible_overlaps(page):
    return page.evaluate(
        """() => {
          const els = [...document.querySelectorAll('button,input,select,textarea,[role="button"],header,main,aside')]
            .filter(e => {
              const r = e.getBoundingClientRect();
              const s = getComputedStyle(e);
              return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
            });
          const out = [];
          for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
            const a = els[i].getBoundingClientRect(), b = els[j].getBoundingClientRect();
            const area = Math.max(0, Math.min(a.right,b.right)-Math.max(a.left,b.left)) *
                         Math.max(0, Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));
            const minArea = Math.min(a.width*a.height, b.width*b.height);
            if (area > 4 && area / minArea > 0.15 && !els[i].contains(els[j]) && !els[j].contains(els[i])) {
              out.push([els[i].tagName + ':' + (els[i].innerText || els[i].getAttribute('aria-label') || '').slice(0,60),
                        els[j].tagName + ':' + (els[j].innerText || els[j].getAttribute('aria-label') || '').slice(0,60),
                        Math.round(area/minArea*100)/100]);
            }
          }
          return out.slice(0,50);
        }"""
    )


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    report = {}
    for name, width, height in [("desktop", 1440, 900), ("tablet", 768, 1024), ("mobile", 390, 844)]:
        page = browser.new_page(viewport={"width": width, "height": height})
        console = []
        failed = []
        page.on("console", lambda msg, bag=console: bag.append({"type": msg.type, "text": msg.text}))
        page.on("requestfailed", lambda req, bag=failed: bag.append({"url": req.url, "error": req.failure}))
        page.goto(URL, wait_until="networkidle", timeout=120_000)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        body = page.locator("body")
        report[name] = {
            "title": page.title(),
            "url": page.url,
            "body_text": body.inner_text()[:20000],
            "buttons": page.locator("button").all_inner_texts(),
            "inputs": page.locator("input").count(),
            "input_details": page.locator("input").evaluate_all("els => els.map(e => ({type:e.type,name:e.name,value:e.value,min:e.min,max:e.max,checked:e.checked,aria:e.getAttribute('aria-label')}))"),
            "selects": page.locator("select").count(),
            "scroll": page.evaluate("() => ({sw:document.documentElement.scrollWidth,cw:document.documentElement.clientWidth,sh:document.documentElement.scrollHeight,ch:document.documentElement.clientHeight})"),
            "overlaps": visible_overlaps(page),
            "console": console,
            "failed_requests": failed,
        }
        page.close()
    browser.close()

(OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: {x: v[x] for x in ("title", "buttons", "inputs", "selects", "scroll", "overlaps", "console", "failed_requests")} for k, v in report.items()}, ensure_ascii=False, indent=2))
