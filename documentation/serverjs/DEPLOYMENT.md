# Deployment Guide

## Development

```bash
cd serverjs
npm install
npm run dev
```

Server akan running di `http://localhost:3021`

## Production

### 1. Using PM2 (Recommended)

```bash
# Install PM2
npm install -g pm2

# Start server
pm2 start index.js --name tiktok-downloader

# View logs
pm2 logs tiktok-downloader

# Restart
pm2 restart tiktok-downloader

# Stop
pm2 stop tiktok-downloader

# Auto-start on system boot
pm2 startup
pm2 save
```

### 2. Using systemd (Linux)

Create `/etc/systemd/system/tiktok-downloader.service`:

```ini
[Unit]
Description=TikTok Downloader API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/yt-dlp-tiktok/serverjs
ExecStart=/usr/bin/node index.js
Restart=on-failure
Environment=NODE_ENV=production
Environment=PORT=3021

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable tiktok-downloader
sudo systemctl start tiktok-downloader
sudo systemctl status tiktok-downloader
```

### 3. Using Docker

Create `Dockerfile`:

```dockerfile
FROM node:18-alpine

# Install yt-dlp dependencies
RUN apk add --no-cache python3 py3-pip

# Install yt-dlp
RUN pip3 install yt-dlp

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci --only=production

# Copy application files
COPY . .

# Copy yt-dlp.sh from parent directory
COPY ../yt-dlp.sh ./yt-dlp.sh
RUN chmod +x ./yt-dlp.sh

EXPOSE 3021

CMD ["node", "index.js"]
```

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  tiktok-downloader:
    build: .
    ports:
      - "3021:3021"
    environment:
      - PORT=3021
      - BASE_URL=http://localhost:3021
      - ENCRYPTION_KEY=your-secret-key-here
      - YT_DLP_PATH=./yt-dlp.sh
    restart: unless-stopped
    volumes:
      - ./logs:/app/logs
```

Build and run:

```bash
docker-compose up -d
```

### 4. Nginx Reverse Proxy

Create `/etc/nginx/sites-available/tiktok-downloader`:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:3021;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Increase timeouts for large video downloads
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
        send_timeout 600;
    }
}
```

Enable and restart:

```bash
sudo ln -s /etc/nginx/sites-available/tiktok-downloader /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 5. SSL with Let's Encrypt

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

## Environment Variables for Production

Create `.env` file:

```env
PORT=3021
BASE_URL=https://your-domain.com
ENCRYPTION_KEY=generate-strong-random-key-here
YT_DLP_PATH=../yt-dlp.sh
NODE_ENV=production
```

Generate strong encryption key:

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

## Security Considerations

### 1. Rate Limiting

```bash
npm install express-rate-limit
```

```javascript
import rateLimit from 'express-rate-limit';

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100, // limit each IP to 100 requests per windowMs
  message: 'Too many requests from this IP, please try again later.'
});

app.use('/tiktok', limiter);
app.use('/download', limiter);
app.use('/stream', limiter);
```

### 2. Helmet for Security Headers

```bash
npm install helmet
```

```javascript
import helmet from 'helmet';
app.use(helmet());
```

### 3. CORS Configuration

For production, limit CORS to specific domains:

```javascript
app.use(cors({
  origin: ['https://your-frontend.com', 'https://www.your-frontend.com'],
  methods: ['GET', 'POST'],
  credentials: true
}));
```

### 4. Request Size Limit

```javascript
app.use(express.json({ limit: '10kb' }));
```

### 5. API Key Authentication (Optional)

```javascript
const API_KEY = process.env.API_KEY;

function authenticateApiKey(req, res, next) {
  const apiKey = req.headers['x-api-key'];
  
  if (!apiKey || apiKey !== API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  
  next();
}

app.use('/tiktok', authenticateApiKey);
```

## Monitoring

### 1. Health Check Endpoint

Already implemented at `/health`

### 2. Logging

Add logging middleware:

```bash
npm install morgan
```

```javascript
import morgan from 'morgan';
import fs from 'fs';
import path from 'path';

// Create logs directory
const logsDir = path.join(__dirname, 'logs');
if (!fs.existsSync(logsDir)) {
  fs.mkdirSync(logsDir);
}

// Create write stream
const accessLogStream = fs.createWriteStream(
  path.join(logsDir, 'access.log'),
  { flags: 'a' }
);

// Setup logger
app.use(morgan('combined', { stream: accessLogStream }));
```

### 3. Error Tracking

Consider using services like:
- Sentry
- Rollbar
- Bugsnag

### 4. Uptime Monitoring

Use services like:
- UptimeRobot
- Pingdom
- StatusCake

## Performance Optimization

### 1. Enable Compression

```bash
npm install compression
```

```javascript
import compression from 'compression';
app.use(compression());
```

### 2. Caching

For metadata responses (not download links):

```bash
npm install node-cache
```

```javascript
import NodeCache from 'node-cache';
const cache = new NodeCache({ stdTTL: 300 }); // 5 minutes

app.post('/tiktok', async (req, res) => {
  const { url } = req.body;
  
  // Check cache
  const cached = cache.get(url);
  if (cached) {
    return res.json(cached);
  }
  
  // Fetch and cache
  const data = await fetchTikTokData(url);
  const response = generateJsonResponse(data, url);
  cache.set(url, response);
  
  return res.json(response);
});
```

### 3. Cluster Mode

```javascript
import cluster from 'cluster';
import os from 'os';

if (cluster.isPrimary) {
  const numCPUs = os.cpus().length;
  
  for (let i = 0; i < numCPUs; i++) {
    cluster.fork();
  }
  
  cluster.on('exit', (worker, code, signal) => {
    console.log(`Worker ${worker.process.pid} died`);
    cluster.fork();
  });
} else {
  // Start server
  app.listen(PORT, () => {
    console.log(`Worker ${process.pid} started`);
  });
}
```

## Backup & Recovery

### 1. Backup Configuration

```bash
# Backup .env file
cp .env .env.backup

# Backup with timestamp
cp .env .env.$(date +%Y%m%d_%H%M%S)
```

### 2. Database (if using)

If you add database for analytics:

```bash
# PostgreSQL
pg_dump dbname > backup.sql

# MongoDB
mongodump --db dbname --out /backup/
```

## Troubleshooting

### yt-dlp not found

```bash
# Check yt-dlp path
which yt-dlp

# Update YT_DLP_PATH in .env
YT_DLP_PATH=/usr/local/bin/yt-dlp
```

### Permission denied

```bash
chmod +x yt-dlp.sh
```

### Port already in use

```bash
# Find process using port
lsof -i :3021

# Kill process
kill -9 <PID>
```

### High memory usage

```bash
# Limit Node.js memory
node --max-old-space-size=512 index.js
```

## Maintenance

### Update yt-dlp

```bash
# If using pip
pip3 install --upgrade yt-dlp

# If using binary
./yt-dlp.sh -U
```

### Update Dependencies

```bash
npm update
npm audit fix
```

### Monitor Disk Space

```bash
df -h
```

### Clean Logs

```bash
# Rotate logs
logrotate /etc/logrotate.d/tiktok-downloader
```

## Scaling

### Horizontal Scaling

Use load balancer (nginx, HAProxy) with multiple instances:

```nginx
upstream tiktok_backend {
    server 127.0.0.1:3021;
    server 127.0.0.1:3022;
    server 127.0.0.1:3023;
}

server {
    location / {
        proxy_pass http://tiktok_backend;
    }
}
```

### Vertical Scaling

Increase server resources (CPU, RAM, Network)

### CDN

Use CDN for static assets and API responses
