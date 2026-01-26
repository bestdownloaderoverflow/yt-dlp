import crypto from 'crypto';

/**
 * Encrypt data with AES-256-GCM and TTL
 * @param {string} text - Text to encrypt
 * @param {string} secretKey - Secret key for encryption
 * @param {number} ttlSeconds - Time to live in seconds (default: 360 = 6 minutes)
 * @returns {string} - Base64 encoded encrypted data
 */
export function encrypt(text, secretKey, ttlSeconds = 360) {
  try {
    const algorithm = 'aes-256-gcm';
    const key = crypto.scryptSync(secretKey, 'salt', 32);
    const iv = crypto.randomBytes(16);
    const cipher = crypto.createCipheriv(algorithm, key, iv);
    
    // Add expiration timestamp
    const expiresAt = Date.now() + (ttlSeconds * 1000);
    const payload = JSON.stringify({
      data: text,
      exp: expiresAt
    });
    
    let encrypted = cipher.update(payload, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    
    const authTag = cipher.getAuthTag();
    
    // Combine iv + authTag + encrypted
    const combined = Buffer.concat([
      iv,
      authTag,
      Buffer.from(encrypted, 'hex')
    ]);
    
    return combined.toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=/g, '');
  } catch (error) {
    throw new Error(`Encryption failed: ${error.message}`);
  }
}

/**
 * Decrypt data encrypted with encrypt function
 * @param {string} encryptedText - Base64 encoded encrypted text
 * @param {string} secretKey - Secret key for decryption
 * @returns {string} - Decrypted text
 */
export function decrypt(encryptedText, secretKey) {
  try {
    const algorithm = 'aes-256-gcm';
    const key = crypto.scryptSync(secretKey, 'salt', 32);
    
    // Restore base64 padding
    let base64 = encryptedText
      .replace(/-/g, '+')
      .replace(/_/g, '/');
    
    while (base64.length % 4) {
      base64 += '=';
    }
    
    const combined = Buffer.from(base64, 'base64');
    
    // Extract iv (16 bytes), authTag (16 bytes), and encrypted data
    const iv = combined.slice(0, 16);
    const authTag = combined.slice(16, 32);
    const encrypted = combined.slice(32);
    
    const decipher = crypto.createDecipheriv(algorithm, key, iv);
    decipher.setAuthTag(authTag);
    
    let decrypted = decipher.update(encrypted, undefined, 'utf8');
    decrypted += decipher.final('utf8');
    
    // Parse payload and check expiration
    const payload = JSON.parse(decrypted);
    
    if (Date.now() > payload.exp) {
      throw new Error('Encrypted data has expired');
    }
    
    return payload.data;
  } catch (error) {
    if (error.message === 'Encrypted data has expired') {
      throw error;
    }
    throw new Error(`Decryption failed: ${error.message}`);
  }
}
