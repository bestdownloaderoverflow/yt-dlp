# Slideshow Generation Feature

## Overview

Server dapat mengkonversi TikTok photo posts menjadi video slideshow dengan audio menggunakan ffmpeg.

## How It Works

```
Photo Post URL → yt-dlp → Extract photos & audio → FFmpeg → Slideshow Video
```

### Process Flow:

1. **Fetch Metadata** - yt-dlp mengambil info photo post
2. **Download Photos** - Download semua foto ke temp folder
3. **Download Audio** - Download audio track
4. **Generate Video** - FFmpeg membuat slideshow:
   - Each photo: 4 seconds
   - Resolution: 1080x1920 (portrait)
   - Scaling: Fit with black padding
   - Audio: Looped to match video duration
5. **Stream to Client** - Video di-stream langsung
6. **Cleanup** - Temp files dihapus otomatis

## Technical Specifications

### Video Settings
- **Resolution**: 1080x1920 (portrait, TikTok format)
- **Duration per image**: 4 seconds
- **Codec**: H.264 (libx264)
- **Pixel format**: yuv420p
- **Frame rate**: CFR (Constant Frame Rate)

### Audio Settings
- **Format**: MP3
- **Loop**: Yes (to match video duration)
- **Trim**: Trimmed to exact video duration

### Image Processing
- **Scaling**: Fit within 1080x1920
- **Padding**: Black bars if aspect ratio doesn't match
- **SAR**: Set to 1:1

## API Usage

### 1. Get Photo Post Metadata

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@username/photo/123"}'
```

**Response:**
```json
{
  "status": "picker",
  "photos": [
    {"type": "photo", "url": "..."},
    {"type": "photo", "url": "..."}
  ],
  "download_link": {
    "no_watermark": ["...", "..."],
    "mp3": "..."
  },
  "download_slideshow_link": "http://localhost:3021/download-slideshow?url=<encrypted>"
}
```

### 2. Download Slideshow

```bash
curl -o slideshow.mp4 "http://localhost:3021/download-slideshow?url=<encrypted>"
```

## JavaScript Example

```javascript
async function downloadSlideshow(tiktokUrl) {
  try {
    // Get metadata
    const response = await fetch('http://localhost:3021/tiktok', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: tiktokUrl })
    });
    
    const data = await response.json();
    
    if (data.status === 'picker') {
      // Photo post detected
      console.log(`Found ${data.photos.length} photos`);
      
      // Download slideshow
      window.location.href = data.download_slideshow_link;
      
      // Or download individual photos
      // data.download_link.no_watermark.forEach((url, i) => {
      //   window.location.href = url;
      // });
    }
  } catch (error) {
    console.error('Error:', error);
  }
}

// Usage
downloadSlideshow('https://www.tiktok.com/@username/photo/123');
```

## Performance

### Timing Breakdown

| Step | Duration | Notes |
|------|----------|-------|
| Metadata fetch | ~5-10s | yt-dlp -J |
| Photo downloads | ~2-5s | Parallel downloads |
| Audio download | ~1-2s | Single file |
| FFmpeg processing | ~5-15s | Depends on photo count |
| Streaming | Immediate | Starts right away |
| **Total** | **~13-32s** | Varies by photo count |

### Factors Affecting Speed

1. **Number of photos** - More photos = longer processing
2. **Image resolution** - Higher res = more processing
3. **Network speed** - Download time varies
4. **Server CPU** - FFmpeg processing speed

## Resource Usage

### Disk Space (Temporary)
- Photos: ~500KB - 2MB each
- Audio: ~500KB - 1MB
- Output video: ~2-10MB
- **Total**: ~5-25MB per slideshow

### Memory
- FFmpeg: ~50-200MB during processing
- Node.js: ~50-100MB
- **Total**: ~100-300MB per concurrent slideshow

### CPU
- FFmpeg: High during processing (1-2 cores)
- yt-dlp: Low
- Node.js: Low

## Configuration

### Environment Variables

```env
FFMPEG_PATH=ffmpeg          # Path to ffmpeg binary
TEMP_DIR=./temp             # Temporary files directory
```

### FFmpeg Path Options

```bash
# Use system ffmpeg
FFMPEG_PATH=ffmpeg

