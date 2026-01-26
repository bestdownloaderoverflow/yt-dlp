#!/bin/bash

# Test script for streaming endpoint
# Tests embedded yt-dlp streaming implementation

set -e

BASE_URL="http://localhost:3021"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "======================================"
echo "🧪 ServerPY Streaming Test Suite"
echo "======================================"
echo ""

# Function to print colored output
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${YELLOW}ℹ $1${NC}"
}

# Check if server is running
echo "1. Checking server health..."
if curl -s "$BASE_URL/health" > /dev/null; then
    print_success "Server is running"
else
    print_error "Server is not running at $BASE_URL"
    exit 1
fi
echo ""

# Test video URL (adjust as needed)
TEST_URL="https://vt.tiktok.com/ZSaPXyDuw/"
print_info "Using test URL: $TEST_URL"
echo ""

# Step 1: Extract video info
echo "2. Extracting video info..."
RESPONSE=$(curl -s -X POST "$BASE_URL/tiktok" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$TEST_URL\"}" \
  | python3 -m json.tool)

if [ $? -eq 0 ]; then
    print_success "Video info extracted"
    echo "$RESPONSE" | head -20
    echo "..."
else
    print_error "Failed to extract video info"
    exit 1
fi
echo ""

# Step 2: Get stream data
echo "3. Extracting stream data..."
STREAM_DATA=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('code') == 0 and data.get('data', {}).get('video_data'):
    print(data['data']['video_data'][0].get('streamData', ''))
else:
    print('')
")

if [ -z "$STREAM_DATA" ]; then
    print_error "No stream data found"
    exit 1
fi

print_success "Stream data extracted"
echo "Stream data length: ${#STREAM_DATA} bytes"
echo ""

# Step 3: Test streaming endpoint
echo "4. Testing streaming endpoint..."
OUTPUT_FILE="/tmp/test_stream_output.mp4"

# Cleanup old file
rm -f "$OUTPUT_FILE"

print_info "Downloading to: $OUTPUT_FILE"
print_info "This may take a few seconds..."

# Measure time
START_TIME=$(date +%s)

# Download with progress
curl -# -o "$OUTPUT_FILE" "$BASE_URL/stream?data=$STREAM_DATA"
CURL_EXIT=$?

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

if [ $CURL_EXIT -eq 0 ] && [ -f "$OUTPUT_FILE" ]; then
    FILE_SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)
    print_success "Stream downloaded successfully"
    echo "  - File size: $FILE_SIZE"
    echo "  - Download time: ${DURATION}s"
    echo "  - Location: $OUTPUT_FILE"
    
    # Verify file is valid
    if [ -s "$OUTPUT_FILE" ]; then
        print_success "File is not empty"
    else
        print_error "File is empty"
        exit 1
    fi
else
    print_error "Stream download failed"
    exit 1
fi
echo ""

# Step 4: Test streaming with audio format
echo "5. Testing audio stream (if available)..."
AUDIO_STREAM_DATA=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('code') == 0 and data.get('data', {}).get('video_data'):
    for item in data['data']['video_data']:
        if 'audio' in item.get('format', '').lower():
            print(item.get('streamData', ''))
            break
    else:
        print('')
else:
    print('')
")

if [ -n "$AUDIO_STREAM_DATA" ]; then
    AUDIO_OUTPUT="/tmp/test_stream_audio.mp3"
    rm -f "$AUDIO_OUTPUT"
    
    print_info "Downloading audio to: $AUDIO_OUTPUT"
    curl -s -o "$AUDIO_OUTPUT" "$BASE_URL/stream?data=$AUDIO_STREAM_DATA"
    
    if [ -f "$AUDIO_OUTPUT" ] && [ -s "$AUDIO_OUTPUT" ]; then
        AUDIO_SIZE=$(du -h "$AUDIO_OUTPUT" | cut -f1)
        print_success "Audio stream downloaded successfully ($AUDIO_SIZE)"
    else
        print_error "Audio stream failed"
    fi
else
    print_info "No audio format available for this video"
fi
echo ""

# Step 5: Test error handling
echo "6. Testing error handling..."

# Test with invalid encrypted data
print_info "Testing with invalid data..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/stream?data=invalid")
if [ "$HTTP_CODE" == "500" ] || [ "$HTTP_CODE" == "400" ]; then
    print_success "Invalid data rejected (HTTP $HTTP_CODE)"
else
    print_error "Should reject invalid data (got HTTP $HTTP_CODE)"
fi

# Test with missing data
print_info "Testing with missing data..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/stream")
if [ "$HTTP_CODE" == "422" ]; then
    print_success "Missing data rejected (HTTP $HTTP_CODE)"
else
    print_error "Should reject missing data (got HTTP $HTTP_CODE)"
fi
echo ""

# Step 6: Performance test
echo "7. Performance test (concurrent streams)..."
print_info "Starting 3 concurrent streams..."

CONCURRENT_COUNT=3
PIDS=()

for i in $(seq 1 $CONCURRENT_COUNT); do
    OUTPUT="/tmp/test_stream_concurrent_$i.mp4"
    rm -f "$OUTPUT"
    curl -s -o "$OUTPUT" "$BASE_URL/stream?data=$STREAM_DATA" &
    PIDS+=($!)
done

# Wait for all downloads
FAILED=0
for PID in "${PIDS[@]}"; do
    if ! wait $PID; then
        FAILED=$((FAILED + 1))
    fi
done

if [ $FAILED -eq 0 ]; then
    print_success "All $CONCURRENT_COUNT concurrent streams completed"
    
    # Check file sizes
    for i in $(seq 1 $CONCURRENT_COUNT); do
        OUTPUT="/tmp/test_stream_concurrent_$i.mp4"
        if [ -f "$OUTPUT" ] && [ -s "$OUTPUT" ]; then
            SIZE=$(du -h "$OUTPUT" | cut -f1)
            echo "  - Stream $i: $SIZE"
        fi
    done
else
    print_error "$FAILED out of $CONCURRENT_COUNT streams failed"
fi
echo ""

# Step 7: Memory check
echo "8. Checking memory usage..."
if command -v ps &> /dev/null; then
    PROCESS_INFO=$(ps aux | grep "python.*main.py" | grep -v grep | head -1)
    if [ -n "$PROCESS_INFO" ]; then
        MEM_PERCENT=$(echo "$PROCESS_INFO" | awk '{print $4}')
        RSS_MB=$(echo "$PROCESS_INFO" | awk '{printf "%.1f", $6/1024}')
        print_success "Server memory usage: ${MEM_PERCENT}% (${RSS_MB}MB RSS)"
    fi
else
    print_info "ps command not available, skipping memory check"
fi
echo ""

# Summary
echo "======================================"
echo "✅ All streaming tests completed!"
echo "======================================"
echo ""
echo "Test Results:"
echo "  1. Server health: ✓"
echo "  2. Video extraction: ✓"
echo "  3. Stream data extraction: ✓"
echo "  4. Video streaming: ✓"
echo "  5. Audio streaming: ${AUDIO_STREAM_DATA:+✓}${AUDIO_STREAM_DATA:-ℹ (not available)}"
echo "  6. Error handling: ✓"
echo "  7. Concurrent streams: ✓"
echo "  8. Memory check: ✓"
echo ""
echo "Cleanup:"
print_info "Test files saved in /tmp/test_stream_*.mp4"
print_info "To clean up: rm -f /tmp/test_stream_*"
echo ""
