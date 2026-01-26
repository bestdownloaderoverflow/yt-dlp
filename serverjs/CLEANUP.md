# Cleanup Mechanism Documentation

## 🧹 Automatic Cleanup System

Server memiliki automatic cleanup mechanism untuk menghapus temporary folders yang sudah tidak digunakan.

---

## 📋 Overview

### Why Cleanup is Needed

**Problem:**
- Slideshow generation creates temporary folders
- If client disconnects or errors occur, folders may not be cleaned up immediately
- Over time, temp folders can accumulate and fill disk space

**Solution:**
- Automatic scheduled cleanup every 15 minutes
- Removes folders older than 1 hour
- Manual cleanup option available

---

## 🔧 How It Works

### Cleanup Schedule

```javascript
// Runs every 15 minutes
initCleanupSchedule();
```

**Schedule:**
- **Frequency:** Every 15 minutes
- **Max Age:** 1 hour (folders older than this are removed)
- **Auto Start:** Runs on server startup

### Cleanup Process

1. **Scan** temp directory for all folders
2. **Check** age of each folder (based on creation time)
3. **Remove** folders older than 1 hour
4. **Log** cleanup results

---

## 📁 Files

### `cleanup.js`

Main cleanup module with three functions:

#### 1. `cleanupFolder(folderPath)`

Remove a specific folder.

```javascript
import { cleanupFolder } from './cleanup.js';

await cleanupFolder('/path/to/temp/folder');
```

**Parameters:**
- `folderPath` (string) - Path to folder to remove

**Returns:** Promise<void>

---

#### 2. `cleanupOldFolders(maxAgeMs)`

Remove all folders older than specified age.

```javascript
import { cleanupOldFolders } from './cleanup.js';

// Remove folders older than 1 hour (default)
await cleanupOldFolders();

// Remove folders older than 30 minutes
await cleanupOldFolders(30 * 60 * 1000);
```

**Parameters:**
- `maxAgeMs` (number) - Maximum age in milliseconds (default: 3600000 = 1 hour)

**Returns:** Promise<number> - Number of folders removed

---

#### 3. `initCleanupSchedule(schedule)`

Initialize automatic cleanup schedule.

```javascript
import { initCleanupSchedule } from './cleanup.js';

// Start automatic cleanup (every 15 minutes - default)
initCleanupSchedule('*/15 * * * *');

// Or use default
initCleanupSchedule();
```

**Parameters:**
- `schedule` (string) - Cron expression (default: `'*/15 * * * *'`)

**Behavior:**
- Runs cleanup based on cron schedule
- Performs initial cleanup on startup
- Continues running in background

---

## 🎯 Usage

### Automatic Cleanup (Default)

Server automatically starts cleanup on startup:

```javascript
// In index.js
import { initCleanupSchedule } from './cleanup.js';

initCleanupSchedule('*/15 * * * *'); // Runs every 15 minutes
```

**No configuration needed!** Cleanup runs automatically.

---

### Manual Cleanup

Run cleanup manually when needed:

```bash
# Option 1: Using npm script
npm run cleanup

# Option 2: Direct execution
node cleanup.js
```

**Output:**
```
Running cleanup directly
Starting cleanup of folders older than 60 minutes...
Found old folder (slideshow_1234567890, age: 75 minutes), removing...
Cleaning up folder: /app/temp/slideshow_1234567890
Successfully removed folder: /app/temp/slideshow_1234567890
Cleanup complete. Removed 1 folders.
Cleaned up 1 folders
```

---

## 📊 Monitoring

### Check Cleanup Logs

```bash
# View server logs
docker-compose logs -f yt-dlp-server | grep -i cleanup

# Or without Docker
tail -f logs/server.log | grep -i cleanup
```

**Example logs:**
```
Setting up cleanup schedule: Every 15 minutes
Cleanup scheduler initialized
Starting cleanup of folders older than 60 minutes...
Cleanup complete. Removed 0 folders.
Running scheduled cleanup at 2026-01-26T08:00:00.000Z
Starting cleanup of folders older than 60 minutes...
Found old folder (slideshow_1737878400, age: 62 minutes), removing...
Cleaning up folder: /app/serverjs/temp/slideshow_1737878400
Successfully removed folder: /app/serverjs/temp/slideshow_1737878400
Cleanup complete. Removed 1 folders.
```

---

## ⚙️ Configuration

### Change Cleanup Frequency

Edit `index.js`:

