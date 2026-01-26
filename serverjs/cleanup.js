import fs from 'fs-extra';
import path from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import cron from 'node-cron';

// Get the directory name
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Temp directory path
const tempDir = path.join(__dirname, 'temp');

/**
 * Removes a specific folder from the temp directory
 * @param {string} folderPath - Path to the folder that should be removed
 * @returns {Promise<void>}
 */
export async function cleanupFolder(folderPath) {
  try {
    console.log(`Cleaning up folder: ${folderPath}`);
    await fs.remove(folderPath);
    console.log(`Successfully removed folder: ${folderPath}`);
  } catch (error) {
    console.error(`Error removing folder ${folderPath}:`, error);
  }
}

/**
 * Removes all folders in the temp directory that are older than the specified age
 * @param {number} maxAgeMs - Maximum age of folders in milliseconds before they're removed
 * @returns {Promise<number>} - Number of folders removed
 */
export async function cleanupOldFolders(maxAgeMs = 60 * 60 * 1000) { // Default: 1 hour
  try {
    console.log(`Starting cleanup of folders older than ${maxAgeMs/1000/60} minutes...`);
    const now = Date.now();
    let removedCount = 0;
    
    // Create temp directory if it doesn't exist
    await fs.ensureDir(tempDir);
    
    // Get all folders in the temp directory
    const items = await fs.readdir(tempDir);
    
    for (const item of items) {
      const itemPath = path.join(tempDir, item);
      
      try {
        // Get folder stats
        const stats = await fs.stat(itemPath);
        
        // Check if it's a directory
        if (stats.isDirectory()) {
          // If folder is older than the specified age, remove it
          if (now - stats.ctimeMs > maxAgeMs) {
            console.log(`Found old folder (${item}, age: ${Math.round((now - stats.ctimeMs)/1000/60)} minutes), removing...`);
            await cleanupFolder(itemPath);
            removedCount++;
          }
        }
      } catch (error) {
        console.error(`Error processing ${item}:`, error);
      }
    }
    
    console.log(`Cleanup complete. Removed ${removedCount} folders.`);
    return removedCount;
  } catch (error) {
    console.error('Error in cleanupOldFolders:', error);
    return 0;
  }
}

/**
 * Initialize the cleanup schedule
 * @param {string} schedule - Cron schedule expression (default: run every 15 minutes)
 */
export function initCleanupSchedule(schedule = '*/15 * * * *') {
  // Validate the schedule
  if (!cron.validate(schedule)) {
    console.error(`Invalid cron schedule: ${schedule}`);
    console.log('Using default schedule: Every 15 minutes');
    schedule = '*/15 * * * *';
  }
  
  console.log(`Setting up cleanup schedule: ${schedule}`);
  
  // Schedule the cleanup task
  cron.schedule(schedule, async () => {
    console.log(`Running scheduled cleanup at ${new Date().toISOString()}`);
    await cleanupOldFolders();
  });
  
  // Run an initial cleanup on startup
  cleanupOldFolders();
  
  console.log('Cleanup scheduler initialized');
}

// If this file is run directly, perform a cleanup immediately
if (import.meta.url === `file://${process.argv[1]}`) {
  console.log('Running cleanup directly');
  cleanupOldFolders().then(count => {
    console.log(`Cleaned up ${count} folders`);
    process.exit(0);
  }).catch(err => {
    console.error('Error during cleanup:', err);
    process.exit(1);
  });
}
