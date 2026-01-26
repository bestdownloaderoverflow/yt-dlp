# Contoh Penggunaan API

## 1. Basic Usage - Mendapatkan Metadata Video

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}'
```

**Response:**
```json
{
  "status": "tunnel",
  "title": "The Navy's new BATTLESHIP 🇺🇸",
  "description": "...",
  "statistics": {
    "repost_count": 2774,
    "comment_count": 3048,
    "digg_count": 139300,
    "play_count": 2200000
  },
  "download_link": {
    "watermark": "http://localhost:3021/stream?data=...",
    "no_watermark_hd": "http://localhost:3021/stream?data=...",
    "watermark_hd": "http://localhost:3021/stream?data=..."
  }
}
```

## 2. Download Video (Streaming via yt-dlp)

```bash
# Ambil link dari response di atas
curl -o video.mp4 "http://localhost:3021/stream?data=<encrypted_data>"
```

## 3. Photo Slideshow

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392"}'
```

**Response:**
```json
{
  "status": "picker",
  "photos": [
    {
      "type": "photo",
      "url": "https://..."
    }
  ],
  "download_link": {
    "no_watermark": [
      "http://localhost:3021/download?data=...",
      "http://localhost:3021/download?data=..."
    ],
    "mp3": "http://localhost:3021/download?data=..."
  },
  "download_slideshow_link": "http://localhost:3021/download-slideshow?url=..."
}
```

## 3.1. Download Slideshow Video

```bash
# Get slideshow link from previous response
curl -o slideshow.mp4 "http://localhost:3021/download-slideshow?url=<encrypted_url>"
```

**What happens:**
1. Server downloads all photos
2. Server downloads audio
3. FFmpeg creates video (4 seconds per photo)
4. Audio loops to match video duration
5. Video streams to client
6. Temp files cleaned up automatically

## 4. JavaScript/Frontend Integration

```javascript
// Fungsi untuk download TikTok
async function downloadTikTok(url) {
  try {
    // 1. Dapatkan metadata
    const response = await fetch('http://localhost:3021/tiktok', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ url })
    });
    
    const data = await response.json();
    
    if (data.status === 'tunnel') {
      // Video biasa
      console.log('Video Title:', data.title);
      console.log('Views:', data.statistics.play_count);
      
      // Download HD version (streaming dari yt-dlp)
      const downloadUrl = data.download_link.no_watermark_hd || 
                         data.download_link.no_watermark;
      
      // Redirect ke download
      window.location.href = downloadUrl;
      
    } else if (data.status === 'picker') {
      // Photo slideshow
      console.log('Photos:', data.photos.length);
      
      // Download semua gambar
      data.download_link.no_watermark.forEach((url, index) => {
        const a = document.createElement('a');
        a.href = url;
        a.download = `photo_${index + 1}.jpg`;
        a.click();
      });
      
      // Download audio
      if (data.download_link.mp3) {
        window.location.href = data.download_link.mp3;
      }
    }
    
  } catch (error) {
    console.error('Error:', error);
  }
}

// Contoh penggunaan
downloadTikTok('https://www.tiktok.com/@username/video/123456789');
```

## 5. React Component Example