```javascript
// Change cron schedule (default: every 15 minutes)
initCleanupSchedule('*/15 * * * *'); // Change this value
```

**Cron Expression Examples:**
- `'*/5 * * * *'` - Every 5 minutes
- `'*/30 * * * *'` - Every 30 minutes
- `'0 * * * *'` - Every hour
- `'0 */2 * * *'` - Every 2 hours
- `'0 0 * * *'` - Every day at midnight

**Cron Format:**
```
* * * * *
│ │ │ │ │
│ │ │ │ └─── Day of week (0-7, Sun-Sat)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

---

### Change Max Age

Edit `cleanup.js`:

```javascript
export async function cleanupOldFolders(maxAgeMs = 60 * 60 * 1000) {
  // Change default max age (default: 1 hour)
  // ...
}
```

**Examples:**
- `30 * 60 * 1000` - 30 minutes
- `2 * 60 * 60 * 1000` - 2 hours
- `24 * 60 * 60 * 1000` - 24 hours

---

## 🧪 Testing

### Test Cleanup Manually

```bash
# Create test folder
mkdir -p temp/test_folder_$(date +%s)

# Wait a moment, then run cleanup
npm run cleanup
```

### Test Automatic Cleanup

```bash
# Start server
npm start

# Check logs for cleanup messages
# Should see "Cleanup scheduler initialized"
# And periodic "Running scheduled cleanup" messages
```

---

## 🔍 Troubleshooting

### Issue: Cleanup not running

**Check:**
```bash
# Verify cleanup is initialized
docker-compose logs yt-dlp-server | grep "Cleanup scheduler"
```

**Expected:** `Cleanup scheduler initialized`

**If not found:**
- Check `index.js` has `initCleanupSchedule()` call
- Restart server

---

### Issue: Folders not being removed

**Possible causes:**
1. Folders are not old enough (< 1 hour)
2. Permission issues
3. Folders are in use

**Debug:**
```bash
# Check folder ages
ls -lah temp/

# Run manual cleanup with logs
node cleanup.js
```

---

### Issue: Too many folders accumulating

**Solutions:**

1. **Decrease max age:**
   ```javascript
   // In cleanup.js
   export async function cleanupOldFolders(maxAgeMs = 30 * 60 * 1000) {
     // Now removes folders older than 30 minutes
   ```

2. **Increase cleanup frequency:**
   ```javascript
   // In index.js
   initCleanupSchedule('*/5 * * * *'); // Every 5 minutes
   ```

3. **Manual cleanup:**
   ```bash
   npm run cleanup
   ```

---

## 📈 Best Practices

### Production

1. **Monitor disk space:**
   ```bash
   df -h
   ```

2. **Set up alerts:**
   - Alert when disk usage > 80%
   - Alert when temp folder size > 1GB

3. **Regular manual cleanup:**
   ```bash
   # Add to crontab
   0 */6 * * * cd /path/to/serverjs && npm run cleanup
   ```

### Development

1. **More frequent cleanup:**
   - Every 5-10 minutes
   - Max age: 30 minutes

2. **Manual cleanup after testing:**
   ```bash
   npm run cleanup
   ```

---

## 🎯 Comparison with downloader-bun

### Similarities ✅

| Feature | downloader-bun | yt-dlp-server | Status |
|---------|----------------|---------------|--------|
| `cleanupFolder()` | ✅ | ✅ | Same |
| `cleanupOldFolders()` | ✅ | ✅ | Same |
| `initCleanupSchedule()` | ✅ | ✅ | Same |
| Auto cleanup on startup | ✅ | ✅ | Same |
| Manual cleanup script | ✅ | ✅ | Same |

### Differences

| Feature | downloader-bun | yt-dlp-server |
|---------|----------------|---------------|
| Cleanup frequency | Every 15 min (cron) | Every 15 min (cron) |
| Cron dependency | ✅ Uses node-cron | ✅ Uses node-cron |
| Schedule format | Cron expression | Cron expression |

**Note:** 100% identical implementation! ✅

---

## ✅ Summary

**Cleanup mechanism:**
- ✅ Automatic cleanup every 15 minutes
- ✅ Removes folders older than 1 hour
- ✅ Manual cleanup available
- ✅ 100% identical to downloader-bun
- ✅ Production ready

**Commands:**
```bash
# Start server (auto cleanup enabled)
npm start

# Manual cleanup
npm run cleanup

# Check logs
docker-compose logs -f yt-dlp-server | grep cleanup
```

**No configuration needed!** Works out of the box. 🎉
