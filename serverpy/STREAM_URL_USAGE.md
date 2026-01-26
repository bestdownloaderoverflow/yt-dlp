# Stream URL Usage Guide

## ⚠️ Common Issues & Solutions

### Issue: "Invalid or corrupted encrypted data"

**Cause:** URL parameter `data` tidak di-encode dengan benar atau mengandung karakter tambahan.

---

## ✅ Correct Usage

### **Method 1: Using `streamData` from API Response (RECOMMENDED)**

```javascript
// 1. Get video info
const response = await fetch('http://localhost:3021/tiktok', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ url: 'https://vt.tiktok.com/...' })
});

const data = await response.json();

// 2. Get streamData (already encrypted and safe)
const streamData = data.data.video_data[0].streamData;

// 3. Use it directly - NO ENCODING NEEDED!
const streamUrl = `http://localhost:3021/stream?data=${streamData}`;

// 4. Download
window.location.href = streamUrl;
// or
fetch(streamUrl).then(r => r.blob()).then(blob => {
  // handle download
});
```

---

### **Method 2: Manual URL Construction**

If you need to build the URL manually:

```javascript
// CORRECT ✅
const streamUrl = `http://localhost:3021/stream?data=${streamData}`;

// WRONG ❌ - Don't encode already encoded data
const wrongUrl = `http://localhost:3021/stream?data=${encodeURIComponent(streamData)}`;

// WRONG ❌ - Don't add quotes or JSON
const wrongUrl2 = `http://localhost:3021/stream?data="${streamData}"`;
```

---

## 📋 Response Format

### **POST /tiktok Response Structure**

```json
{
  "code": 0,
  "message": "Success",
  "data": {
    "video_data": [
      {
        "url": "original_url",
        "format": "video-480p",
        "format_id": "video-1",
        "quality": "480p",
        "extension": "mp4",
        "streamData": "XkFTS1JZVkNZQRk..."  // ← Use this!
      }
    ]
  }
}
```

**Key Points:**
- `streamData` is **already encrypted and base64 encoded**
- It's **URL-safe** (uses `base64.urlsafe_b64encode`)
- **No additional encoding needed**

---

## 🔧 Implementation Examples

### **JavaScript (Browser)**

```javascript
async function downloadVideo(tiktokUrl) {
  try {
    // 1. Extract video info
    const infoResponse = await fetch('http://localhost:3021/tiktok', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: tiktokUrl })
    });
    
    const info = await infoResponse.json();
    
    if (info.code !== 0) {
      throw new Error(info.message || 'Failed to extract video');
    }
    
    // 2. Get stream data
    const videoData = info.data.video_data[0];
    const streamData = videoData.streamData;
    
    // 3. Build stream URL
    const streamUrl = `http://localhost:3021/stream?data=${streamData}`;
    
    // 4. Download
    const link = document.createElement('a');
    link.href = streamUrl;
    link.download = `${info.data.author || 'video'}.${videoData.extension || 'mp4'}`;
    link.click();
    
  } catch (error) {
    console.error('Download failed:', error);
  }
}

// Usage
downloadVideo('https://vt.tiktok.com/ZSaPXyDuw/');
```

---

### **Python (requests)**

```python
import requests

