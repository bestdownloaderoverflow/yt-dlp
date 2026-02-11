use std::path::Path;
use std::process::Command;
use tracing::{error, info};

/// Download file from URL to local path (blocking, for use in spawn_blocking)
pub fn download_file(url: &str, output_path: &str, timeout_secs: u64) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .redirect(reqwest::redirect::Policy::limited(10))
        .build()
        .map_err(|e| format!("Failed to create HTTP client: {e}"))?;

    let mut response = client
        .get(url)
        .send()
        .map_err(|e| format!("Failed to download file: {e}"))?;

    if !response.status().is_success() {
        return Err(format!("HTTP error: {}", response.status()));
    }

    let mut file =
        std::fs::File::create(output_path).map_err(|e| format!("Failed to create file: {e}"))?;

    std::io::copy(&mut response, &mut file).map_err(|e| format!("Failed to write file: {e}"))?;

    info!("Downloaded file: {output_path}");
    Ok(())
}

/// Create a slideshow video from images and audio using FFmpeg.
/// Blocking — call from spawn_blocking.
pub fn create_slideshow(
    image_paths: &[String],
    audio_path: &str,
    output_path: &str,
    duration_per_image: u32,
) -> Result<(), String> {
    if image_paths.is_empty() {
        return Err("No image paths provided".into());
    }
    if !Path::new(audio_path).exists() {
        return Err(format!("Audio file not found: {audio_path}"));
    }
    for img in image_paths {
        if !Path::new(img).exists() {
            return Err(format!("Image file not found: {img}"));
        }
    }

    let mut cmd = Command::new("ffmpeg");
    cmd.arg("-y");

    // Add each image as input with duration
    for img_path in image_paths {
        cmd.args(["-loop", "1", "-t", &duration_per_image.to_string(), "-i", img_path]);
    }

    // Add audio with loop
    cmd.args(["-stream_loop", "-1", "-i", audio_path]);

    // Build complex filter
    let mut filter_parts = Vec::new();

    // Scale and pad each image to 1080x1920 (portrait)
    for i in 0..image_paths.len() {
        filter_parts.push(format!(
            "[{i}:v]scale=w=1080:h=1920:force_original_aspect_ratio=decrease,\
             pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]"
        ));
    }

    // Concatenate all scaled/padded video streams
    let concat_inputs: String = (0..image_paths.len()).map(|i| format!("[v{i}]")).collect();
    filter_parts.push(format!(
        "{concat_inputs}concat=n={}:v=1:a=0[vout]",
        image_paths.len()
    ));

    // Calculate total video duration and trim audio
    let video_duration = image_paths.len() as u32 * duration_per_image;
    filter_parts.push(format!("[{}:a]atrim=0:{video_duration}[aout]", image_paths.len()));

    let filter_complex = filter_parts.join(";");

    cmd.args([
        "-filter_complex",
        &filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-c:a",
        "aac",
        output_path,
    ]);

    info!("Creating slideshow with {} images", image_paths.len());

    let output = cmd
        .output()
        .map_err(|e| format!("Failed to run FFmpeg: {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        error!("FFmpeg error: {stderr}");
        // Clean up partial output
        let _ = std::fs::remove_file(output_path);
        return Err(format!("FFmpeg failed with code {:?}", output.status.code()));
    }

    if !Path::new(output_path).exists() {
        return Err("Output file was not created".into());
    }

    info!("Slideshow created successfully: {output_path}");
    Ok(())
}
