"""Check authentication and actual SSE in the deployed browser UI."""
import json
from pathlib import Path
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright


def main() -> None:
    """Keep credentials inside the input field; store only statuses and rendered reply."""
    config = dotenv_values(".env")
    key = next(iter(json.loads(config["IBEX_API_KEYS"])))
    base = "http://127.0.0.1:" + config.get("IBEX_HTTP_PORT", "18704")
    statuses, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: statuses.append(response.status) if response.url.endswith("/chat/stream") else None)
        page.goto(base)
        page.locator("#input").fill("Reply WEB_AUTH_OK; no tools.")
        page.locator("#send").click()
        page.wait_for_function("!document.querySelector('#send').disabled", timeout=15000)
        assert statuses == [401]
        page.locator("#apiKeyInput").fill(key)
        page.locator("#input").fill("Reply exactly WEB_AUTH_OK. Do not use tools.")
        page.locator("#send").click()
        page.wait_for_function("document.querySelectorAll('.msg.bot .bubble').length >= 2 && [...document.querySelectorAll('.msg.bot .bubble')].at(-1).textContent.includes('WEB_AUTH_OK')", timeout=90000)
        page.wait_for_function("!document.querySelector('#send').disabled", timeout=30000)
        assert statuses == [401, 200] and not errors
        # Clear the credential before producing a shareable screenshot.
        page.locator("#apiKeyInput").fill("")
        page.screenshot(path="logs/completion-browser.png", full_page=True)
        result = {"statuses": statuses, "errors": errors, "reply": page.locator(".msg.bot .bubble").last.inner_text()}
        Path("logs/completion-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
