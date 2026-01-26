#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

BASE_URL="http://localhost:3021"

echo "=================================="
echo "TikTok Downloader API Test Suite"
echo "=================================="
echo ""

# Test 1: Health Check
echo -e "${YELLOW}Test 1: Health Check${NC}"
response=$(curl -s "$BASE_URL/health")
if echo "$response" | grep -q '"status":"ok"'; then
    echo -e "${GREEN}✓ Health check passed${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ Health check failed${NC}"
    echo "$response"
fi
echo ""

# Test 2: Regular Video
echo -e "${YELLOW}Test 2: Regular Video (Whitehouse)${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}')

if echo "$response" | grep -q '"status":"tunnel"'; then
    echo -e "${GREEN}✓ Video metadata fetched${NC}"
    echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(json.dumps({'status': data['status'], 'title': data['title'][:50] + '...', 'views': data['statistics']['play_count'], 'download_links': list(data['download_link'].keys())}, indent=2))"
else
    echo -e "${RED}✗ Video fetch failed${NC}"
    echo "$response"
fi
echo ""

# Test 3: Photo Slideshow
echo -e "${YELLOW}Test 3: Photo Slideshow${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392"}')

if echo "$response" | grep -q '"status":"picker"'; then
    echo -e "${GREEN}✓ Photo slideshow metadata fetched${NC}"
    echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(json.dumps({'status': data['status'], 'photos_count': len(data['photos']), 'has_audio': 'mp3' in data['download_link']}, indent=2))"
else
    echo -e "${RED}✗ Photo slideshow fetch failed${NC}"
    echo "$response"
fi
echo ""

# Test 4: Error Handling - No URL
echo -e "${YELLOW}Test 4: Error Handling - No URL${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{}')

if echo "$response" | grep -q '"error"'; then
    echo -e "${GREEN}✓ Error handling works${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ Error handling failed${NC}"
    echo "$response"
fi
echo ""

# Test 5: Error Handling - Invalid URL
echo -e "${YELLOW}Test 5: Error Handling - Invalid URL${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://youtube.com/watch?v=123"}')

if echo "$response" | grep -q '"error"'; then
    echo -e "${GREEN}✓ Invalid URL rejected${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ Invalid URL not rejected${NC}"
    echo "$response"
fi
echo ""

# Test 6: Download Link Encryption
echo -e "${YELLOW}Test 6: Download Link Encryption${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}')

download_link=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data['download_link'].get('watermark', ''))")

if [[ $download_link == *"/stream?data="* ]]; then
    echo -e "${GREEN}✓ Download links are encrypted${NC}"
    echo "Sample link: ${download_link:0:80}..."
else
    echo -e "${RED}✗ Download links not encrypted${NC}"
    echo "$download_link"
fi
echo ""

# Test 7: 404 Handler
echo -e "${YELLOW}Test 7: 404 Handler${NC}"
response=$(curl -s "$BASE_URL/nonexistent")
if echo "$response" | grep -q '"error":"Route not found"'; then
    echo -e "${GREEN}✓ 404 handler works${NC}"
    echo "$response" | python3 -m json.tool
else
    echo -e "${RED}✗ 404 handler failed${NC}"
    echo "$response"
fi
echo ""

# Test 8: CORS Headers
echo -e "${YELLOW}Test 8: CORS Headers${NC}"
response=$(curl -s -I "$BASE_URL/health")
if echo "$response" | grep -q "access-control-allow-origin"; then
    echo -e "${GREEN}✓ CORS headers present${NC}"
    echo "$response" | grep -i "access-control"
else
    echo -e "${RED}✗ CORS headers missing${NC}"
fi
echo ""

# Test 9: Slideshow Link Generation
echo -e "${YELLOW}Test 9: Slideshow Link Generation${NC}"
response=$(curl -s -X POST "$BASE_URL/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url": "https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392"}')

slideshow_link=$(echo "$response" | python3 -c "import sys, json; data = json.load(sys.stdin); print(data.get('download_slideshow_link', ''))")

if [[ $slideshow_link == *"/download-slideshow?url="* ]]; then
    echo -e "${GREEN}✓ Slideshow link generated${NC}"
    echo "Sample link: ${slideshow_link:0:80}..."
else
    echo -e "${RED}✗ Slideshow link not generated${NC}"
    echo "$slideshow_link"
fi
echo ""

# Summary
echo "=================================="
echo -e "${GREEN}Test Suite Complete${NC}"
echo "=================================="
echo ""
echo "Note: To test actual downloads, use:"
echo "  # Video download"
echo "  curl -o test.mp4 '<download_link_from_test_2>'"
echo ""
echo "  # Slideshow download (takes ~10-30 seconds)"
echo "  curl -o slideshow.mp4 '<slideshow_link_from_test_9>'"
echo ""
