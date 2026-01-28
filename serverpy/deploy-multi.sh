#!/bin/bash
# =====================================================
# Multi-Instance VPN Deploy Script
# TikTok Downloader - Singapore, Japan, USA
# =====================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_header() {
    echo -e "${BLUE}"
    echo "======================================================"
    echo "  TikTok Downloader - Multi-Instance VPN Setup"
    echo "======================================================"
    echo -e "${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_info() {
    echo -e "${YELLOW}ℹ $1${NC}"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
   print_error "Do not run as root"
   exit 1
fi

# Check Docker
if ! command -v docker &> /dev/null; then
    print_error "Docker is not installed"
    echo "Install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

# Check Docker Compose
if ! docker compose version &> /dev/null && ! docker-compose version &> /dev/null; then
    print_error "Docker Compose is not installed"
    exit 1
fi

print_header

# Determine which compose command to use
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

# Create necessary directories
echo "Creating directories..."
mkdir -p gluetun/sg gluetun/jp gluetun/us
mkdir -p temp/sg temp/jp temp/us
mkdir -p cookies
mkdir -p ssl
print_success "Directories created"

# Check if .env.multi exists
if [ ! -f ".env.multi" ]; then
    print_error ".env.multi file not found!"
    echo "Please create .env.multi from the template:"
    echo "  cp .env.multi.example .env.multi"
    echo "  nano .env.multi  # Edit with your VPN keys"
    exit 1
fi

# Source environment variables
export $(grep -v '^#' .env.multi | xargs)

# Check if VPN keys are set
if [ "$WG_KEY_SG" = "your_singapore_wireguard_private_key" ] || \
   [ "$WG_KEY_JP" = "your_japan_wireguard_private_key" ] || \
   [ "$WG_KEY_US" = "your_usa_wireguard_private_key" ]; then
    print_error "VPN keys not configured!"
    print_info "Please edit .env.multi and add your WireGuard keys from Mullvad"
    exit 1
fi

print_success "Environment variables loaded"

# Pull latest images
echo ""
echo "Pulling Docker images..."
$COMPOSE_CMD -f docker-compose.multi.yml pull
print_success "Images pulled"

# Start services
echo ""
echo "Starting services..."
$COMPOSE_CMD -f docker-compose.multi.yml up -d
print_success "Services started"

# Wait for services to be healthy
echo ""
echo "Waiting for services to be healthy..."
sleep 10

# Check health
print_info "Checking service health..."

for i in {1..5}; do
    SG_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3021/health || echo "000")
    JP_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3022/health || echo "000")
    US_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3023/health || echo "000")
    NGINX_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:80/health || echo "000")
    
    if [ "$SG_HEALTH" = "200" ] && [ "$JP_HEALTH" = "200" ] && [ "$US_HEALTH" = "200" ]; then
        print_success "All instances are healthy!"
        break
    fi
    
    print_info "Waiting... (SG: $SG_HEALTH, JP: $JP_HEALTH, US: $US_HEALTH, NGINX: $NGINX_HEALTH)"
    sleep 5
done

# Display status
echo ""
echo -e "${GREEN}======================================================"
echo "  Deployment Complete!"
echo "======================================================${NC}"
echo ""
echo "Services:"
echo "  Singapore: http://localhost:3021"
echo "  Japan:     http://localhost:3022"
echo "  USA:       http://localhost:3023"
echo "  Nginx:     http://localhost:80"
echo ""
echo "Gluetun Control Servers:"
echo "  Singapore: http://localhost:8001"
echo "  Japan:     http://localhost:8002"
echo "  USA:       http://localhost:8003"
echo ""
echo "Monitoring:"
echo "  Health Check: curl http://localhost/health"
echo ""
echo "Useful commands:"
echo "  View logs:    $COMPOSE_CMD -f docker-compose.multi.yml logs -f"
echo "  Stop:         $COMPOSE_CMD -f docker-compose.multi.yml down"
echo "  Restart:      $COMPOSE_CMD -f docker-compose.multi.yml restart"
echo "  Status:       $COMPOSE_CMD -f docker-compose.multi.yml ps"
echo ""
echo "Test failover:"
echo "  curl -X POST http://localhost/tiktok -H 'Content-Type: application/json' -d '{\"url\":\"https://vt.tiktok.com/ZSaPXyDuw/\"}'"
echo ""

# Test a request
echo "Testing API..."
TEST_RESPONSE=$(curl -s -X POST http://localhost/tiktok \
    -H "Content-Type: application/json" \
    -d '{"url":"https://vt.tiktok.com/ZSaure8MF/"}' 2>/dev/null || echo '{"error":"failed"}')

if echo "$TEST_RESPONSE" | grep -q "status"; then
    print_success "API test successful!"
    echo "Response: $TEST_RESPONSE" | head -c 200
    echo "..."
else
    print_error "API test failed"
    print_info "This is normal if TikTok is blocking the IP. The failover system will handle it."
fi

echo ""
print_success "Setup complete!"
