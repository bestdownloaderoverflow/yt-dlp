#!/bin/bash

# Start script for TikTok Downloader API (Python)

set -e

echo "🚀 Starting TikTok Downloader API (Python)"
echo "=========================================="

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed"
    exit 1
fi

# Check if FFmpeg is installed
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️  Warning: FFmpeg is not installed. Slideshow feature will not work."
    echo "   Install FFmpeg: brew install ffmpeg (macOS) or apt install ffmpeg (Ubuntu)"
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Install/upgrade dependencies
echo "📥 Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
fi

# Create necessary directories
mkdir -p temp cookies

# Load environment variables
export $(grep -v '^#' .env | xargs)

echo ""
echo "✅ Setup complete!"
echo ""
echo "📊 Configuration:"
echo "   Port: ${PORT:-3021}"
echo "   Base URL: ${BASE_URL:-http://localhost:3021}"
echo "   Max Workers: ${MAX_WORKERS:-20}"
echo "   Temp Dir: ${TEMP_DIR:-./temp}"
echo ""
echo "🌐 Starting server..."
echo ""

# Start server
python3 main.py
