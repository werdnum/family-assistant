import asyncio
from playwright.async_api import async_playwright, expect

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # Navigate to the main page
        await page.goto("http://localhost:5173/")

        # Wait for the heading to be visible to ensure the page has loaded
        heading = page.locator("h1:has-text('Family Assistant')")
        await expect(heading).to_be_visible(timeout=10000)

        # Take a screenshot
        await page.screenshot(path="jules-scratch/verification/main_page.png")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())