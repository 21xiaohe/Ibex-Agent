"""Exercise the shipped browser UI against the running local API."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


def main() -> None:
    """Send a real browser request and preserve the rendered result and HTTP evidence."""
    server = ThreadingHTTPServer(("127.0.0.1", 18701), partial(SimpleHTTPRequestHandler, directory="web"))
    Thread(target=server.serve_forever, daemon=True).start()
    events = []
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: events.append({"url": response.url, "status": response.status}) if ":18700/" in response.url else None)
            page.goto("http://127.0.0.1:18701")
            page.locator("#serverInput").fill("http://127.0.0.1:18700")
            page.locator("#input").fill("Reply exactly IBEX_BROWSER_OK. Do not use tools.")
            page.locator("#send").click()
            page.wait_for_function("document.querySelector('.msg.bot .bubble')?.textContent.includes('IBEX_BROWSER_OK')", timeout=90000)
            actual = page.locator(".msg.bot .bubble").last.inner_text()
            escaped = page.evaluate("render('<img src=x onerror=alert(1)>')")
            page.screenshot(path="logs/browser-verification.png", full_page=True)
            Path("logs/browser-verification.json").write_text(json.dumps({"reply": actual, "responses": events, "page_errors": errors, "html_escaped": "<img" not in escaped}, indent=2), encoding="utf-8")
            print(actual, events, errors)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
