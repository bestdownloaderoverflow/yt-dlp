#!/usr/bin/env python3
"""
Example usage of the TikTok/X Downloader API
"""

import requests
import json

BASE_URL = "http://localhost:8000"


def test_api():
    """Test the API with sample URLs"""
    
    # Test URLs
    test_urls = [
        {
            "name": "X (Twitter) - Windsurf",
            "url": "https://x.com/windsurf/status/2017679700630659449"
        },
        {
            "name": "X (Twitter) - Julian Goldie",
            "url": "https://x.com/JulianGoldieSEO/status/2017655446618915041?s=20"
        }
    ]
    
    for test in test_urls:
        print(f"\n{'='*70}")
        print(f"Testing: {test['name']}")
        print(f"URL: {test['url']}")
        print('='*70)
        
        try:
            response = requests.post(
                f"{BASE_URL}/download",
                json={"url": test['url']},
                timeout=60
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('success'):
                    video_data = data['data']
                    print(f"\n✅ SUCCESS!")
                    print(f"\n📹 Video Info:")
                    print(f"  Platform: {video_data['platform']}")
                    print(f"  ID: {video_data['video_id']}")
                    print(f"  Title: {video_data['title'][:70]}...")
                    print(f"  Author: {video_data['author_name']} (@{video_data['author_username']})")
                    print(f"  Duration: {video_data['duration_formatted']}")
                    print(f"  Stats: {video_data['stats']}")
                    
                    print(f"\n📥 Available Formats:")
                    print(f"  Video: {len(data['video_formats'])} formats")
                    for vf in data['video_formats'][:3]:
                        print(f"    - {vf['quality']}: {vf['url'][:60]}...")
                    
                    print(f"  Audio: {len(data['audio_formats'])} formats")
                    for af in data['audio_formats'][:2]:
                        print(f"    - {af['quality']}: {af['url'][:60]}...")
                    
                    print(f"\n⭐ Best URLs:")
                    print(f"  Video: {data['best_video_url'][:70]}...")
                    if data['best_audio_url']:
                        print(f"  Audio: {data['best_audio_url'][:70]}...")
                else:
                    print(f"\n❌ API returned error: {data.get('message')}")
            else:
                print(f"\n❌ HTTP Error {response.status_code}: {response.text}")
                
        except Exception as e:
            print(f"\n❌ Request failed: {e}")


def download_video(url: str, output_path: str = "video.mp4"):
    """
    Download video directly using the best URL from API
    """
    print(f"Extracting video info...")
    
    response = requests.post(
        f"{BASE_URL}/download",
        json={"url": url},
        timeout=60
    )
    
    if response.status_code != 200:
        print(f"Failed to extract: {response.text}")
        return
    
    data = response.json()
    
    if not data.get('success') or not data.get('best_video_url'):
        print("No video URL found")
        return
    
    video_url = data['best_video_url']
    video_info = data['data']
    
    print(f"Downloading: {video_info['title']}")
    print(f"From: {video_url[:80]}...")
    
    # Download the video
    video_response = requests.get(video_url, stream=True, timeout=120)
    video_response.raise_for_status()
    
    with open(output_path, 'wb') as f:
        for chunk in video_response.iter_content(chunk_size=8192):
            f.write(chunk)
    
    print(f"✅ Downloaded to: {output_path}")
    print(f"   Size: {len(video_response.content) / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        # Download mode: python example_usage.py download <url> [output]
        if len(sys.argv) < 3:
            print("Usage: python example_usage.py download <url> [output.mp4]")
            sys.exit(1)
        
        url = sys.argv[2]
        output = sys.argv[3] if len(sys.argv) > 3 else "video.mp4"
        download_video(url, output)
    else:
        # Test mode
        test_api()
