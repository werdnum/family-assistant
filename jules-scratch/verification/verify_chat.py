from playwright.sync_api import sync_playwright, expect

def run_verification():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("http://localhost:5173/chat")

        # Wait for the app to be ready
        expect(page.locator("html[data-app-ready='true']")).to_be_visible(timeout=10000)

        # Find the message input and send a message
        message_input = page.get_by_placeholder("Write a message...")
        message_input.fill("Hello, this is a test message.")
        message_input.press("Enter")

        # Wait for the user message to appear
        expect(page.get_by_text("Hello, this is a test message.")).to_be_visible()

        # Take a screenshot
        page.screenshot(path="jules-scratch/verification/verification.png")

        browser.close()

if __name__ == "__main__":
    run_verification()