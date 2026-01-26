import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import { createReadStream } from 'fs';
import fs from 'fs-extra';
import ffmpeg from 'fluent-ffmpeg';
import ffmpegStatic from 'ffmpeg-static';
import { encrypt, decrypt } from './encryption.js';

dotenv.config();

// Set ffmpeg path
ffmpeg.setFfmpegPath(ffmpegStatic);

// Environment variables
const PORT = process.env.PORT || 3021;
const BASE_URL = process.env.BASE_URL || 'http://localhost:3021';
const ENCRYPTION_KEY = process.env.ENCRYPTION_KEY || 'overflow';
const YT_DLP_PATH = process.env.YT_DLP_PATH || '../yt-dlp.sh';
const FFMPEG_PATH = process.env.FFMPEG_PATH || ffmpegStatic;
const TEMP_DIR = process.env.TEMP_DIR || './temp';

// Get the directory name
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Temp directory path
const tempDir = path.join(__dirname, TEMP_DIR);

// Create temp directory if it doesn't exist
fs.ensureDirSync(tempDir);

// Initialize Express app
const app = express();

// Middleware
app.use(cors({
  origin: '*',
  methods: ['GET', 'POST', 'OPTIONS'],
  allowedHeaders: ['Origin', 'Content-Type', 'Content-Length', 'Accept-Encoding', 'Authorization'],
  exposedHeaders: ['Content-Disposition', 'X-Filename', 'Content-Length']
}));
app.use(express.json());

// Content type mapping
const contentTypes = {
  mp3: ['audio/mpeg', 'mp3'],
  video: ['video/mp4', 'mp4'],
  image: ['image/jpeg', 'jpg']
};

/**
 * Download file from URL to local path
 * @param {string} url - URL to download
 * @param {string} outputPath - Local path to save file
 * @param {Object} options - Options including signal for abort
 * @returns {Promise<string>} - Path to downloaded file
 */
async function downloadFile(url, outputPath, options = {}) {
  const { signal } = options;
  const cleanupPartial = () => fs.remove(outputPath).catch(() => {});

  try {
    const { default: got } = await import('got');
    
    await new Promise((resolve, reject) => {
      const writeStream = fs.createWriteStream(outputPath);
      const downloadStream = got.stream(url, {
        timeout: {
          request: 120000 // 120 seconds timeout
        },
        retry: {
          limit: 2
        },
        signal
      });

      const handleError = (error) => {
        downloadStream.destroy();
        writeStream.destroy();
        cleanupPartial().finally(() => reject(error));
      };

      downloadStream.pipe(writeStream);

      downloadStream.on('error', handleError);
      writeStream.on('error', handleError);
      writeStream.on('finish', resolve);
    });

    return outputPath;
  } catch (error) {
    if (error?.name === 'AbortError' || error?.name === 'CancelError') {
      throw error;
    }
    throw new Error(`Failed to download file: ${error.message}`);
  }
}

/**
 * Create a slideshow from images and audio using ffmpeg
 * @param {string[]} imagePaths - Array of image file paths
 * @param {string} audioPath - Path to audio file
 * @param {string} outputPath - Path for output video
 * @param {Object} options - Options including signal and onCommand callback
 * @returns {Promise<void>}
 */
