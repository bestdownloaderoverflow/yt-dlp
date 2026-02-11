use base64::{engine::general_purpose::URL_SAFE, Engine};
use std::time::{SystemTime, UNIX_EPOCH};

/// Encrypt text using XOR cipher with base64url encoding.
/// Compatible with serverjs/serverpy encryption.
pub fn encrypt(text: &str, key: &str, expiry_minutes: Option<u64>) -> String {
    let text_with_expiry = if let Some(minutes) = expiry_minutes {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let expiry_time = now + (minutes * 60);
        format!("{expiry_time}|{text}")
    } else {
        text.to_string()
    };

    let key_bytes = key.as_bytes();
    let text_bytes = text_with_expiry.as_bytes();

    let encrypted: Vec<u8> = text_bytes
        .iter()
        .enumerate()
        .map(|(i, &b)| b ^ key_bytes[i % key_bytes.len()])
        .collect();

    URL_SAFE.encode(&encrypted)
}

/// Decrypt text encrypted with encrypt().
/// Compatible with serverjs/serverpy decryption.
pub fn decrypt(encrypted_text: &str, key: &str) -> Result<String, String> {
    let encrypted_bytes = URL_SAFE
        .decode(encrypted_text.as_bytes())
        .map_err(|e| format!("Base64 decode failed: {e}"))?;

    let key_bytes = key.as_bytes();

    let decrypted: Vec<u8> = encrypted_bytes
        .iter()
        .enumerate()
        .map(|(i, &b)| b ^ key_bytes[i % key_bytes.len()])
        .collect();

    let decrypted_text =
        String::from_utf8(decrypted).map_err(|e| format!("UTF-8 decode failed: {e}"))?;

    // Check for expiry
    if let Some(pipe_pos) = decrypted_text.find('|') {
        let timestamp_str = &decrypted_text[..pipe_pos];
        if let Ok(expiry_time) = timestamp_str.parse::<u64>() {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs();
            if now > expiry_time {
                return Err("Encrypted data has expired".to_string());
            }
            return Ok(decrypted_text[pipe_pos + 1..].to_string());
        }
        // Not a timestamp, return as-is
    }

    Ok(decrypted_text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encrypt_decrypt_no_expiry() {
        let key = "testkey";
        let text = "Hello, World!";
        let encrypted = encrypt(text, key, None);
        let decrypted = decrypt(&encrypted, key).unwrap();
        assert_eq!(decrypted, text);
    }

    #[test]
    fn test_encrypt_decrypt_with_expiry() {
        let key = "testkey";
        let text = "Hello, World!";
        let encrypted = encrypt(text, key, Some(1));
        let decrypted = decrypt(&encrypted, key).unwrap();
        assert_eq!(decrypted, text);
    }

    #[test]
    fn test_json_payload() {
        let key = "overflow";
        let payload = r#"{"url":"https://example.com","author":"test","type":"video"}"#;
        let encrypted = encrypt(payload, key, Some(360));
        let decrypted = decrypt(&encrypted, key).unwrap();
        assert_eq!(decrypted, payload);
    }
}
