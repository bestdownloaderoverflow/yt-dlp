#!/bin/bash

# Test script for TikTok Downloader API (Python)

set -e

BASE_URL="${BASE_URL:-http://localhost:3021}"
TEST_URL="https://vt.tiktok.com/ZSaPXyDuw/"

echo "🧪 Testing TikTok Downloader API (Python)"
echo "=========================================="
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Health Check
echo -n "1️⃣  Testing /health endpoint... "
HEALTH=$(curl -s -w "\n%{http_code}" "${BASE_URL}/health")
HTTP_CODE=$(echo "$HEALTH" | tail -n1)
BODY=$(echo "$HEALTH" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ PASS${NC}"
    echo "   Response: $BODY"
else
    echo -e "${RED}❌ FAIL${NC} (HTTP $HTTP_CODE)"
    exit 1
fi
echo ""

# Test 2: TikTok Video Extraction
echo -n "2️⃣  Testing /tiktok endpoint... "
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/tiktok" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"${TEST_URL}\"}")

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    STATUS=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', 'unknown'))" 2>/dev/null || echo "error")
    
    if [ "$STATUS" = "tunnel" ] || [ "$STATUS" = "picker" ]; then
        echo -e "${GREEN}✅ PASS${NC}"
        echo "   Status: $STATUS"
        
        # Extract some metadata
        TITLE=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('title', 'N/A')[:50])" 2>/dev/null || echo "N/A")
        AUTHOR=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('author', {}).get('nickname', 'N/A'))" 2>/dev/null || echo "N/A")
        
        echo "   Title: $TITLE"
        echo "   Author: $AUTHOR"
    else
        echo -e "${YELLOW}⚠️  WARNING${NC} - Unexpected status: $STATUS"
    fi
else
    echo -e "${RED}❌ FAIL${NC} (HTTP $HTTP_CODE)"
    echo "   Response: $BODY"
    exit 1
fi
echo ""

# Test 3: Invalid URL
echo -n "3️⃣  Testing invalid URL handling... "
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/tiktok" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://invalid.com/video"}')

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "400" ]; then
    echo -e "${GREEN}✅ PASS${NC}"
    echo "   Correctly rejected invalid URL"
else
    echo -e "${YELLOW}⚠️  WARNING${NC} - Expected 400, got $HTTP_CODE"
fi
echo ""

# Test 4: Missing URL
echo -n "4️⃣  Testing missing URL handling... "
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/tiktok" \
    -H "Content-Type: application/json" \
    -d '{}')

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "422" ] || [ "$HTTP_CODE" = "400" ]; then
    echo -e "${GREEN}✅ PASS${NC}"
    echo "   Correctly rejected missing URL"
else
    echo -e "${YELLOW}⚠️  WARNING${NC} - Expected 422 or 400, got $HTTP_CODE"
fi
echo ""

# Test 5: CORS Headers
echo -n "5️⃣  Testing CORS headers... "
CORS=$(curl -s -I -X OPTIONS "${BASE_URL}/tiktok" \
    -H "Origin: http://example.com" \
    -H "Access-Control-Request-Method: POST")

if echo "$CORS" | grep -q "Access-Control-Allow-Origin"; then
    echo -e "${GREEN}✅ PASS${NC}"
    echo "   CORS enabled"
else
    echo -e "${RED}❌ FAIL${NC}"
    echo "   CORS headers not found"
fi
echo ""

# Summary
echo "=========================================="
echo -e "${GREEN}🎉 All tests completed!${NC}"
echo ""
echo "📝 API Endpoints:"
echo "   POST ${BASE_URL}/tiktok"
echo "   GET  ${BASE_URL}/download"
echo "   GET  ${BASE_URL}/download-slideshow"
echo "   GET  ${BASE_URL}/stream"
echo "   GET  ${BASE_URL}/health"
echo ""
echo "📊 Performance Info:"
curl -s "${BASE_URL}/health" | python3 -m json.tool 2>/dev/null || echo "   (Unable to format)"
echo ""
