use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::{error, info};

/// Remove a folder and all its contents (blocking)
pub fn cleanup_folder(folder_path: &str) {
    let path = Path::new(folder_path);
    if !path.exists() {
        return;
    }
    match std::fs::remove_dir_all(path) {
        Ok(_) => info!("Cleaned up folder: {folder_path}"),
        Err(e) => error!("Error cleaning up folder {folder_path}: {e}"),
    }
}

/// Remove folders older than max_age_seconds. Returns number of folders removed.
pub fn cleanup_old_folders(base_dir: &str, max_age_seconds: u64) -> usize {
    let base = Path::new(base_dir);
    if !base.exists() {
        return 0;
    }

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();

    let mut removed = 0usize;

    let entries = match std::fs::read_dir(base) {
        Ok(e) => e,
        Err(e) => {
            error!("Error scanning directory {base_dir}: {e}");
            return 0;
        }
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }

        let mtime = match entry.metadata().and_then(|m| m.modified()) {
            Ok(t) => t.duration_since(UNIX_EPOCH).unwrap().as_secs(),
            Err(_) => continue,
        };

        let age = now.saturating_sub(mtime);
        if age > max_age_seconds {
            match std::fs::remove_dir_all(&path) {
                Ok(_) => {
                    removed += 1;
                    info!("Removed old folder: {} (age: {age}s)", path.display());
                }
                Err(e) => error!("Error removing folder {}: {e}", path.display()),
            }
        }
    }

    removed
}

/// Spawn a background cleanup task that runs every 15 minutes.
/// Call this once at startup.
pub fn spawn_cleanup_task(temp_dir: String) {
    tokio::spawn(async move {
        info!("Initializing cleanup schedule for: {temp_dir}");
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(15 * 60));
        // Skip the first immediate tick
        interval.tick().await;

        loop {
            interval.tick().await;
            let dir = temp_dir.clone();
            let removed = tokio::task::spawn_blocking(move || {
                cleanup_old_folders(&dir, 3600) // 1 hour max age
            })
            .await
            .unwrap_or(0);

            if removed > 0 {
                info!("Scheduled cleanup: removed {removed} old folders");
            }
        }
    });
}
