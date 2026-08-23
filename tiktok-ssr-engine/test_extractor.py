import asyncio
import sys
from extractor import TikTokSSRExtractor

async def main():
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.tiktok.com/@tiktok/video/7311111111111111111"
    print(f"Testing extraction for: {test_url}")
    extractor = TikTokSSRExtractor()
    try:
        data = await extractor.extract(test_url)
        print("Success!")
        print("Title:", data.get("title"))
        print("Author:", data.get("artist"))
        print("Download links:", list(data.get("download_link", {}).keys()))
    except Exception as e:
        print(f"Extraction result / error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
