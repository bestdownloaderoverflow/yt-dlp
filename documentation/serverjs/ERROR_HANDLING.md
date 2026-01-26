# Error Handling Documentation

## ✅ Error Handling Improvements

Server sekarang memiliki error handling yang lebih baik dengan pesan error yang user-friendly.

---

## 🧪 Test Results

### Test 1: Invalid Video URL ✅

**Request:**
```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@ido.tadmor.shayaveneta/video/sjjdjs"}'
```

**Response:**
```json
{
  "error": "Video not found. Please check the URL and make sure the video exists."
}
```

**Status Code:** `404` ✅

---

### Test 2: Missing URL Parameter ✅

**Request:**
```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{
  "error": "URL parameter is required"
}
```

**Status Code:** `400` ✅

---

### Test 3: Non-TikTok URL ✅

**Request:**
```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=123"}'
```

**Response:**
```json
{
  "error": "Only TikTok and Douyin URLs are supported"
}
```

**Status Code:** `400` ✅

---

## 📋 Error Types & Responses

### 1. Invalid/Non-existent Video

**Trigger:**
- Video ID doesn't exist
- Video was deleted
- Invalid video format

**Response:**
```json
{
  "error": "Video not found. Please check the URL and make sure the video exists."
}
```

**Status Code:** `404`

**Frontend Display:**
- Shows error message to user
- Allows retry with different URL

---

### 2. Missing URL Parameter

**Trigger:**
- Request body missing `url` field
- Empty request body

**Response:**
```json
{
  "error": "URL parameter is required"
}
```

**Status Code:** `400`

**Frontend Display:**
- Validation error
- Form validation should catch this

---

### 3. Non-TikTok URL

**Trigger:**
- URL doesn't contain `tiktok.com` or `douyin.com`
- Wrong platform URL

**Response:**
```json
{
  "error": "Only TikTok and Douyin URLs are supported"
}
```

**Status Code:** `400`

**Frontend Display:**
- Validation error
- Form validation should catch this

---

### 4. IP Blocked / Access Denied

**Trigger:**
- TikTok blocks server IP
- Video is private/restricted
- Rate limiting

**Response:**
```json
{
  "error": "Access denied. The video may be private or restricted."
}
```

**Status Code:** `403`

**Frontend Display:**
- Shows access denied message
- Suggests video may be private

---

### 5. Generic yt-dlp Errors

**Trigger:**
- Network issues
- yt-dlp extraction failures
- Unknown errors

**Response:**
```json
{
  "error": "Extracted error message from yt-dlp"
}
```

**Status Code:** `500`

**Frontend Display:**
- Shows error message
- Allows retry

---

## 🔧 Implementation

### Error Handling Code

```javascript
app.post('/tiktok', async (req, res) => {
  try {
    const { url } = req.body;

    // Validation
    if (!url) {
      return res.status(400).json({ error: 'URL parameter is required' });
    }

    if (!url.includes('tiktok.com') && !url.includes('douyin.com')) {
      return res.status(400).json({ error: 'Only TikTok and Douyin URLs are supported' });
    }

    // Process request
    const data = await fetchTikTokData(url);
    const response = generateJsonResponse(data, url);
    return res.status(200).json(response);

  } catch (error) {
    console.error('Error in TikTok handler:', error);
    
    // Provide user-friendly error messages
    let errorMessage = error.message || 'An error occurred processing the request';
    let statusCode = 500;
    
    // Handle specific error cases
    if (errorMessage.includes('Unsupported URL') || 
        errorMessage.includes('Unable to download webpage')) {
      statusCode = 404;
      errorMessage = 'Video not found. Please check the URL and make sure the video exists.';
    } else if (errorMessage.includes('IP address is blocked')) {
      statusCode = 403;
      errorMessage = 'Access denied. The video may be private or restricted.';
    } else if (errorMessage.includes('yt-dlp failed')) {
      // Extract cleaner error message from yt-dlp output
      const match = errorMessage.match(/ERROR: (.+)/);
      if (match) {
        errorMessage = match[1];
      }
    }
    
    return res.status(statusCode).json({ error: errorMessage });
  }
});
```

---

## 🎯 Error Message Mapping

