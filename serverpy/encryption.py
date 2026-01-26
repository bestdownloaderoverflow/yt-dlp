"""Encryption and decryption utilities"""

import base64
import time
from typing import Optional


def encrypt(text: str, key: str, expiry_minutes: Optional[int] = None) -> str:
    """
    Encrypt text using XOR cipher with base64 encoding
    Compatible with serverjs encryption
    
    Args:
        text: Text to encrypt
        key: Encryption key
        expiry_minutes: Optional expiry time in minutes
    
    Returns:
        Base64 encoded encrypted string
    """
    if expiry_minutes:
        # Add expiry timestamp
        expiry_time = int(time.time()) + (expiry_minutes * 60)
        text = f"{expiry_time}|{text}"
    
    # XOR encryption
    encrypted = bytearray()
    key_bytes = key.encode('utf-8')
    text_bytes = text.encode('utf-8')
    
    for i, char in enumerate(text_bytes):
        key_char = key_bytes[i % len(key_bytes)]
        encrypted.append(char ^ key_char)
    
    # Base64 encode
    return base64.urlsafe_b64encode(encrypted).decode('utf-8')


def decrypt(encrypted_text: str, key: str) -> str:
    """
    Decrypt text encrypted with encrypt()
    Compatible with serverjs decryption
    
    Args:
        encrypted_text: Base64 encoded encrypted string
        key: Decryption key
    
    Returns:
        Decrypted text
    
    Raises:
        ValueError: If decryption fails or data is expired
    """
    try:
        # Base64 decode
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_text.encode('utf-8'))
        
        # XOR decryption
        decrypted = bytearray()
        key_bytes = key.encode('utf-8')
        
        for i, char in enumerate(encrypted_bytes):
            key_char = key_bytes[i % len(key_bytes)]
            decrypted.append(char ^ key_char)
        
        decrypted_text = decrypted.decode('utf-8')
        
        # Check for expiry
        if '|' in decrypted_text:
            parts = decrypted_text.split('|', 1)
            if len(parts) == 2:
                try:
                    expiry_time = int(parts[0])
                    if time.time() > expiry_time:
                        raise ValueError("Encrypted data has expired")
                    return parts[1]
                except ValueError as e:
                    if "expired" in str(e):
                        raise
                    # Not a timestamp, return as-is
                    pass
        
        return decrypted_text
    
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}")


def test_encryption():
    """Test encryption/decryption"""
    key = "testkey"
    text = "Hello, World!"
    
    # Test without expiry
    encrypted = encrypt(text, key)
    decrypted = decrypt(encrypted, key)
    assert decrypted == text, "Encryption/decryption failed"
    
    # Test with expiry
    encrypted_expiry = encrypt(text, key, expiry_minutes=1)
    decrypted_expiry = decrypt(encrypted_expiry, key)
    assert decrypted_expiry == text, "Encryption/decryption with expiry failed"
    
    print("✅ Encryption tests passed")


if __name__ == "__main__":
    test_encryption()
