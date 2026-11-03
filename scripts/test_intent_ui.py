import asyncio
import os
from urllib.parse import quote
from playwright.async_api import async_playwright

async def main():
    brave_exe = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    profile_dir = os.path.abspath(".brave_profile")
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            executable_path=brave_exe,
            headless=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        
        await context.add_cookies([
            {
                "name": "auth_token",
                "value": "a294d9a719cb70a0a0a58774af41e67472eacd3f",
                "domain": ".x.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            },
            {
                "name": "ct0",
                "value": "b04bcfcd519aa695f5be9070a4d4bd172c3b12e10c1f2bdc91f73a0ea544bdb4328d48f24fcf4ea231f7e37cb451505a49cc1efcd7bf7d9eb4cbb6729f7228ba5959967c4f36c022a017e82a8d6db8fa",
                "domain": ".x.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
            }
        ])
        
        page = context.pages[0] if context.pages else await context.new_page()
        test_text = "Test tweet from automation"
        intent_url = f"https://x.com/intent/post?text={quote(test_text)}"
        print(f"Navigating to intent URL: {intent_url}")
        await page.goto(intent_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)
        
        # Check buttons
        tweet_btn = page.locator("[data-testid='tweetButton']")
        inline_btn = page.locator("[data-testid='tweetButtonInline']")
        
        has_tweet_btn = await tweet_btn.is_visible()
        has_inline_btn = await inline_btn.is_visible()
        print(f"tweetButton visible: {has_tweet_btn}")
        print(f"tweetButtonInline visible: {has_inline_btn}")
        
        # Check editor
        editor = page.locator("[data-testid='tweetTextarea_0']")
        has_editor = await editor.is_visible()
        print(f"tweetTextarea_0 visible: {has_editor}")
        if has_editor:
            print(f"Editor text: {await editor.inner_text()}")
            
        await context.close()
        print("Done inspection!")

if __name__ == "__main__":
    asyncio.run(main())