```jsx
import { useState } from 'react';

function TikTokDownloader() {
  const [url, setUrl] = useState('');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    
    try {
      const response = await fetch('http://localhost:3021/tiktok', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ url })
      });
      
      if (!response.ok) {
        throw new Error('Failed to fetch TikTok data');
      }
      
      const result = await response.json();
      setData(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Paste TikTok URL here"
        />
        <button type="submit" disabled={loading}>
          {loading ? 'Loading...' : 'Download'}
        </button>
      </form>

      {error && <div className="error">{error}</div>}

      {data && (
        <div>
          <h3>{data.title}</h3>
          <p>Views: {data.statistics.play_count.toLocaleString()}</p>
          <p>Likes: {data.statistics.digg_count.toLocaleString()}</p>
          
          {data.status === 'tunnel' && (
            <div>
              <h4>Download Options:</h4>
              {data.download_link.no_watermark_hd && (
                <a href={data.download_link.no_watermark_hd} download>
                  Download HD (No Watermark)
                </a>
              )}
              {data.download_link.no_watermark && (
                <a href={data.download_link.no_watermark} download>
                  Download SD (No Watermark)
                </a>
              )}
              {data.download_link.mp3 && (
                <a href={data.download_link.mp3} download>
                  Download Audio (MP3)
                </a>
              )}
            </div>
          )}
          
          {data.status === 'picker' && (
            <div>
              <h4>Photos:</h4>
              {data.photos.map((photo, index) => (
                <img key={index} src={photo.url} alt={`Photo ${index + 1}`} />
              ))}
              <div>
                {data.download_link.no_watermark.map((url, index) => (
                  <a key={index} href={url} download>
                    Download Photo {index + 1}
                  </a>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default TikTokDownloader;
```

## 6. Python Example

```python
import requests

def download_tiktok(url):
    # Dapatkan metadata
    response = requests.post(
        'http://localhost:3021/tiktok',
        json={'url': url}
    )
    
    data = response.json()
    
    if data['status'] == 'tunnel':
        # Video biasa
        print(f"Title: {data['title']}")
        print(f"Views: {data['statistics']['play_count']}")
        
        # Download HD version
        download_url = data['download_link'].get('no_watermark_hd') or \
                      data['download_link'].get('no_watermark')
        
        if download_url:
            video_response = requests.get(download_url, stream=True)
            with open('video.mp4', 'wb') as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print("Video downloaded successfully!")
    
    elif data['status'] == 'picker':
        # Photo slideshow
        print(f"Found {len(data['photos'])} photos")
        
        for i, photo_url in enumerate(data['download_link']['no_watermark']):
            photo_response = requests.get(photo_url)
            with open(f'photo_{i+1}.jpg', 'wb') as f:
                f.write(photo_response.content)
        
        print("Photos downloaded successfully!")

# Contoh penggunaan
download_tiktok('https://www.tiktok.com/@whitehouse/video/7587051948285644087')
```

## 7. Testing dengan cURL

```bash
# Test health check
curl http://localhost:3021/health

# Test video download
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}' \
  | jq '.download_link'

# Test photo slideshow
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@yusuf_sufiandi24/photo/7457053391559216392"}' \
  | jq '.photos'
```

## 8. Handling IP Restrictions

Jika user tidak bisa akses URL langsung dari TikTok CDN karena IP restriction, server ini sudah menggunakan streaming endpoint yang akan:

1. Download video menggunakan yt-dlp di server
2. Stream langsung ke client melalui pipe
3. Tidak perlu menyimpan file di server

**Cara kerja:**
```
Client Request → Server → yt-dlp (download) → Pipe → Client
```

Ini mengatasi masalah IP restriction karena:
- Server yang melakukan request ke TikTok CDN, bukan client
- Client hanya terima stream dari server lokal
- Tidak ada file temporary yang disimpan

## 9. Error Handling

```javascript
async function downloadWithErrorHandling(url) {
  try {
    const response = await fetch('http://localhost:3021/tiktok', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.error || 'Unknown error');
    }
    
    const data = await response.json();
    return data;
    
  } catch (error) {
    if (error.message.includes('URL parameter is required')) {
      console.error('Please provide a valid TikTok URL');
    } else if (error.message.includes('Only TikTok and Douyin URLs')) {
      console.error('Only TikTok/Douyin URLs are supported');
    } else if (error.message.includes('Encrypted data has expired')) {
      console.error('Download link expired, please generate new link');
    } else {
      console.error('Error:', error.message);
    }
    throw error;
  }
}
```

## 10. Rate Limiting (Recommended)

Untuk production, tambahkan rate limiting:

```bash
npm install express-rate-limit
```

```javascript
import rateLimit from 'express-rate-limit';

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100 // limit each IP to 100 requests per windowMs
});

app.use('/tiktok', limiter);
```
