use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

const SIGNATURE_TTL_SECS: u64 = 120; // 2 minutes

/// Generate a signed URL token for streaming
/// Format: {url}|{format}|{expires_at}
/// Signature: HMAC-SHA256(payload, secret)
pub fn generate_stream_token(url: &str, format: &str, secret: &str) -> (String, u64) {
    let expires_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
        + SIGNATURE_TTL_SECS;

    let payload = format!("{}|{}|{}", url, format, expires_at);

    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .expect("HMAC can take key of any size");
    mac.update(payload.as_bytes());
    let signature = hex::encode(mac.finalize().into_bytes());

    (signature, expires_at)
}

/// Verify a stream token signature and check expiry
pub fn verify_stream_token(
    url: &str,
    format: &str,
    expires_at: u64,
    signature: &str,
    secret: &str,
) -> Result<(), SignatureError> {
    // Check expiry first
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();

    if now > expires_at {
        return Err(SignatureError::Expired);
    }

    // Verify signature
    let payload = format!("{}|{}|{}", url, format, expires_at);

    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .expect("HMAC can take key of any size");
    mac.update(payload.as_bytes());

    let expected = hex::encode(mac.finalize().into_bytes());

    if signature != expected {
        return Err(SignatureError::Invalid);
    }

    Ok(())
}

#[derive(Debug)]
pub enum SignatureError {
    Expired,
    Invalid,
}

impl std::fmt::Display for SignatureError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SignatureError::Expired => write!(f, "Link expired"),
            SignatureError::Invalid => write!(f, "Invalid signature"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_signature_roundtrip() {
        let secret = "test_secret_key";
        let url = "https://youtube.com/watch?v=abc123";
        let format = "best";

        let (sig, expires) = generate_stream_token(url, format, secret);
        assert!(verify_stream_token(url, format, expires, &sig, secret).is_ok());
    }

    #[test]
    fn test_invalid_signature() {
        let secret = "test_secret_key";
        let url = "https://youtube.com/watch?v=abc123";
        let format = "best";

        let (_, expires) = generate_stream_token(url, format, secret);
        assert!(verify_stream_token(url, format, expires, "invalid", secret).is_err());
    }
}
