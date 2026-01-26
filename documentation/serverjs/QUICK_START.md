# Quick Start Guide

## 1️⃣ Install

```bash
cd serverjs
npm install
```

## 2️⃣ Configure

```bash
cp .env.example .env
# Edit .env if needed
```

## 3️⃣ Run

```bash
npm start
```

## 4️⃣ Test

```bash
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@whitehouse/video/7587051948285644087"}'
```

## 5️⃣ Use in Your App

```javascript
// Fetch metadata
const response = await fetch('http://localhost:3021/tiktok', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ 
    url: 'https://www.tiktok.com/@username/video/123' 
  })
});

const data = await response.json();

// Download video
if (data.status === 'tunnel') {
  window.location.href = data.download_link.no_watermark_hd;
}

// Download photos
if (data.status === 'picker') {
  data.download_link.no_watermark.forEach(url => {
    window.location.href = url;
  });
}
```

## 🎯 That's it!

For more examples, see [EXAMPLES.md](EXAMPLES.md)

For deployment, see [DEPLOYMENT.md](DEPLOYMENT.md)

For comparison with original API, see [COMPARISON.md](COMPARISON.md)
