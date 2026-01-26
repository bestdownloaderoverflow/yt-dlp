#!/bin/bash

# Quick start script for Docker deployment

set -e

echo "🐳 yt-dlp TikTok Server - Docker Quick Start"
echo "=============================================="
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

# Check if Docker Compose is installed
if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

echo "✅ Docker and Docker Compose are installed"
echo ""

# Build and start
echo "📦 Building Docker image..."
docker-compose build

echo ""
echo "🚀 Starting container..."
docker-compose up -d

echo ""
echo "⏳ Waiting for server to start..."
sleep 5

# Check health
echo ""
echo "🏥 Checking server health..."
if curl -f http://localhost:3021/health > /dev/null 2>&1; then
    echo "✅ Server is running and healthy!"
    echo ""
    echo "📊 Server Status:"
    docker-compose ps
    echo ""
    echo "📝 View logs:"
    echo "   docker-compose logs -f yt-dlp-server"
    echo ""
    echo "🛑 Stop server:"
    echo "   docker-compose down"
    echo ""
    echo "🌐 Server URL: http://localhost:3021"
    echo "📖 API Docs: See README.md"
else
    echo "⚠️  Server may still be starting. Check logs:"
    echo "   docker-compose logs yt-dlp-server"
fi
