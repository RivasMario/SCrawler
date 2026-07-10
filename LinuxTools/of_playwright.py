import asyncio
import json
import os
import argparse
import base64
import subprocess
import time
import sys
import re
from playwright.async_api import async_playwright

# Generic User Agent - Playwright will override this based on chosen browser
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Platform-agnostic download base
if sys.platform == "win32":
    DOWNLOAD_BASE = os.path.join(os.environ.get("USERPROFILE", "C:\\"), "Downloads", "OnlyFans")
else:
    DOWNLOAD_BASE = os.path.expanduser("~/Downloads/OnlyFans")

class OFScraper:
    def __init__(self, username, profile_dir, download_dir=None, user_agent=DEFAULT_UA):
        self.username = username
        self.profile_dir = os.path.abspath(profile_dir)
        self.download_base = os.path.abspath(download_dir) if download_dir else DOWNLOAD_BASE
        self.custom_dir = bool(download_dir)
        
        # Meta storage directory
        meta_folder = username if username else "Purchased_Metadata"
        if self.custom_dir:
            self.metadata_dir = os.path.join(self.download_base, "metadata")
        else:
            self.metadata_dir = os.path.join(self.download_base, meta_folder, "metadata")
        os.makedirs(self.metadata_dir, exist_ok=True)
        
        self.user_agent = user_agent
        self.captured_media = {} # id -> data
        self.user_info = {}
        self.uid = None
        self.is_purchased_mode = (username == None)

    async def handle_response(self, response):
        url = response.url
        if response.status != 200: return
        
        try:
            # 1. User Info (Only if not in purchased mode)
            if not self.is_purchased_mode and f"/api2/v2/users/{self.username}" in url and "medias" not in url:
                data = await response.json()
                self.user_info = data
                self.uid = data.get("id")
                print(f"\r  [Capture] User info for {self.username} (UID: {self.uid})")
            
            # 2. Media / Posts / Purchased
            is_media_call = "/posts/medias" in url or "/posts" in url
            is_purchased_call = "/posts/collection/purchased" in url
            
            if is_media_call or is_purchased_call:
                data = await response.json()
                items = data.get("list", []) if isinstance(data, dict) else []
                
                valid_items = []
                for item in items:
                    if self.uid:
                        author = item.get("author", {})
                        if str(author.get("id")) == str(self.uid):
                            valid_items.append(item)
                    else:
                        valid_items.append(item)
                
                if valid_items:
                    self.extract_media(valid_items)
                    mode_label = "Purchased" if is_purchased_call else "Media"
                    print(f"\r  [Capture] {len(valid_items)} items from {mode_label} (Total unique: {len(self.captured_media)})", end="")

        except: pass

    def extract_media(self, items):
        for item in items:
            media_list = item.get("media", [])
            # Try to get creator username, fallback to 'Unknown'
            author_name = item.get("author", {}).get("username", "Unknown_Creator")
            
            for m in media_list:
                m_id = str(m.get("id"))
                if not m_id: continue
                source_url = m.get("source", {}).get("source") or next((m.get("files", {}).get(k, {}).get("url") for k in ("source", "full") if m.get("files", {}).get(k, {}).get("url")), None)
                
                if source_url and m_id not in self.captured_media:
                    self.captured_media[m_id] = {
                        "id": m_id,
                        "type": m.get("type"),
                        "source": source_url,
                        "creator": author_name,
                        "timestamp": item.get("postedAt") or item.get("createdAt")
                    }

    async def auto_scroll(self, page):
        print("\nAuto-scrolling... (Capturing data as it loads)")
        last_height = await page.evaluate("document.body.scrollHeight")
        attempts = 0
        while True:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                attempts += 1
                if attempts >= 5: break
            else:
                attempts = 0
                last_height = new_height
            if await page.query_selector(".b-loader"): await asyncio.sleep(1)

    async def run(self, browser_type="chromium"):
        async with async_playwright() as p:
            if browser_type == "chromium": bt = p.chromium
            elif browser_type == "webkit": bt = p.webkit
            else: bt = p.firefox

            kwargs = {
                'headless': False,
                'viewport': {'width': 1280, 'height': 720}
            }
            if browser_type == "chromium":
                kwargs['channel'] = 'chrome'
                kwargs['ignore_default_args'] = ["--enable-automation"]
                kwargs['args'] = ["--disable-blink-features=AutomationControlled"]

            try:
                context = await bt.launch_persistent_context(
                    self.profile_dir,
                    **kwargs
                )
            except Exception as e:
                print(f"Initial launch failed (ensure Google Chrome is installed if using chromium channel). Falling back... Error: {e}")
                if 'channel' in kwargs:
                    del kwargs['channel']
                try:
                    context = await bt.launch_persistent_context(
                        self.profile_dir,
                        **kwargs
                    )
                except Exception as inner_e:
                    print(f"CRITICAL ERROR: Failed to launch Playwright browser. You may need to run 'python3 -m playwright install'. Error: {inner_e}")
                    return
            
            page = await context.new_page()
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
            except: pass
            
            page.on("response", self.handle_response)
            
            if self.is_purchased_mode:
                print(f"Opening OnlyFans Purchased Tab...")
                await page.goto("https://onlyfans.com/my/collections/purchased")
            else:
                print(f"Opening Media Tab for {self.username}...")
                await page.goto(f"https://onlyfans.com/{self.username}/media")
            
            try:
                await page.wait_for_selector(".b-sidebar", timeout=20000)
            except:
                print("\nLogin required. Please login in the browser window. Waiting for successful login...")
                while True:
                    try:
                        await page.wait_for_selector(".b-sidebar", timeout=5000)
                        print("Login detected! Navigating back to the target page...")
                        if self.is_purchased_mode:
                            await page.goto("https://onlyfans.com/my/collections/purchased")
                        else:
                            await page.goto(f"https://onlyfans.com/{self.username}/media")
                        await page.wait_for_selector(".b-sidebar", timeout=20000)
                        break
                    except:
                        pass

            await self.auto_scroll(page)
            
            # Save metadata
            with open(os.path.join(self.metadata_dir, "media_list.json"), "w") as f:
                json.dump(list(self.captured_media.values()), f, indent=2)
            
            # Export cookies
            browser_cookies = await context.cookies()
            cookie_file = os.path.join(self.metadata_dir, f"cookies_session.txt")
            with open(cookie_file, "w") as f:
                for c in browser_cookies:
                    domain = c['domain'] if c['domain'].startswith('.') else '.' + c['domain']
                    f.write(f"{domain}\tTRUE\t{c['path']}\t{str(c['secure']).upper()}\t{int(c.get('expires', 0))}\t{c['name']}\t{c['value']}\n")
            
            print(f"\nCapture summary: {len(self.captured_media)} items.")
            await context.close()
            self.download_all(cookie_file)

    def download_all(self, cookie_file):
        queue = list(self.captured_media.values())
        print(f"\nDownloading to: {self.download_base}")
        
        downloaded = skipped = errors = 0
        for i, m in enumerate(queue):
            # Always route to the creator's folder within the main Downloads dir
            subfolder = "Photos" if m["type"] == "photo" else "Videos"
            if self.custom_dir:
                target_dir = os.path.join(self.download_base, subfolder)
            else:
                creator_folder = m.get('creator') if m.get('creator') and m.get('creator') != "Unknown_Creator" else (self.username or "Unknown_Creator")
                target_dir = os.path.join(self.download_base, creator_folder, subfolder)
                
            os.makedirs(target_dir, exist_ok=True)
            ext = "mp4" if m["type"] == "video" else "jpg"
            filepath = os.path.join(target_dir, f"{m['id']}.{ext}")
            
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                skipped += 1
                continue
                
            print(f"  [{i+1}/{len(queue)}] {m['id']}.{ext} ({creator_folder})... ", end="", flush=True)
            try:
                cmd = ["curl", "-L", "-s", "--fail", "-o", filepath, "-A", self.user_agent, "-b", cookie_file, m["source"]]
                res = subprocess.run(cmd, timeout=600)
                if res.returncode == 0:
                    downloaded += 1
                    print("Success.")
                else:
                    print(f"Failed.")
                    errors += 1
            except:
                print("Error.")
                errors += 1
            time.sleep(0.1)

        print(f"\n--- SESSION FINISHED ---")
        print(f"New: {downloaded} | Existing: {skipped} | Failed: {errors}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OF Browser Scraper v4")
    parser.add_argument("username", nargs="?", help="OF username (leave empty for Purchased mode)")
    parser.add_argument("--profile", default="./of_profile", help="Browser profile directory")
    parser.add_argument("--browser", choices=["firefox", "chromium", "webkit"], default="chromium", help="Browser engine")
    parser.add_argument("--dir", help="Output directory")
    args = parser.parse_args()
    
    scraper = OFScraper(args.username, args.profile, args.dir)
    asyncio.run(scraper.run(args.browser))
