import asyncio
from curl_cffi.requests import AsyncSession
from config import load_cookie_string
from extractor import DESKTOP_BROWSER_HEADERS

async def test_web_item_detail():
    item_id = "7660242147043544334"
    url = f"https://www.tiktok.com/api/item/detail/?itemId={item_id}"
    cookie_str = load_cookie_string()
    headers = dict(DESKTOP_BROWSER_HEADERS)
    if cookie_str:
        headers["Cookie"] = cookie_str
    headers["Referer"] = "https://www.tiktok.com/"

    async with AsyncSession(impersonate="chrome120") as s:
        resp = await s.get(url, headers=headers, timeout=10)
        print("Status code:", resp.status_code)
        print("Length:", len(resp.text))
        try:
            data = resp.json()
            print("Status Code from TikTok API:", data.get("statusCode"))
            item = data.get("itemInfo", {}).get("itemStruct")
            if item:
                print("SUCCESS WEB API! Title:", item.get("desc"))
                print("Author:", item.get("author", {}).get("nickname"))
                video = item.get("video", {})
                print("PlayAddr:", video.get("playAddr")[:60] if video.get("playAddr") else "None")
            else:
                print("Item not found in response:", data)
        except Exception as e:
            print("Error parsing JSON:", e, resp.text[:300])

if __name__ == "__main__":
    asyncio.run(test_web_item_detail())
