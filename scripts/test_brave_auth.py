import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def main():
    brave_exe = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    profile_dir = os.path.abspath(".brave_profile")
    
    print(f"Launching Brave via Playwright from {brave_exe}...")
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
        print("Brave persistent context launched!")
        
        # Inject X cookies from .env
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
        print("Session cookies injected!")
        
        page = context.pages[0] if context.pages else await context.new_page()
        print("Navigating to https://x.com/home ...")
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(5)
        
        print(f"Current URL: {page.url}")
        print(f"Page title: {await page.title()}")
        
        # Check login state
        post_btn = page.locator("[data-testid='SideNav_NewTweet_Button']")
        is_logged_in = await post_btn.is_visible()
        print(f"Is Logged In to X? {is_logged_in}")
        
        if not is_logged_in:
            account_btn = page.locator("[data-testid='AppTabBar_Profile_Link']")
            print(f"Alternative check (Profile Link): {await account_btn.is_visible()}")
        
        await context.close()
        print("Done!")

if __name__ == "__main__":
    asyncio.run(main())