# Use specific path
FFMPEG_PATH=/usr/local/bin/ffmpeg

# Use ffmpeg-static (default, included)
# No need to set, automatic
```

## Error Handling

### Common Errors

**1. "Only image posts are supported"**
- URL is not a photo post
- Solution: Check URL is `/photo/` not `/video/`

**2. "No images found"**
- Photo post has no images
- Solution: Verify URL is valid

**3. "Could not find audio URL"**
- Photo post has no audio
- Solution: Not all photo posts have audio

**4. "FFmpeg error"**
- FFmpeg processing failed
- Solution: Check ffmpeg installation

**5. "Slideshow rendering aborted"**
- Client disconnected
- Solution: Normal, cleanup automatic

## Cleanup

### Automatic Cleanup

Temp files dihapus otomatis:
- ✅ After successful stream
- ✅ On client disconnect
- ✅ On error
- ✅ On abort

### Manual Cleanup

```bash
# Clean temp directory
rm -rf serverjs/temp/*
```

## Limitations

1. **Processing Time** - Takes 10-30 seconds
2. **Concurrent Requests** - Limited by server resources
3. **File Size** - Large slideshows may timeout
4. **Audio Required** - Photo post must have audio

## Advanced Usage

### Custom Duration per Image

Edit `index.js`:

```javascript
// Change from 4 seconds to 3 seconds
imagePaths.forEach(imagePath => {
  command.input(imagePath).inputOptions(['-loop 1', '-t 3']); // Changed to 3
});

// Update total duration calculation
const videoDuration = imagePaths.length * 3; // Changed to 3
```

### Custom Resolution

```javascript
// Change from 1080x1920 to 720x1280
filter.push(`[${index}:v]scale=w=720:h=1280:force_original_aspect_ratio=decrease,` +
  `pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v${index}]`);
```

### Custom Codec

```javascript
// Change from H.264 to H.265
command
  .videoCodec('libx265') // Changed from libx264
  .outputOptions([
    '-preset', 'fast',
    '-crf', '28'
  ])
```

## Monitoring

### Check Temp Directory

```bash
# List temp files
ls -lh serverjs/temp/

# Check disk usage
du -sh serverjs/temp/
```

### Server Logs

```bash
# Watch logs
tail -f logs/access.log

# Check for errors
grep "slideshow" logs/error.log
```

## Troubleshooting

### Slideshow Takes Too Long

**Solutions:**
1. Reduce image count
2. Increase server resources
3. Use faster storage
4. Optimize ffmpeg settings

### Out of Disk Space

**Solutions:**
1. Clean temp directory
2. Increase disk space
3. Implement cleanup schedule

### High CPU Usage

**Solutions:**
1. Limit concurrent slideshows
2. Use faster CPU
3. Implement queue system

### Memory Issues

**Solutions:**
1. Limit concurrent requests
2. Increase server memory
3. Optimize ffmpeg settings

## Best Practices

1. ✅ **Implement rate limiting** - Prevent abuse
2. ✅ **Monitor disk space** - Prevent full disk
3. ✅ **Set timeouts** - Prevent hanging requests
4. ✅ **Log errors** - Track issues
5. ✅ **Test with various photo counts** - Ensure stability

## Production Recommendations

### 1. Add Queue System

```javascript
import Queue from 'bull';

const slideshowQueue = new Queue('slideshow', {
  redis: { host: 'localhost', port: 6379 }
});

slideshowQueue.process(async (job) => {
  // Process slideshow
});
```

### 2. Add Progress Updates

```javascript
// WebSocket for progress
io.emit('progress', {
  step: 'downloading',
  progress: 50
});
```

### 3. Add Caching

```javascript
// Cache generated slideshows
const cacheKey = `slideshow_${videoId}`;
const cached = await redis.get(cacheKey);
if (cached) {
  return res.sendFile(cached);
}
```

### 4. Add Cleanup Schedule

```javascript
import cron from 'node-cron';

// Clean temp every hour
cron.schedule('0 * * * *', () => {
  cleanupOldFiles(tempDir);
});
```

## Summary

✅ **Full slideshow generation support**
✅ **Automatic cleanup**
✅ **Error handling**
✅ **Client abort support**
✅ **Production ready**

**Ready to use!** 🎬
