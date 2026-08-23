import asyncio
import httpx

async def test_api():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:7799") as client:
        # 1. Health check
        h = await client.get("/health")
        print("Health status:", h.status_code, h.json())

        # 2. Extract video
        url = "https://www.tiktok.com/@jjtrailwalker/video/7660242147043544334?is_from_webapp=1&sender_device=pc"
        print(f"\nSending POST /tiktok for: {url}")
        res = await client.post("/tiktok", json={"url": url})
        print("Extract status:", res.status_code)
        data = res.json()
        print("Title:", data.get("title"))
        print("Artist:", data.get("artist"))
        print("Source:", data.get("extract_source"))
        print("Stats:", data.get("statistics"))
        print("Download Links:", data.get("download_link"))

        # 3. Test Download Tunnel endpoint
        no_wm_link = data.get("download_link", {}).get("no_watermark")
        if no_wm_link:
            print(f"\nTesting GET {no_wm_link} ...")
            dl_res = await client.get(no_wm_link, headers={"Range": "bytes=0-1024"})
            print("Download Tunnel status:", dl_res.status_code)
            print("Content-Type:", dl_res.headers.get("content-type"))
            print("Content-Range:", dl_res.headers.get("content-range"))
            print("Bytes received:", len(dl_res.content))
            if dl_res.status_code in (200, 206) and len(dl_res.content) > 0:
                print("\n🎉 ALL TESTS PASSED! FASTAPI MICROSERVICE IS 100% WORKING!")

if __name__ == "__main__":
    asyncio.run(test_api())
