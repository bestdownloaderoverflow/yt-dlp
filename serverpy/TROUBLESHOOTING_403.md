# Troubleshooting 403 Forbidden Error

## 🔴 Problem

```
403 Client Error: Forbidden for url: https://v16-webapp-prime.tiktok.com/video/...
```

**Cause:** TikTok CDN blocks requests that don't have proper headers or session context.

---

## 🎯 Solutions (Ranked by Effectiveness)

### **Solution 1: Use yt-dlp Direct Download (RECOMMENDED)**

Instead of extracting URL and streaming separately, let yt-dlp handle the entire download process:

```python
def download_with_ytdlp():
    """Let yt-dlp handle download completely"""
    try:
        ydl_opts = {
            'format': stream_data['format_id'],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Download to memory
            import io
            
            # Custom progress hook to stream chunks
            downloaded_bytes = []
            
            def download_hook(d):
                if d['status'] == 'downloading':
                    # Get downloaded data
                    if 'downloaded_bytes' in d:
                        pass  # Track progress
                elif d['status'] == 'finished':
                    # Read the file
                    filename = d['filename']
                    with open(filename, 'rb') as f:
                        while True:
                            chunk = f.read(8192)
                            if not chunk:
                                break
                            chunk_queue.put(chunk)
                    # Clean up
                    os.remove(filename)
                    chunk_queue.put(None)
            
            ydl_opts['progress_hooks'] = [download_hook]
            ydl_opts['outtmpl'] = f'/tmp/temp_stream_{uuid.uuid4()}.mp4'
            
            # This will download and call our hook
            ydl.download([stream_data['url']])
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        download_error['error'] = str(e)
        chunk_queue.put(None)
```

**Pros:**
- ✅ yt-dlp handles all TikTok anti-bot measures
- ✅ No 403 errors
- ✅ Most reliable

**Cons:**
- ⚠️ Downloads to disk first (temp file)
- ⚠️ Slight delay before streaming starts

---

### **Solution 2: Use yt-dlp's Downloader Directly**

Let yt-dlp's internal downloader handle the streaming:

```python
def download_with_ytdlp_downloader():
    """Use yt-dlp's downloader for streaming"""
    try:
        ydl_opts = {
            'format': stream_data['format_id'],
            'quiet': True,
            'no_warnings': True,
            # Use yt-dlp's native downloader
            'external_downloader': None,  # Use yt-dlp's own
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(stream_data['url'], download=False)
            
            # Find format
            requested_format = next(
                (f for f in info.get('formats', []) 
                 if f.get('format_id') == stream_data['format_id']),
                None
            )
            
            if not requested_format:
                raise Exception("Format not found")
            
            # Use yt-dlp to download
            # This maintains proper session and headers
            from yt_dlp.downloader.http import HttpFD
            
            downloader = HttpFD(ydl, {'test': False})
            
            # Download with yt-dlp's downloader
            # It handles all the headers and cookies properly
            video_url = requested_format['url']
            
            # Create custom file-like object that writes to queue
            class QueueWriter:
                def write(self, data):
                    chunk_queue.put(data)
                    return len(data)
                
                def flush(self):
                    pass
            
            # This might not work directly, need adaptation
            # downloader.download(filename, {'url': video_url})
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        download_error['error'] = str(e)
        chunk_queue.put(None)
```

---

### **Solution 3: Proxy Through Node.js/serverjs**

If ServerJS works, proxy through it:

```python
async def stream_via_serverjs(stream_data):
    """Proxy through serverjs which handles TikTok properly"""
    
    # Call serverjs /stream endpoint
    serverjs_url = "http://localhost:3000/stream"
    
    # Encrypt data using same encryption
    from encryption import encrypt
    import json
    
    encrypted = encrypt(json.dumps({
        'url': stream_data['url'],
        'format_id': stream_data['format_id'],
        'author': stream_data['author']
    }), settings.ENCRYPTION_KEY)
    
    # Stream from serverjs
    import httpx
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream('GET', f"{serverjs_url}?data={encrypted}") as response:
            async for chunk in response.aiter_bytes(chunk_size=8192):
                yield chunk
```

**Pros:**
- ✅ Leverages working serverjs implementation
- ✅ No 403 errors

**Cons:**
- ❌ Requires serverjs to be running
- ❌ Extra hop/latency
- ❌ Defeats purpose of serverpy

---

### **Solution 4: Use Download Endpoint Instead**

Don't use `/stream` at all, use `/download` which already works:

```javascript
// Frontend: Use /download instead of /stream
const downloadUrl = `http://localhost:3021/download?data=${downloadData}`;

// This will work because it:
// 1. Downloads with yt-dlp to temp file
// 2. Serves the file
// 3. Cleans up after

