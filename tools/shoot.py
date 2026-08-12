"""Screenshot helper for visual QA of the dashboard."""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8077"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/freelancer/projects/40643875/shots"

EMAILS = {
    "manager": "marta.lindqvist@northwind-commercial.example",
}

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.screenshot(path=f"{OUT}/01-login.png")

    who = sys.argv[2] if len(sys.argv) > 2 else EMAILS["manager"]
    page.fill("#email", who)
    page.fill("#password", "demo1234")
    page.click("button[type=submit]")
    page.wait_for_selector(".kpi", timeout=15000)
    page.wait_for_timeout(1200)
    page.screenshot(path=f"{OUT}/02-dashboard-top.png")

    page.mouse.wheel(0, 760)
    page.wait_for_timeout(700)
    page.screenshot(path=f"{OUT}/03-dashboard-mid.png")

    page.mouse.wheel(0, 760)
    page.wait_for_timeout(700)
    page.screenshot(path=f"{OUT}/04-dashboard-low.png")

    # dark mode
    page.mouse.wheel(0, -2000)
    page.click("#theme")
    page.wait_for_timeout(800)
    page.screenshot(path=f"{OUT}/05-dark.png")
    page.click("#theme")

    print("console errors:", errors or "none")
    b.close()