| yt-dlp Error | Detected Pattern | User-Friendly Message | Status Code |
|--------------|------------------|----------------------|-------------|
| `Unsupported URL` | `includes('Unsupported URL')` | "Video not found..." | 404 |
| `Unable to download webpage` | `includes('Unable to download')` | "Video not found..." | 404 |
| `IP address is blocked` | `includes('IP address is blocked')` | "Access denied..." | 403 |
| `yt-dlp failed: ERROR: ...` | `includes('yt-dlp failed')` | Extracted error message | 500 |
| Other errors | Default | Original message | 500 |

---

## 📊 Frontend Integration

### Error Response Format

**Standard Format:**
```json
{
  "error": "User-friendly error message"
}
```

**Frontend Handling:**
```javascript
// app/api/tiktok/route.js
try {
  const data = await ky.post(process.env.API_URL + '/tiktok', {
    json: { url: tiktokUrl }
  }).json();
  
  return NextResponse.json(data);
  
} catch (error) {
  if (error.name === 'HTTPError') {
    const errorData = await error.response.json();
    return NextResponse.json(
      { error: errorData.error || 'Failed to fetch TikTok data' },
      { status: error.response.status }
    );
  }
}
```

**Display in UI:**
```javascript
// app/components/DownloadCard.js
{error && (
  <div className="p-4 mb-4 rounded-lg bg-red-100 dark:bg-red-900/30 border border-red-200 dark:border-red-700 text-red-700 dark:text-red-200">
    {error}
  </div>
)}
```

---

## ✅ Error Handling Checklist

- [x] ✅ Invalid video URL → 404 with friendly message
- [x] ✅ Missing URL parameter → 400 with clear message
- [x] ✅ Non-TikTok URL → 400 with platform restriction message
- [x] ✅ IP blocked → 403 with access denied message
- [x] ✅ Generic errors → 500 with extracted error message
- [x] ✅ All errors return JSON format
- [x] ✅ Appropriate HTTP status codes
- [x] ✅ User-friendly error messages
- [x] ✅ Frontend compatible format

---

## 🧪 Testing Guide

### Manual Testing

```bash
# Test 1: Invalid URL
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@user/video/invalid"}'

# Expected: 404 with "Video not found" message

# Test 2: Missing URL
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{}'

# Expected: 400 with "URL parameter is required"

# Test 3: Non-TikTok URL
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtube.com/watch?v=123"}'

# Expected: 400 with "Only TikTok and Douyin URLs are supported"

# Test 4: Valid URL (should work)
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}'

# Expected: 200 with video metadata
```

---

## 🎉 Summary

### Before Improvements

**Error Response:**
```json
{
  "error": "yt-dlp failed: WARNING: [generic] Falling back on generic information extractor\nERROR: Unsupported URL: https://www.tiktok.com/@user/video/invalid\n"
}
```

**Issues:**
- ❌ Technical error messages
- ❌ Hard to understand for users
- ❌ Includes warnings and technical details
- ❌ Not user-friendly

### After Improvements

**Error Response:**
```json
{
  "error": "Video not found. Please check the URL and make sure the video exists."
}
```

**Benefits:**
- ✅ User-friendly messages
- ✅ Clear and actionable
- ✅ Appropriate HTTP status codes
- ✅ Frontend compatible
- ✅ Better UX

---

## 📝 Best Practices

### 1. Always Return JSON

```javascript
// ✅ Good
return res.status(404).json({ error: 'Video not found' });

// ❌ Bad
return res.status(404).send('Video not found');
```

### 2. Use Appropriate Status Codes

- `400` - Bad Request (missing/invalid parameters)
- `403` - Forbidden (access denied)
- `404` - Not Found (video doesn't exist)
- `500` - Internal Server Error (unexpected errors)

### 3. Provide Clear Messages

```javascript
// ✅ Good
"Video not found. Please check the URL and make sure the video exists."

// ❌ Bad
"ERROR: Unsupported URL: https://..."
```

### 4. Log Technical Details

```javascript
// Log full error for debugging
console.error('Error in TikTok handler:', error);

// Return user-friendly message
return res.status(404).json({ error: 'Video not found...' });
```

---

## 🚀 Production Ready

**Error handling sekarang:**
- ✅ Comprehensive
- ✅ User-friendly
- ✅ Frontend compatible
- ✅ Properly tested
- ✅ Production ready

**No changes needed!** 🎉
