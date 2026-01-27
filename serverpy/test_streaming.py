#!/usr/bin/env python3
"""
Test script untuk memverifikasi implementasi streaming yt-dlp
"""
import sys
import os
from pathlib import Path

# Add parent directory to path
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

import yt_dlp
from queue import Queue
import threading
import time

def test_custom_stream():
    """Test apakah output_stream parameter bekerja dengan benar"""
    print("=" * 60)
    print("TEST 1: Custom Stream Parameter")
    print("=" * 60)
    
    class TestWriter:
        def __init__(self):
            self.data = bytearray()
            self.write_count = 0
            self.closed = False
            self.mode = 'wb'
            self.name = '<test>'
            
        def write(self, data):
            if isinstance(data, str):
                data = data.encode('utf-8')
            self.data.extend(data)
            self.write_count += 1
            return len(data)
        
        def flush(self):
            pass
        
        def close(self):
            self.closed = True
            
        def writable(self):
            return not self.closed
        
        def readable(self):
            return False
        
        def seekable(self):
            return False
    
    # Test dengan URL TikTok sederhana (hanya extract info, tidak download penuh)
    test_url = 'https://www.tiktok.com/@scout2015/video/6718335390845095173'
    
    writer = TestWriter()
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': '-',
        'output_stream': writer,  # Parameter kustom kita
        'test': True,  # Hanya download sebagian kecil untuk testing
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Extracting info from: {test_url}")
            info = ydl.extract_info(test_url, download=False)
            
            if info:
                print(f"✓ Video found: {info.get('title', 'Unknown')}")
                print(f"✓ Uploader: {info.get('uploader', 'Unknown')}")
                print(f"✓ Duration: {info.get('duration', 0)} seconds")
                
                # Test download dengan stream kustom
                print("\nTesting download with custom stream...")
                ydl.download([test_url])
                
                if writer.write_count > 0:
                    print(f"✓ Custom stream received data!")
                    print(f"  - Write calls: {writer.write_count}")
                    print(f"  - Total bytes: {len(writer.data)}")
                    print(f"  - Stream closed: {writer.closed}")
                    return True
                else:
                    print("✗ Custom stream did NOT receive data")
                    return False
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_queue_streaming():
    """Test apakah queue-based streaming bekerja seperti di main.py"""
    print("\n" + "=" * 60)
    print("TEST 2: Queue-Based Streaming (Production Mode)")
    print("=" * 60)
    
    class QueueWriter:
        def __init__(self, queue):
            self.queue = queue
            self.buffer = bytearray()
            self.closed = False
            self.mode = 'wb'
            self.name = '<queue>'
            
        def write(self, data):
            if self.closed:
                raise ValueError("I/O operation on closed file")
            
            if isinstance(data, str):
                data = data.encode('utf-8')
            
            self.buffer.extend(data)
            
            # Send in 8KB chunks
            while len(self.buffer) >= 8192:
                chunk = bytes(self.buffer[:8192])
                self.buffer = self.buffer[8192:]
                self.queue.put(chunk)
            
            return len(data)
        
        def flush(self):
            if not self.closed and self.buffer:
                self.queue.put(bytes(self.buffer))
                self.buffer = bytearray()
        
        def close(self):
            if not self.closed:
                self.flush()
                self.closed = True
        
        def writable(self):
            return not self.closed
        
        def readable(self):
            return False
        
        def seekable(self):
            return False
    
    test_url = 'https://www.tiktok.com/@scout2015/video/6718335390845095173'
    chunk_queue = Queue(maxsize=20)
    received_chunks = []
    error_dict = {'error': None}
    
    def download_thread():
        try:
            queue_writer = QueueWriter(chunk_queue)
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'outtmpl': '-',
                'output_stream': queue_writer,
                'test': True,  # Hanya download sebagian
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([test_url])
            
            queue_writer.flush()
            chunk_queue.put(None)  # End signal
            
        except Exception as e:
            error_dict['error'] = str(e)
            chunk_queue.put(None)
    
    # Start download thread
    thread = threading.Thread(target=download_thread, daemon=True)
    thread.start()
    
    # Consume chunks
    print("Waiting for chunks...")
    timeout = 30
    start_time = time.time()
    
    while True:
        if time.time() - start_time > timeout:
            print("✗ Timeout waiting for chunks")
            return False
        
        try:
            chunk = chunk_queue.get(timeout=5)
            
            if chunk is None:  # End signal
                break
            
            received_chunks.append(chunk)
            print(f"  Received chunk #{len(received_chunks)}: {len(chunk)} bytes")
            
        except Exception as e:
            print(f"✗ Error receiving chunk: {e}")
            break
    
    thread.join(timeout=5)
    
    if error_dict['error']:
        print(f"✗ Download error: {error_dict['error']}")
        return False
    
    if received_chunks:
        total_bytes = sum(len(c) for c in received_chunks)
        print(f"\n✓ Queue streaming successful!")
        print(f"  - Total chunks: {len(received_chunks)}")
        print(f"  - Total bytes: {total_bytes}")
        return True
    else:
        print("✗ No chunks received")
        return False


def test_without_custom_stream():
    """Test bahwa download normal masih berfungsi (tanpa output_stream)"""
    print("\n" + "=" * 60)
    print("TEST 3: Normal Download (Without Custom Stream)")
    print("=" * 60)
    
    test_url = 'https://www.tiktok.com/@scout2015/video/6718335390845095173'
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'outtmpl': '/tmp/test_yt_dlp_%(id)s.%(ext)s',
        'test': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Testing normal download to file...")
            ydl.download([test_url])
            print("✓ Normal download still works (no regression)")
            return True
    except Exception as e:
        print(f"✗ Error: {e}")
        return False


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("VERIFIKASI IMPLEMENTASI STREAMING YT-DLP")
    print("=" * 60)
    print()
    
    results = []
    
    # Run tests
    results.append(("Custom Stream Parameter", test_custom_stream()))
    results.append(("Queue-Based Streaming", test_queue_streaming()))
    results.append(("Normal Download (Regression)", test_without_custom_stream()))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(r[1] for r in results)
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✓ ALL TESTS PASSED - Implementasi sudah benar!")
    else:
        print("✗ SOME TESTS FAILED - Ada masalah dengan implementasi")
    print("=" * 60)
    
    sys.exit(0 if all_passed else 1)
