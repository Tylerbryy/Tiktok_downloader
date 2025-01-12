import asyncio
import os
from typing import List, Dict
from playwright.async_api import async_playwright, Page, TimeoutError
import aiohttp
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

import re
import json

class TikTokDownloader:
    def __init__(self):
        self.download_path = 'tiktok_downloads'
        self.debug = False
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
        self.video_selectors = [
            "div[data-e2e='user-post-item']",
            "div[class*='DivItemContainer']",
            "a[href*='/video/']",
            "div[class*='VideoItem']"
        ]

    def print_debug(self, message: str):
        """Print debug message if debug mode is enabled"""
        if self.debug:
            print(f"[DEBUG] {message}")

    async def wait_for_videos(self, page: Page) -> bool:
        """Wait for videos to load with multiple selector attempts"""
        for selector in self.video_selectors:
            try:
                await page.wait_for_selector(selector, timeout=5000)
                self.print_debug(f"Found videos with selector: {selector}")
                return True
            except TimeoutError:
                self.print_debug(f"Timeout for selector: {selector}")
                continue
        return False

    async def extract_video_info(self, page: Page) -> List[Dict]:
        """Extract video information from the page"""
        try:
            # Try getting videos through JavaScript evaluation
            video_links = await page.evaluate('''
                () => Array.from(new Set(
                    Array.from(document.querySelectorAll('a[href*="/video/"]'))
                        .map(el => ({
                            url: el.href,
                            timestamp: Date.now()
                        }))
                        .filter(link => link.url.includes('/video/'))
                ))
            ''')
            
            self.print_debug(f"Found {len(video_links)} video links")
            return video_links
            
        except Exception as e:
            self.print_debug(f"Error extracting video info: {str(e)}")
            return []

    async def auto_scroll(self, page: Page, max_scrolls: int = 20) -> int:
        """Auto-scroll the page to load all videos"""
        self.print_debug("Starting auto-scroll")
        last_height = await page.evaluate('document.documentElement.scrollHeight')
        stable_count = 0
        
        for scroll in range(max_scrolls):
            await page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
            await page.wait_for_timeout(1000)
            
            new_height = await page.evaluate('document.documentElement.scrollHeight')
            if new_height == last_height:
                stable_count += 1
                if stable_count >= 3:  # If height remains same for 3 scrolls
                    break
            else:
                stable_count = 0
                last_height = new_height
                
            self.print_debug(f"Scroll {scroll + 1}: Height = {new_height}")
        
        video_count = await page.evaluate('document.querySelectorAll(\'a[href*="/video/"]\').length')
        self.print_debug(f"Found {video_count} videos after scrolling")
        return video_count

    async def get_video_urls(self, page: Page, profile_url: str) -> List[Dict]:
        """Get video URLs from profile page"""
        try:
            self.print_debug(f"Loading profile: {profile_url}")
            await page.goto(profile_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)
            
            if not await self.wait_for_videos(page):
                self.print_debug("No videos found, attempting reload")
                await page.reload()
                if not await self.wait_for_videos(page):
                    self.print_debug("Still no videos found after reload")
                    return []
            
            await self.auto_scroll(page)
            videos = await self.extract_video_info(page)
            self.print_debug(f"Extracted {len(videos)} video URLs")
            return videos
            
        except Exception as e:
            self.print_debug(f"Error getting video URLs: {str(e)}")
            return []

    async def download_video(self, session: aiohttp.ClientSession, video_info: Dict, save_path: str, filename: str) -> bool:
        """Download a single video"""
        try:
            # Add more headers to mimic browser
            headers = {
                **self.headers,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
            }

            self.print_debug(f"Downloading video: {filename}")
            
            # First request to get the video page
            async with session.get(video_info['url'], headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    self.print_debug(f"Failed to get video page: {response.status}")
                    return False
                
                html = await response.text()
                
                # Multiple patterns to find video URL
                url_patterns = [
                    r'<video[^>]+src="([^"]+)"',
                    r'"playAddr":"([^"]+)"',
                    r'"downloadAddr":"([^"]+)"',
                    r'<link[^>]+?rel="video_src"[^>]+?href="([^"]+)"',
                    r'property="og:video"\s+content="([^"]+)"'
                ]
                
                video_url = None
                for pattern in url_patterns:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        video_url = match.group(1).replace('\\u002F', '/').replace('&amp;', '&')
                        self.print_debug(f"Found video URL using pattern: {pattern}")
                        break

                if not video_url:
                    # Try alternate method - look for videoData in JavaScript
                    js_match = re.search(r'videoData["\']:\s*({[^}]+})', html)
                    if js_match:
                        try:
                            video_data = json.loads(js_match.group(1))
                            video_url = video_data.get('playAddr') or video_data.get('downloadAddr')
                            self.print_debug("Found video URL in JavaScript data")
                        except:
                            pass

                if not video_url:
                    self.print_debug("Could not find video URL in page")
                    return False

                # Special headers for video download
                video_headers = {
                    **headers,
                    'Accept': 'video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7',
                    'Referer': video_info['url']
                }

                # Download the video
                async with session.get(video_url, headers=video_headers) as video_response:
                    if video_response.status == 200:
                        content_type = video_response.headers.get('Content-Type', '')
                        if not any(t in content_type.lower() for t in ['video', 'octet-stream']):
                            self.print_debug(f"Invalid content type: {content_type}")
                            return False

                        filepath = os.path.join(save_path, filename)
                        with open(filepath, 'wb') as f:
                            async for chunk in video_response.content.iter_chunked(8192):
                                f.write(chunk)
                        
                        # Verify file size
                        if os.path.getsize(filepath) < 10000:  # Less than 10KB is probably not a valid video
                            os.remove(filepath)
                            self.print_debug(f"Downloaded file too small")
                            return False
                        
                        self.print_debug(f"Successfully downloaded: {filename}")
                        return True
                    else:
                        self.print_debug(f"Failed to download video: {video_response.status}")
                        return False
            
        except Exception as e:
            self.print_debug(f"Error downloading video: {str(e)}")
            return False

    async def download_all_videos(self, profile_url: str):
        """Download all videos from a profile"""
        console = Console()
        username = profile_url.split('@')[1].split('/')[0]
        save_path = os.path.join(self.download_path, username)
        os.makedirs(save_path, exist_ok=True)
        
        with console.status("[bold blue]Finding videos...", spinner="dots") as status:
            async with async_playwright() as p:
                browser = await p.chromium.launch_persistent_context(
                    user_data_dir=os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
                    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    headless=False,
                    args=['--disable-blink-features=AutomationControlled']
                )
                
                page = await browser.new_page()
                videos = await self.get_video_urls(page, profile_url)
                await browser.close()
        
        if not videos:
            console.print("[red]No videos found[/red]")
            return
        
        total_videos = len(videos)
        console.print(f"[green]Found {total_videos} videos[/green]")
        console.print("\n[bold cyan]Starting downloads...[/bold cyan]")
        
        downloaded = 0
        failed = 0
        
        async with aiohttp.ClientSession() as session:
            tasks = []
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                download_progress = progress.add_task("", total=total_videos)
                
                for index, video in enumerate(videos, 1):
                    progress.update(
                        download_progress,
                        description=f"[yellow]Downloading video {index}/{total_videos}[/yellow]"
                    )
                    
                    if len(tasks) >= 3:
                        completed = await asyncio.gather(*tasks)
                        for success in completed:
                            if success:
                                downloaded += 1
                                progress.update(download_progress, advance=1)
                            else:
                                failed += 1
                        tasks = []
                        await asyncio.sleep(1)
                    
                    tasks.append(asyncio.create_task(
                        self.download_video(session, video, save_path, f"video_{index}.mp4")
                    ))
                
                if tasks:
                    completed = await asyncio.gather(*tasks)
                    for success in completed:
                        if success:
                            downloaded += 1
                            progress.update(download_progress, advance=1)
                        else:
                            failed += 1
        
        console.print(f"\n[bold green]Download Complete![/bold green]")



async def main():
    console = Console()
    console.print(Panel.fit(
        "[bold cyan]TikTok Video Downloader[/bold cyan]",
        title="Welcome",
        border_style="blue"
    ))
    
    profile_url = input("\nEnter TikTok profile URL: ")
    await TikTokDownloader().download_all_videos(profile_url)

if __name__ == "__main__":
    asyncio.run(main())