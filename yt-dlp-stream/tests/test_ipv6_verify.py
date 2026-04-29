#!/usr/bin/env python3
"""Test to verify if force_ipv6 actually uses IPv6."""

import asyncio
import aiohttp
import json

GATEWAY_URL = "http://localhost:9111"

async def test_ipv6():
    # URL yang akan return IP address kita
    # Gunakan yt-dlp untuk extract info dari URL yang return IP
    # Atau test langsung ke worker
    
    # Test 1: Tanpa force_ipv6
    print("=== Test WITHOUT force_ipv6 ===")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GATEWAY_URL}/fetch",
            json={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "force_ipv6": False},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            print(f"Status: {resp.status}")
            print(f"Response keys: {list(data.keys())}")
            if "platform" in data:
                print(f"Platform: {data['platform']}")
            if "error" in data:
                print(f"Error: {data['error']}")
            print()

    # Test 2: Dengan force_ipv6
    print("=== Test WITH force_ipv6 ===")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GATEWAY_URL}/fetch",
            json={"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "force_ipv6": True},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            print(f"Status: {resp.status}")
            print(f"Response keys: {list(data.keys())}")
            if "platform" in data:
                print(f"Platform: {data['platform']}")
            if "error" in data:
                print(f"Error: {data['error']}")
            print()

    # Test 3: TikTok tanpa force_ipv6
    print("=== TikTok WITHOUT force_ipv6 ===")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GATEWAY_URL}/tiktok",
            json={"url": "https://www.tiktok.com/@louie.rodrigo3/video/7598105227488693524", "force_ipv6": False},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            print(f"Status: {resp.status}")
            if "status" in data:
                print(f"Response status: {data['status']}")
            if "extract_source" in data:
                print(f"Extract source: {data['extract_source']}")
            if "error" in data:
                print(f"Error: {data['error']}")
            if "detail" in data:
                print(f"Detail: {data['detail'][:200]}")
            print()

    # Test 4: TikTok dengan force_ipv6
    print("=== TikTok WITH force_ipv6 ===")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GATEWAY_URL}/tiktok",
            json={"url": "https://www.tiktok.com/@louie.rodrigo3/video/7598105227488693524", "force_ipv6": True},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            print(f"Status: {resp.status}")
            if "status" in data:
                print(f"Response status: {data['status']}")
            if "extract_source" in data:
                print(f"Extract source: {data['extract_source']}")
            if "error" in data:
                print(f"Error: {data['error']}")
            if "detail" in data:
                print(f"Detail: {data['detail'][:200]}")
            print()

if __name__ == "__main__":
    asyncio.run(test_ipv6())