function createSlideshow(imagePaths, audioPath, outputPath, options = {}) {
  const { onCommand, signal } = options;

  return new Promise((resolve, reject) => {
    const command = ffmpeg();
    let settled = false;

    if (typeof onCommand === 'function') {
      onCommand(command);
    }

    const finish = (callback) => (value) => {
      if (settled) {
        return;
      }
      settled = true;
      if (signal) {
        signal.removeEventListener('abort', onAbort);
      }
      callback(value);
    };

    const onAbort = () => {
      if (settled) {
        return;
      }
      settled = true;
      try {
        command.kill('SIGKILL');
      } catch (abortError) {
        // Ignore kill errors; command may already be stopped
      }
      if (signal) {
        signal.removeEventListener('abort', onAbort);
      }
      reject(new Error('Slideshow rendering aborted'));
    };

    if (signal) {
      signal.addEventListener('abort', onAbort, { once: true });
      if (signal.aborted) {
        onAbort();
        return;
      }
    }

    // Add each image as input
    imagePaths.forEach(imagePath => {
      command.input(imagePath).inputOptions(['-loop 1', '-t 4']);
    });

    // Add audio with loop
    command.input(audioPath).inputOptions(['-stream_loop -1']);

    // Build complex filter for scaling and concatenating images
    const filter = [];

    // Scale and pad each image
    imagePaths.forEach((_, index) => {
      filter.push(`[${index}:v]scale=w=1080:h=1920:force_original_aspect_ratio=decrease,` +
        `pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v${index}]`);
    });

    // Concatenate all scaled/padded video streams
    const concatInputs = imagePaths.map((_, i) => `[v${i}]`).join('');
    filter.push(`${concatInputs}concat=n=${imagePaths.length}:v=1:a=0[vout]`);

    // Calculate total duration
    const videoDuration = imagePaths.length * 4;

    // Add audio filter to trim the looping audio to the video duration
    filter.push(`[${imagePaths.length}:a]atrim=0:${videoDuration}[aout]`);

    command
      .complexFilter(filter)
      .outputOptions([
        '-map', '[vout]',
        '-map', '[aout]',
        '-pix_fmt', 'yuv420p',
        '-fps_mode', 'cfr'
      ])
      .videoCodec('libx264')
      .output(outputPath)
      .on('error', finish((err) => {
        reject(new Error(`FFmpeg error: ${err.message}`));
      }))
      .on('end', finish(() => {
        console.log('Slideshow created successfully');
        resolve();
      }))
      .run();
  });
}

/**
 * Cleanup temporary folder
 * @param {string} folderPath - Path to folder to cleanup
 */
async function cleanupFolder(folderPath) {
  try {
    await fs.remove(folderPath);
    console.log(`Cleaned up temp folder: ${folderPath}`);
  } catch (error) {
    console.error(`Error cleaning up folder ${folderPath}:`, error);
  }
}

/**
 * Execute yt-dlp command and return JSON output
 * @param {string} url - TikTok URL
 * @returns {Promise<Object>} - Parsed JSON from yt-dlp
 */
async function fetchTikTokData(url) {
  return new Promise((resolve, reject) => {
    const ytDlpPath = path.resolve(__dirname, YT_DLP_PATH);
    const args = ['-J', url];
    
    const process = spawn(ytDlpPath, args);
    
    let stdout = '';
    let stderr = '';
    
    process.stdout.on('data', (data) => {
      stdout += data.toString();
    });
    
    process.stderr.on('data', (data) => {
      stderr += data.toString();
    });
    
    process.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(`yt-dlp failed: ${stderr || 'Unknown error'}`));
        return;
      }
      
      try {
        const data = JSON.parse(stdout);
        resolve(data);
      } catch (error) {
        reject(new Error(`Failed to parse yt-dlp output: ${error.message}`));
      }
    });
    
    process.on('error', (error) => {
      reject(new Error(`Failed to execute yt-dlp: ${error.message}`));
    });
  });
}

/**
 * Generate JSON response matching the expected format
 * @param {Object} data - yt-dlp JSON output
 * @param {string} url - Original TikTok URL
 * @returns {Object} - Formatted response
 */