// Client gets the file
window.location.href = downloadUrl;
```

**Pros:**
- ✅ Already works
- ✅ No 403 errors
- ✅ Simple

**Cons:**
- ⚠️ Not true streaming (downloads fully first)
- ⚠️ Uses more disk space
- ⚠️ Slower for large files

---

## 🔧 Recommended Implementation

**Best approach: Modify `/stream` to download via yt-dlp first**

This combines reliability with minimal disk usage:

```python
@app.get("/stream")
async def stream_video(data: str, request: Request):
    """Stream video using yt-dlp download"""
    try:
        # ... decrypt and validate ...
        
        import tempfile
        import uuid
        import threading
        from queue import Queue
        
        chunk_queue = Queue(maxsize=20)
        download_error = {'error': None}
        
        def download_and_stream():
            """Download with yt-dlp and stream from file"""
            temp_file = None
            try:
                # Create temp file
                temp_file = tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix='.mp4',
                    dir=settings.TEMP_DIR
                )
                temp_path = temp_file.name
                temp_file.close()
                
                # Download with yt-dlp
                ydl_opts = {
                    'format': stream_data['format_id'],
                    'quiet': True,
                    'no_warnings': True,
                    'noprogress': True,
                    'outtmpl': temp_path,
                }
                
                logger.info(f"Downloading to temp file: {temp_path}")
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([stream_data['url']])
                
                logger.info("Download complete, streaming from file")
                
                # Stream from file to queue
                with open(temp_path, 'rb') as f:
                    while True:
                        chunk = f.read(8192)
                        if not chunk:
                            break
                        chunk_queue.put(chunk)
                
                chunk_queue.put(None)  # End signal
                logger.info("Streaming complete")
                
            except Exception as e:
                logger.error(f"Download error: {e}")
                download_error['error'] = str(e)
                chunk_queue.put(None)
            finally:
                # Clean up temp file
                if temp_file and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger.info(f"Cleaned up temp file: {temp_path}")
                    except:
                        pass
        
        # Start download thread
        thread = threading.Thread(target=download_and_stream, daemon=True)
        thread.start()
        
        async def stream_content():
            """Stream from queue"""
            loop = asyncio.get_event_loop()
            
            while True:
                if await request.is_disconnected():
                    break
                
                try:
                    chunk = await loop.run_in_executor(
                        None,
                        lambda: chunk_queue.get(timeout=30)
                    )
                    
                    if chunk is None:
                        if download_error['error']:
                            logger.error(f"Stream error: {download_error['error']}")
                        break
                    
                    yield chunk
                except:
                    break
        
        ext = 'mp3' if 'audio' in stream_data['format_id'] else 'mp4'
        content_type = 'audio/mpeg' if ext == 'mp3' else 'video/mp4'
        filename = f"{stream_data['author']}.{ext}"
        
        return StreamingResponse(
            stream_content(),
            media_type=content_type,
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'X-Filename': filename,
                'Cache-Control': 'no-cache',
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

**Benefits:**
- ✅ No 403 errors (yt-dlp handles everything)
- ✅ Still streams to client (uses queue)
- ✅ Memory efficient (temp file deleted immediately)
- ✅ Reliable

**Trade-offs:**
- ⚠️ Brief delay while downloading
- ⚠️ Uses temp disk space
- ⚠️ Not "true" streaming (download → stream)

---

## 📊 Comparison

| Method | 403 Risk | Memory | Speed | Reliability |
|--------|----------|--------|-------|-------------|
| **Current (direct URL)** | ❌ High | ✅ Low | ✅ Fast | ❌ Fails |
| **yt-dlp download** | ✅ None | ⚠️ Medium | ⚠️ Medium | ✅ High |
| **Proxy serverjs** | ✅ None | ✅ Low | ⚠️ Slow | ✅ High |
| **Use /download** | ✅ None | ❌ High | ❌ Slow | ✅ High |

---

## 🎯 Final Recommendation

**Implement Solution 4 (yt-dlp download + stream from temp file)**

This is the best balance:
1. No 403 errors
2. Reasonable memory usage
3. Good performance
4. High reliability
5. Client still gets streaming response

---

## 🔧 Quick Fix Commands

```bash
# Test current implementation
curl -I "http://localhost:3021/stream?data=..."

# If 403, check headers
curl -v "http://localhost:3021/stream?data=..."

# Use /download instead (works now)
curl "http://localhost:3021/download?data=..." -o video.mp4
```

---

## 📞 Need Help?

If 403 persists:
1. Use `/download` endpoint (works reliably)
2. Implement yt-dlp download solution above
3. Consider using serverjs for streaming
4. Check if VPN/proxy helps

**Remember:** TikTok actively blocks scrapers. yt-dlp handles this well, so let it do the download!
