# Changelog

All notable changes to this project will be documented in this file.

## [1.4.0] - 2026-01-26

### Added
- ✅ **Automatic Cleanup Mechanism** - Scheduled cleanup of old temp folders
  - New `cleanup.js` module (100% identical to downloader-bun)
  - `cleanupFolder()` - Remove specific folder
  - `cleanupOldFolders()` - Remove folders older than 1 hour
  - `initCleanupSchedule()` - Run cleanup every 15 minutes using cron
  - Automatic cleanup on server startup
  - Manual cleanup: `node cleanup.js`
  - Uses `node-cron` for scheduling (same as downloader-bun)

### Technical Details
- Cleanup runs every 15 minutes automatically (cron: `*/15 * * * *`)
- Removes temp folders older than 1 hour
- Prevents disk space issues from abandoned slideshow jobs
- 100% identical cleanup mechanism as downloader-bun

## [1.3.0] - 2026-01-26

### Added
- ✅ **Docker Support** - Complete Docker & Docker Compose setup
  - Dockerfile with Node.js 20, Python3, yt-dlp, ffmpeg
  - docker-compose.yml for easy deployment
  - .dockerignore for optimized builds
  - Health checks configured
  - Volume mounts for temp directory
- ✅ **Docker Documentation** - Comprehensive Docker guide (DOCKER.md)
  - Quick start guide
  - Production deployment instructions
  - Troubleshooting guide
  - Security best practices

### Technical Details
- Base image: node:20-bullseye-slim
- Installed: ffmpeg, python3, yt-dlp, curl
- Health check: Every 30 seconds
- Temp directory: Mounted as volume
- yt-dlp wrapper: /usr/local/bin/yt-dlp.sh

## [1.2.1] - 2026-01-26

### Fixed
- ✅ **Audio download for videos** - MP3 button now appears for video posts
  - Fixed audio format detection to use video formats when audio-only format not available
  - Videos now correctly show "Download Audio" button in frontend
- ✅ **Client disconnect handling** - Improved resource cleanup
  - `/download` endpoint now properly handles client disconnects
  - Prevents resource leaks when users cancel downloads
  - All streaming endpoints now have proper cleanup on disconnect
- ✅ **403 Forbidden errors fixed** - Audio downloads now use streaming
  - Changed audio downloads from direct URL to `/stream` endpoint
  - Prevents "403 Forbidden" errors from TikTok CDN
  - Works for both video and photo posts

### Technical Details
- Audio format fallback: Uses first video format with audio if no audio-only format exists
- Client disconnect: Added `req.on('close')` handlers to destroy streams properly
- Resource management: Ensures all streams are destroyed when client disconnects
- Audio streaming: All audio downloads now go through yt-dlp streaming (no direct CDN URLs)

## [1.2.0] - 2026-01-26

### Added
- ✅ **Frontend compatibility fixes** - 100% compatible with Snaptik frontend
- Author fields mapping (uniqueId, avatarThumb, avatarMedium, avatarLarger)
- Statistics mapping (share_count from repost_count)
- Audio download for videos (mp3 format)
- FRONTEND_COMPATIBILITY.md documentation
- INTEGRATION_GUIDE.md for easy frontend integration

### Changed
- Author object now includes all avatar variants
- Statistics now use frontend-expected field names
- Videos now include mp3 download link

### Fixed
- Author uniqueId now properly mapped from uploader_id
- Statistics share_count properly mapped from repost_count
- All avatar fields now available for frontend display

## [1.1.0] - 2026-01-26

### Added
- ✅ **Slideshow generation** - Full implementation using ffmpeg
- Download and merge photos with audio
- Create video slideshow (4 seconds per image)
- Scale and pad images to 1080x1920
- Loop audio to match video duration
- Automatic temp folder cleanup
- Abort support for cancelled requests
- ffmpeg-static for cross-platform support
- fs-extra for file operations

### Changed
- Updated dependencies (added fluent-ffmpeg, ffmpeg-static, fs-extra)
- Environment variables (added FFMPEG_PATH, TEMP_DIR)
- Documentation updated to reflect slideshow support

### Technical Details
- Each image displayed for 4 seconds
- Audio loops to match total video duration
- Images scaled to 1080x1920 with black padding
- H.264 codec for maximum compatibility
- Automatic cleanup of temporary files

## [1.0.0] - 2026-01-26

### Added
- Initial release
- POST /tiktok endpoint for fetching metadata
- GET /download endpoint for downloading files via URL
- GET /stream endpoint for streaming video via yt-dlp
- GET /download-slideshow endpoint (not implemented yet)
- GET /health endpoint for health checks
- AES-256-GCM encryption for download links
- TTL support (360 seconds)
- Support for regular TikTok videos
- Support for TikTok photo slideshows
- CORS support
- Error handling
- 404 handler
- Comprehensive documentation
  - README.md
  - EXAMPLES.md
  - COMPARISON.md
  - DEPLOYMENT.md
  - QUICK_START.md
- Test suite (test.sh)

### Features
- 100% API compatible with downloader-bun/index.js
- IP restriction solution via server streaming
- Self-contained (no external API dependencies)
- Multiple video quality options
- Statistics and metadata extraction
- Author information extraction

### Technical Details
- Node.js >= 18.0.0
- Express.js web framework
- yt-dlp for video extraction
- Streaming support for large files
- Process spawning for yt-dlp execution
- Encrypted download links with expiration

### Known Limitations
- Slower than original API (5-10s vs 2-3s for metadata)
- Slideshow generation takes time (~10-30s)
- Higher server resource usage
- No caching implemented yet

## [Unreleased]

### Planned Features
- [ ] Response caching
- [ ] Rate limiting
- [ ] API key authentication
- [ ] Request logging
- [ ] Metrics/analytics
- [ ] Docker support
- [ ] Cluster mode support
- [ ] WebSocket support for progress updates
- [ ] Batch download support

### Improvements Needed
- [ ] Better error messages
- [ ] Retry logic for failed downloads
- [ ] Timeout configuration
- [ ] Memory optimization
- [ ] Performance optimization
- [ ] Unit tests
- [ ] Integration tests
- [ ] CI/CD pipeline

### Bug Fixes
- None reported yet

---

## Version History

### Version Format
- Major.Minor.Patch (Semantic Versioning)
- Major: Breaking changes
- Minor: New features (backward compatible)
- Patch: Bug fixes (backward compatible)

### Release Notes

#### v1.0.0 (2026-01-26)
First stable release with core functionality:
- TikTok video download
- Photo slideshow support
- Encrypted links
- Streaming support
- Full documentation

---

## Migration Guide

### From Original API to yt-dlp Server

No code changes required! Just change the base URL:

```javascript
// Before
const API_URL = 'https://d.snaptik.fit';

// After
const API_URL = 'http://localhost:3021';

// Everything else stays the same!
```

### Breaking Changes

None - this is the first release.

---

## Support

For issues, questions, or feature requests:
1. Check documentation
2. Run test suite
3. Check logs
4. Open GitHub issue

---

## Contributors

- Initial development: AI Assistant
- Based on: yt-dlp project
- API format: downloader-bun/index.js

---

## License

Same as parent project (yt-dlp-tiktok)