function generateJsonResponse(data, url) {
  const isImage = data.formats && data.formats.some(f => f.format_id && f.format_id.startsWith('image-'));
  
  // Extract author info
  const author = {
    nickname: data.uploader || data.channel || 'unknown',
    signature: data.description || '',
    avatar: data.thumbnails?.[0]?.url || ''
  };
  
  // Extract statistics
  const statistics = {
    repost_count: data.repost_count || 0,
    comment_count: data.comment_count || 0,
    digg_count: data.like_count || 0,
    play_count: data.view_count || 0
  };
  
  // Base metadata
  let metadata = {
    title: data.title || data.fulltitle || '',
    description: data.description || data.title || '',
    statistics: statistics,
    artist: data.artist || author.nickname,
    cover: data.thumbnail || '',
    duration: data.duration ? data.duration * 1000 : 0,
    audio: '',
    download_link: {},
    music_duration: data.duration ? data.duration * 1000 : 0,
    author: author
  };
  
  if (isImage) {
    // Photo slideshow post
    const imageFormats = data.formats.filter(f => f.format_id && f.format_id.startsWith('image-'));
    const audioFormat = data.formats.find(f => f.format_id === 'audio');
    
    const picker = imageFormats.map(img => ({
      type: 'photo',
      url: img.url
    }));
    
    if (audioFormat) {
      metadata.audio = audioFormat.url;
    }
    
    // Create encrypted download links for images
    const encryptedImageUrls = imageFormats.map(img => {
      const encryptedData = encrypt(JSON.stringify({
        url: img.url,
        author: author.nickname,
        type: 'image'
      }), ENCRYPTION_KEY, 360);
      return `${BASE_URL}/download?data=${encryptedData}`;
    });
    
    metadata.download_link.no_watermark = encryptedImageUrls;
    
    if (audioFormat) {
      const encryptedAudio = encrypt(JSON.stringify({
        url: audioFormat.url,
        author: author.nickname,
        type: 'mp3'
      }), ENCRYPTION_KEY, 360);
      metadata.download_link.mp3 = `${BASE_URL}/download?data=${encryptedAudio}`;
    }
    
    // Add slideshow download link
    metadata.download_slideshow_link = `${BASE_URL}/download-slideshow?url=${encrypt(url, ENCRYPTION_KEY, 360)}`;
    
    return {
      status: 'picker',
      photos: picker,
      ...metadata
    };
  } else {
    // Regular video post
    const videoFormats = data.formats.filter(f => 
      f.vcodec && f.vcodec !== 'none' && f.acodec && f.acodec !== 'none'
    );
    
    // Find audio format
    const audioFormat = data.formats.find(f => 
      f.acodec && f.acodec !== 'none' && (!f.vcodec || f.vcodec === 'none')
    );
    
    if (audioFormat) {
      metadata.audio = audioFormat.url;
    }
    
    // Sort video formats by quality
    videoFormats.sort((a, b) => {
      const qualityA = (a.height || 0) * (a.width || 0);
      const qualityB = (b.height || 0) * (b.width || 0);
      return qualityB - qualityA;
    });
    
    // Find different quality versions
    const downloadFormat = data.formats.find(f => f.format_id === 'download');
    const hdFormats = videoFormats.filter(f => f.height >= 720);
    const sdFormats = videoFormats.filter(f => f.height < 720);
    
    const generateDownloadLink = (format, type = 'video', useStream = false) => {
      if (!format) return null;
      
      if (useStream) {
        // Use streaming endpoint for IP-restricted URLs
        const encryptedData = encrypt(JSON.stringify({
          url: url,
          format_id: format.format_id,
          author: author.nickname
        }), ENCRYPTION_KEY, 360);
        return `${BASE_URL}/stream?data=${encryptedData}`;
      } else {
        // Use direct URL download
        const encryptedData = encrypt(JSON.stringify({
          format_id: format.format_id,
          author: author.nickname,
          type: type,
          url: format.url
        }), ENCRYPTION_KEY, 360);
        return `${BASE_URL}/download?data=${encryptedData}`;
      }
    };
    
    // Create download links - use streaming for better compatibility
    metadata.download_link = {};
    
    if (downloadFormat) {
      metadata.download_link.watermark = generateDownloadLink(downloadFormat, 'video', true);
    }
    
    if (sdFormats.length > 0) {
      metadata.download_link.no_watermark = generateDownloadLink(sdFormats[0], 'video', true);
    }
    
    if (hdFormats.length > 0) {
      metadata.download_link.no_watermark_hd = generateDownloadLink(hdFormats[0], 'video', true);
      if (hdFormats.length > 1) {
        metadata.download_link.watermark_hd = generateDownloadLink(hdFormats[1], 'video', true);
      }
    }
    
    if (audioFormat) {
      metadata.download_link.mp3 = generateDownloadLink(audioFormat, 'mp3', false);
    }
    
    // Remove null values
    Object.keys(metadata.download_link).forEach(key => {
      if (metadata.download_link[key] === null) {
        delete metadata.download_link[key];
      }
    });
    
    return {
      status: 'tunnel',
      ...metadata
    };
  }
}