def download_video(tiktok_url, output_file='video.mp4'):
    # 1. Extract video info
    response = requests.post(
        'http://localhost:3021/tiktok',
        json={'url': tiktok_url}
    )
    
    data = response.json()
    
    if data['code'] != 0:
        raise Exception(data.get('message', 'Failed to extract video'))
    
    # 2. Get stream data
    stream_data = data['data']['video_data'][0]['streamData']
    
    # 3. Stream download
    stream_url = f'http://localhost:3021/stream?data={stream_data}'
    
    with requests.get(stream_url, stream=True) as r:
        r.raise_for_status()
        with open(output_file, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    
    print(f'Downloaded to {output_file}')

# Usage
download_video('https://vt.tiktok.com/ZSaPXyDuw/')
```

---

### **cURL**

```bash
#!/bin/bash

# 1. Extract video info and get streamData
STREAM_DATA=$(curl -s -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url":"https://vt.tiktok.com/ZSaPXyDuw/"}' \
  | jq -r '.data.video_data[0].streamData')

# 2. Download using stream endpoint
curl "http://localhost:3021/stream?data=${STREAM_DATA}" -o video.mp4

echo "Downloaded to video.mp4"
```

---

### **Node.js (axios)**

```javascript
const axios = require('axios');
const fs = require('fs');

async function downloadVideo(tiktokUrl, outputFile = 'video.mp4') {
  try {
    // 1. Extract video info
    const infoResponse = await axios.post('http://localhost:3021/tiktok', {
      url: tiktokUrl
    });
    
    const data = infoResponse.data;
    
    if (data.code !== 0) {
      throw new Error(data.message || 'Failed to extract video');
    }
    
    // 2. Get stream data
    const streamData = data.data.video_data[0].streamData;
    
    // 3. Stream download
    const streamUrl = `http://localhost:3021/stream?data=${streamData}`;
    
    const streamResponse = await axios({
      method: 'get',
      url: streamUrl,
      responseType: 'stream'
    });
    
    // 4. Save to file
    const writer = fs.createWriteStream(outputFile);
    streamResponse.data.pipe(writer);
    
    return new Promise((resolve, reject) => {
      writer.on('finish', () => {
        console.log(`Downloaded to ${outputFile}`);
        resolve();
      });
      writer.on('error', reject);
    });
    
  } catch (error) {
    console.error('Download failed:', error.message);
    throw error;
  }
}

// Usage
downloadVideo('https://vt.tiktok.com/ZSaPXyDuw/')
  .then(() => console.log('Success!'))
  .catch(err => console.error('Error:', err));
```

---

## ❌ Common Mistakes

### **Mistake 1: URL Encoding streamData**

```javascript
// WRONG ❌
const streamUrl = `http://localhost:3021/stream?data=${encodeURIComponent(streamData)}`;
```

**Why wrong:** `streamData` uses `base64.urlsafe_b64encode` which is already URL-safe.

**Correct:**
```javascript
// CORRECT ✅
const streamUrl = `http://localhost:3021/stream?data=${streamData}`;
```

---

### **Mistake 2: Adding Quotes**

```javascript
// WRONG ❌
const streamUrl = `http://localhost:3021/stream?data="${streamData}"`;
```

**Why wrong:** Quotes are not part of the encrypted data.

**Correct:**
```javascript
// CORRECT ✅
const streamUrl = `http://localhost:3021/stream?data=${streamData}`;
```

---

### **Mistake 3: Mixing with JSON**

```javascript
// WRONG ❌
const wrongData = JSON.stringify({ data: streamData });
const streamUrl = `http://localhost:3021/stream?data=${wrongData}`;
```

**Why wrong:** Server expects just the encrypted string, not JSON.

**Correct:**
```javascript
// CORRECT ✅
const streamUrl = `http://localhost:3021/stream?data=${streamData}`;
```

---

### **Mistake 4: Copy-Paste from Browser URL**

If you copy URL from browser console/network tab, it might have artifacts:

```
// Browser might show:
http://localhost:3021/stream?data=XkFTS...%22,%22mp3%22:%22...
                                      ^^^^^^^^^^^^^^^^^^^^
                                      Extra JSON artifacts!
```

**Solution:** Always use `streamData` directly from API response, not from browser URL bar.

---

## 🔍 Debugging

### **Check if streamData is Valid**

```javascript
// Valid streamData characteristics:
// 1. Only contains base64url characters: A-Z, a-z, 0-9, -, _
// 2. No spaces
// 3. No special characters like {, }, [, ], ", '
// 4. Ends with = or == (padding) or no padding

function isValidStreamData(streamData) {
  // Base64url regex
  const base64urlRegex = /^[A-Za-z0-9_-]+(=|==)?$/;
  return base64urlRegex.test(streamData);
}

console.log(isValidStreamData(streamData)); // Should be true
```

---

### **Test with cURL**

```bash
# 1. Get streamData
STREAM_DATA=$(curl -s -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url":"https://vt.tiktok.com/ZSaPXyDuw/"}' \
  | jq -r '.data.video_data[0].streamData')

# 2. Check if valid (should have no special chars except - and _)
echo "Stream data: $STREAM_DATA"
echo "Length: ${#STREAM_DATA}"

# 3. Test download
curl -v "http://localhost:3021/stream?data=${STREAM_DATA}" -o test.mp4

# 4. Check file size
ls -lh test.mp4
```

---

### **Server Logs**

If you get errors, check server logs:

```bash
# Look for these patterns:
# ✅ Good: "Stream started for format video-1"
# ❌ Bad: "Data validation error in stream"
# ❌ Bad: "Decryption failed"
# ❌ Bad: "JSON decode error"

tail -f /path/to/server/logs
```

---

## 🎯 Best Practices

1. **Always use fresh streamData**
   - Don't cache `streamData` - URLs may expire
   - Request new data for each download

2. **Handle errors gracefully**
   ```javascript
   try {
     await downloadVideo(url);
   } catch (error) {
     if (error.response?.status === 400) {
       // Invalid data - get new streamData
       console.log('Stream URL expired, please try again');
     } else {
       console.error('Download failed:', error);
     }
   }
   ```

3. **Don't modify streamData**
   - Use it exactly as received from API
   - No encoding, decoding, or transformations

4. **Check API response**
   ```javascript
   if (response.data.code !== 0) {
     throw new Error(response.data.message);
   }
   ```

---

## 📊 Error Codes

| HTTP Code | Meaning | Solution |
|-----------|---------|----------|
| 400 | Invalid/corrupted data | Get new streamData from `/tiktok` |
| 404 | Format not found | Check format_id in response |
| 500 | Server error | Check server logs, retry |
| 422 | Missing parameter | Include `data` parameter |

---

## 🚀 Production Tips

### **Rate Limiting**

```javascript
// Add delay between requests
async function downloadWithRateLimit(urls) {
  for (const url of urls) {
    await downloadVideo(url);
    await new Promise(resolve => setTimeout(resolve, 1000)); // 1s delay
  }
}
```

### **Retry Logic**

```javascript
async function downloadWithRetry(url, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      await downloadVideo(url);
      return; // Success
    } catch (error) {
      if (i === maxRetries - 1) throw error;
      console.log(`Retry ${i + 1}/${maxRetries}...`);
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
  }
}
```

### **Progress Tracking**

```javascript
async function downloadWithProgress(tiktokUrl) {
  // ... get streamData ...
  
  const response = await fetch(streamUrl);
  const reader = response.body.getReader();
  const contentLength = +response.headers.get('Content-Length');
  
  let receivedLength = 0;
  const chunks = [];
  
  while(true) {
    const {done, value} = await reader.read();
    if (done) break;
    
    chunks.push(value);
    receivedLength += value.length;
    
    const progress = (receivedLength / contentLength * 100).toFixed(2);
    console.log(`Progress: ${progress}%`);
  }
  
  const blob = new Blob(chunks);
  // ... save blob ...
}
```

---

## 📞 Support

If you still get errors:

1. Check this guide for common mistakes
2. Verify streamData format with regex test
3. Test with cURL to isolate issue
4. Check server logs for detailed errors
5. Ensure you're using latest server version

---

**Remember:** Always use `streamData` exactly as received from `/tiktok` endpoint! 🎯
