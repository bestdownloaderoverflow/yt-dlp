// MongoDB initialization script
// Run on first container startup

db = db.getSiblingDB('yt_dlp_db');

// Create indexes for download_jobs collection
db.download_jobs.createIndex({ "job_id": 1 }, { unique: true });
db.download_jobs.createIndex({ "status": 1 });
db.download_jobs.createIndex({ "created_at": 1 });
db.download_jobs.createIndex({ "updated_at": 1 });
// Note: No TTL index on expires_at - we do manual cleanup via Celery
// to track cleaned jobs and preserve history

db.download_jobs.createIndex({ "expires_at": 1 });  // Regular index for queries

// Create indexes for Celery results (if using MongoDB as result backend)
db.yt_dlp_celery_results.createIndex({ "date_done": 1 }, { expireAfterSeconds: 604800 }); // 7 days

print("MongoDB indexes created successfully");