// ================== API ROUTES ==================

/**
 * POST /tiktok
 * Process TikTok URL and return metadata with encrypted download links
 */
app.post('/tiktok', async (req, res) => {
  try {
    const { url } = req.body;

    if (!url) {
      return res.status(400).json({ error: 'URL parameter is required' });
    }

    if (!url.includes('tiktok.com') && !url.includes('douyin.com')) {
      return res.status(400).json({ error: 'Only TikTok and Douyin URLs are supported' });
    }

    const data = await fetchTikTokData(url);
    const response = generateJsonResponse(data, url);

    return res.status(200).json(response);

  } catch (error) {
    console.error('Error in TikTok handler:', error);
    return res.status(500).json({ error: error.message || 'An error occurred processing the request' });
  }
});

/**
 * GET /download
 * Download file using encrypted data or stream from yt-dlp
 */
app.get('/download', async (req, res) => {
  try {
    const { data: encryptedData } = req.query;
    
    if (!encryptedData) {
      return res.status(400).json({ error: 'Encrypted data parameter is required' });
    }
    
    const decryptedData = decrypt(encryptedData, ENCRYPTION_KEY);
    const downloadData = JSON.parse(decryptedData);
    
    if (!downloadData.author || !downloadData.type) {
      return res.status(400).json({ error: 'Invalid decrypted data: missing author or type' });
    }
    
    if (!contentTypes[downloadData.type]) {
      return res.status(400).json({ error: 'Invalid file type specified' });
    }
    
    const [contentType, fileExtension] = contentTypes[downloadData.type];
    const filename = `${downloadData.author}.${fileExtension}`;
    const encodedFilename = encodeURIComponent(filename);
    
    // Set headers
    res.setHeader('Content-Type', contentType);
    res.setHeader('Content-Disposition', `attachment; filename="${encodedFilename}"; filename*=UTF-8''${encodedFilename}`);
    res.setHeader('X-Filename', encodedFilename);
    
    // If URL is provided directly, stream it
    if (downloadData.url) {
      const { default: got } = await import('got');
      const downloadStream = got.stream(downloadData.url, {
        timeout: {
          request: 120000
        },
        retry: {
          limit: 2
        }
      });
      
      downloadStream.on('response', (response) => {
        const contentLength = response.headers['content-length'];
        if (contentLength) {
          res.setHeader('Content-Length', contentLength);
        }
      });
      
      downloadStream.on('error', (error) => {
        console.error('Error streaming file:', error);
        if (!res.headersSent) {
          res.status(500).json({ error: error.message || 'Failed to download from source' });
        } else {
          res.end();
        }
      });
      
      downloadStream.pipe(res);
    } else {
      return res.status(400).json({ error: 'No download URL provided' });
    }
    
  } catch (error) {
    console.error('Error in download handler:', error);
    
    if (!res.headersSent) {
      return res.status(500).json({ error: error.message || 'An error occurred processing the download' });
    }
  }
});

/**
 * GET /download-slideshow
 * Generate and download slideshow video from image post
 */
