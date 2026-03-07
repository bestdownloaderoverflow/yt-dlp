# Gluetun VPN Setup for yt-dlp-stream

Konfigurasi ini membungkus yt-dlp-stream dengan Gluetun VPN untuk:
- **IP Rotation**: Ganti IP otomatis saat terkena rate limit/block
- **Geo-unblocking**: Akses konten yang dibatasi region
- **Privacy**: Semua traffic yt-dlp routing melalui VPN

## Quick Start

```bash
# Jalankan dengan Gluetun
cd yt-dlp-stream
docker-compose -f docker-compose.gluetun.yml up -d

# Cek status VPN
docker-compose -f docker-compose.gluetun.yml exec app python vpn_manager.py status

# Rotate IP manual
docker-compose -f docker-compose.gluetun.yml exec app python vpn_manager.py reconnect
```

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Client        │────▶│   Gluetun       │────▶│   yt-dlp-stream │
│   (Request)     │     │   (VPN/WG)      │     │   (App)         │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌─────────────────┐
                        │   WireGuard     │
                        │   162.159.192.1 │
                        └─────────────────┘
```

## Konfigurasi WireGuard

VPN menggunakan WireGuard dengan custom provider:

```yaml
VPN_SERVICE_PROVIDER=custom
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY=mJNxbqpODxFWrNpoJnNJt3GAZaegIFuiY6XQekl0zkI=
WIREGUARD_ADDRESSES=172.16.0.2/32
WIREGUARD_PUBLIC_KEY=bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
WIREGUARD_ENDPOINT_IP=162.159.192.1
WIREGUARD_ENDPOINT_PORT=2408
```

## Port Mapping

| Host Port | Container | Service |
|-----------|-----------|---------|
| 9487 | gluetun:9487 | yt-dlp-stream API |
| 8000 | gluetun:8000 | Gluetun Control Server |

## VPN Manager Commands

### Check Status
```bash
docker-compose -f docker-compose.gluetun.yml exec app python vpn_manager.py status
```

### Reconnect (Get New IP)
```bash
docker-compose -f docker-compose.gluetun.yml exec app python vpn_manager.py reconnect
```

### Handle 403 Error
```bash
docker-compose -f docker-compose.gluetun.yml exec app python vpn_manager.py handle-403
```

## Integrasi dengan TikTok Downloader

Untuk auto-rotate IP saat terkena 403:

```python
from vpn_manager import get_vpn_manager

# Di dalam error handler 403
vpn = get_vpn_manager()
await vpn.handle_403_error()
```

## Multiple VPN Instances (Advanced)

Untuk load balancing dengan multiple region:

```yaml
# docker-compose.multi-vpn.yml
services:
  gluetun-sg:
    image: qmcgaw/gluetun:v3.41.1
    ports:
      - "9487:9487"  # Instance 1
      - "8001:8000"
    environment:
      - SERVER_COUNTRIES=Singapore
      # ... other config

  gluetun-jp:
    image: qmcgaw/gluetun:v3.41.1
    ports:
      - "9488:9487"  # Instance 2
      - "8002:8000"
    environment:
      - SERVER_COUNTRIES=Japan
      # ... other config
```

## Troubleshooting

### Check Gluetun Logs
```bash
docker-compose -f docker-compose.gluetun.yml logs -f gluetun
```

### Verify VPN Connection
```bash
# Check public IP from inside container
docker-compose -f docker-compose.gluetun.yml exec gluetun wget -qO- https://ipinfo.io
```

### Restart VPN
```bash
docker-compose -f docker-compose.gluetun.yml restart gluetun
```

## Security Notes

- **Jangan commit credentials WireGuard** ke git
- Gunakan `.env` file untuk menyimpan secrets:
  ```bash
  WIREGUARD_PRIVATE_KEY=your_key_here
  GLUETUN_PASSWORD=strong_password
  ```
- Gluetun Control Server di-bind ke localhost only via `network_mode: service:gluetun`