app.get('/download-slideshow', async (req, res) => {
  const jobAbortController = new AbortController();
  let workDir = '';
  let ffmpegCommand = null;
  let fileStream = null;
  let tempCleaned = false;

  const cleanupTempDir = async () => {
    if (tempCleaned || !workDir) {
      return;
    }
    tempCleaned = true;
    try {
      await cleanupFolder(workDir);
    } catch (cleanupError) {
      console.error('Error removing temp folder:', cleanupError);
    }
  };

  const detachListeners = () => {
    req.removeListener('close', onRequestClose);
    res.removeListener('close', onResponseClose);
  };

  const cancelJob = async () => {
    if (!jobAbortController.signal.aborted) {
      jobAbortController.abort();
    }

    if (ffmpegCommand) {
      try {
        ffmpegCommand.kill('SIGKILL');
      } catch (error) {
        // Ignore kill errors; command may have already exited
      } finally {
        ffmpegCommand = null;
      }
    }

    if (fileStream) {
      fileStream.destroy();
      fileStream = null;
    }

    await cleanupTempDir();
    detachListeners();
  };

  function onRequestClose() {
    cancelJob().catch((err) => {
      console.error('Error cancelling slideshow job:', err);
    });
  }

  function onResponseClose() {
    if (!res.writableEnded) {
      onRequestClose();
    }
  }

  req.on('close', onRequestClose);
  res.on('close', onResponseClose);

  try {
    const { url: encryptedUrl } = req.query;

    if (!encryptedUrl) {
      await cleanupTempDir();
      detachListeners();
      return res.status(400).json({ error: 'URL parameter is required' });
    }

    const url = decrypt(encryptedUrl, ENCRYPTION_KEY);

    // Fetch TikTok data using yt-dlp
    const data = await fetchTikTokData(url);

    if (!data) {
      await cleanupTempDir();
      detachListeners();
      return res.status(500).json({ error: 'Invalid response from yt-dlp' });
    }

    // Check if it's an image post
    const isImage = data.formats && data.formats.some(f => f.format_id && f.format_id.startsWith('image-'));

    if (!isImage) {
      await cleanupTempDir();
      detachListeners();
      return res.status(400).json({ error: 'Only image posts are supported' });
    }

    // Create work directory
    const videoId = data.id || 'unknown';
    const authorId = data.uploader_id || 'unknown';
    const folderName = `${videoId}_${authorId}_${Date.now()}`;
    workDir = path.join(tempDir, folderName);

    await fs.ensureDir(workDir);

    // Get image and audio URLs
    const imageFormats = data.formats.filter(f => f.format_id && f.format_id.startsWith('image-'));
    const audioFormat = data.formats.find(f => f.format_id === 'audio');

    if (imageFormats.length === 0) {
      throw new Error('No images found');
    }

    if (!audioFormat || !audioFormat.url) {
      throw new Error('Could not find audio URL');
    }

    const imageURLs = imageFormats.map(f => f.url);
    const audioURL = audioFormat.url;

    // Download audio
    const audioPath = path.join(workDir, 'audio.mp3');
    const audioTask = downloadFile(audioURL, audioPath, { signal: jobAbortController.signal });

    // Download all images
    const imageDownloadTasks = imageURLs.map((imageUrl, index) => {
      const imagePath = path.join(workDir, `image_${index}.jpg`);
      return downloadFile(imageUrl, imagePath, { signal: jobAbortController.signal });
    });

    // Wait for all downloads to complete
    const [imagePaths] = await Promise.all([
      Promise.all(imageDownloadTasks),
      audioTask
    ]);

    // Create slideshow
    const outputPath = path.join(workDir, 'slideshow.mp4');
    await createSlideshow(imagePaths, audioPath, outputPath, {
      signal: jobAbortController.signal,
      onCommand: (command) => {
        ffmpegCommand = command;
      }
    });
    ffmpegCommand = null;

    // Prepare response
    const authorNickname = data.uploader || data.channel || 'unknown';
    const sanitized = authorNickname.replace(/[^a-zA-Z0-9]/g, '_');
    const filename = `${sanitized}_${Date.now()}.mp4`;

    const stats = await fs.stat(outputPath);

    res.setHeader('Content-Type', 'video/mp4');
    res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    res.setHeader('Content-Length', stats.size);

    fileStream = createReadStream(outputPath);

    fileStream.on('end', () => {
      fileStream = null;
      console.log('File stream ended, cleaning up folder');
      cleanupTempDir().finally(detachListeners);
    });

    fileStream.on('error', (error) => {
      console.error('Error streaming file:', error);
      cancelJob().catch((err) => {
        console.error('Error during cancellation after stream failure:', err);
      });
    });

    fileStream.pipe(res);

  } catch (error) {
    console.error('Error in slideshow handler:', error);

    if (jobAbortController.signal.aborted) {
      return;
    }

    await cleanupTempDir();
    detachListeners();

    if (!res.headersSent) {
      return res.status(500).json({ error: error.message || 'An error occurred creating the slideshow' });
    }
  }
});

/**
 * GET /stream
 * Stream video directly from yt-dlp using encrypted data
 */
app.get('/stream', async (req, res) => {
  try {
    const { data: encryptedData } = req.query;
    
    if (!encryptedData) {
      return res.status(400).json({ error: 'Encrypted data parameter is required' });
    }
    
    const decryptedData = decrypt(encryptedData, ENCRYPTION_KEY);
    const streamData = JSON.parse(decryptedData);
    
    if (!streamData.url || !streamData.format_id || !streamData.author) {
      return res.status(400).json({ error: 'Invalid decrypted data: missing url, format_id, or author' });
    }
    
    const ytDlpPath = path.resolve(__dirname, YT_DLP_PATH);
    const args = ['-f', streamData.format_id, '-o', '-', streamData.url];
    
    const ytDlpProcess = spawn(ytDlpPath, args);
    
    // Set headers
    const ext = streamData.format_id.includes('audio') ? 'mp3' : 'mp4';
    const contentType = ext === 'mp3' ? 'audio/mpeg' : 'video/mp4';
    const filename = `${streamData.author}.${ext}`;
    const encodedFilename = encodeURIComponent(filename);
    
    res.setHeader('Content-Type', contentType);
    res.setHeader('Content-Disposition', `attachment; filename="${encodedFilename}"; filename*=UTF-8''${encodedFilename}`);
    res.setHeader('X-Filename', encodedFilename);
    
    // Handle client disconnect
    req.on('close', () => {
      if (!ytDlpProcess.killed) {
        ytDlpProcess.kill('SIGKILL');
      }
    });
    
    // Pipe stdout to response
    ytDlpProcess.stdout.pipe(res);
    
    // Log errors
    ytDlpProcess.stderr.on('data', (data) => {
      console.error('yt-dlp stderr:', data.toString());
    });
    
    ytDlpProcess.on('error', (error) => {
      console.error('yt-dlp process error:', error);
      if (!res.headersSent) {
        res.status(500).json({ error: 'Failed to stream video' });
      } else {
        res.end();
      }
    });
    
    ytDlpProcess.on('close', (code) => {
      if (code !== 0 && !res.headersSent) {
        res.status(500).json({ error: `yt-dlp exited with code ${code}` });
      }
    });
    
  } catch (error) {
    console.error('Error in stream handler:', error);
    if (!res.headersSent) {
      return res.status(500).json({ error: error.message || 'An error occurred streaming the video' });
    }
  }
});

/**
 * GET /health
 * Health check endpoint
 */
app.get('/health', async (req, res) => {
  const health = {
    status: 'ok',
    time: new Date().toISOString(),
    ytdlp: 'unknown'
  };

  // Test yt-dlp
  try {
    const ytDlpPath = path.resolve(__dirname, YT_DLP_PATH);
    await new Promise((resolve, reject) => {
      const process = spawn(ytDlpPath, ['--version']);
      let version = '';
      
      process.stdout.on('data', (data) => {
        version += data.toString();
      });
      
      process.on('close', (code) => {
        if (code === 0) {
          health.ytdlp = version.trim();
          resolve();
        } else {
          reject(new Error('yt-dlp not working'));
        }
      });
      
      process.on('error', reject);
    });
  } catch (error) {
    health.ytdlp = 'offline';
  }

  res.status(200).json(health);
});

// Add 404 handler
app.use((req, res) => {
  res.status(404).json({ error: 'Route not found' });
});

// Global error handler
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).json({
    error: 'Unexpected server error',
    message: err.message
  });
});

// Start the server
app.listen(PORT, () => {
  console.log(`Server started on port ${PORT}`);
  console.log(`Base URL: ${BASE_URL}`);
  console.log(`yt-dlp path: ${path.resolve(__dirname, YT_DLP_PATH)}`);
});
