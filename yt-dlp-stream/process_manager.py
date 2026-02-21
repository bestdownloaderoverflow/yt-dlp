"""
Process Manager untuk tracking dan cleanup automatic.

Menangani:
- FFmpeg process tracking
- Zombie process cleanup
- Resource leak prevention
- Graceful shutdown
"""
import asyncio
import logging
import psutil
import signal
import threading
import time
from typing import Dict, Set, Optional
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("uvicorn.error")


@dataclass
class ProcessInfo:
    """Info tentang running process."""
    pid: int
    process: any  # subprocess.Popen
    created: float
    stream_id: Optional[str] = None
    type: str = "ffmpeg"  # ffmpeg, download, etc


class ProcessManager:
    """
    Singleton manager untuk tracking dan cleanup processes.
    
    Features:
    - Track semua FFmpeg dan download processes
    - Auto-cleanup zombie/orphan processes
    - Graceful shutdown semua processes
    - Resource usage monitoring
    """
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_manager()
        return cls._instance

    def _init_manager(self):
        """Initialize manager state."""
        self._processes: Dict[int, ProcessInfo] = {}
        self._process_lock = threading.Lock()
        self._cleanup_interval = 60  # Cleanup every minute
        self._max_process_age = 3600  # Kill processes older than 1 hour
        self._shutdown_event = threading.Event()
        
        # Start background cleanup thread
        self._cleanup_thread = threading.Thread(
            target=self._background_cleanup,
            daemon=True,
            name="ProcessCleanup"
        )
        self._cleanup_thread.start()
        
        logger.info("ProcessManager initialized")

    def register_process(
        self,
        process,
        stream_id: Optional[str] = None,
        process_type: str = "ffmpeg"
    ) -> int:
        """Register new process untuk tracking."""
        with self._process_lock:
            info = ProcessInfo(
                pid=process.pid,
                process=process,
                created=time.time(),
                stream_id=stream_id,
                type=process_type
            )
            self._processes[process.pid] = info
            logger.info(f"Registered {process_type} process PID={process.pid} stream_id={stream_id}")
            return process.pid

    def unregister_process(self, pid: int):
        """Unregister process setelah selesai."""
        with self._process_lock:
            if pid in self._processes:
                info = self._processes.pop(pid)
                logger.info(f"Unregistered process PID={pid} type={info.type}")

    def terminate_process(self, pid: int, timeout: float = 5.0) -> bool:
        """
        Terminate process dengan graceful shutdown.
        
        Returns True jika berhasil terminated.
        """
        with self._process_lock:
            if pid not in self._processes:
                return False
            
            info = self._processes[pid]
            process = info.process
            
            try:
                # Try graceful termination first
                process.terminate()
                
                # Wait for process to exit
                try:
                    process.wait(timeout=timeout)
                    logger.info(f"Process PID={pid} terminated gracefully")
                except:
                    # Force kill if not responding
                    process.kill()
                    process.wait(timeout=2)
                    logger.warning(f"Process PID={pid} force killed")
                
                self._processes.pop(pid, None)
                return True
                
            except Exception as e:
                logger.error(f"Failed to terminate process PID={pid}: {e}")
                return False

    def _background_cleanup(self):
        """Background thread untuk periodic cleanup."""
        while not self._shutdown_event.is_set():
            try:
                self._cleanup_dead_processes()
                self._cleanup_old_processes()
                self._cleanup_zombies()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            
            # Sleep with interruptible wait
            self._shutdown_event.wait(self._cleanup_interval)

    def _cleanup_dead_processes(self):
        """Cleanup processes yang sudah selesai tapi belum di-unregister."""
        with self._process_lock:
            dead_pids = []
            
            for pid, info in self._processes.items():
                try:
                    # Check if process still running
                    if info.process.poll() is not None:
                        dead_pids.append(pid)
                except:
                    dead_pids.append(pid)
            
            for pid in dead_pids:
                info = self._processes.pop(pid, None)
                if info:
                    logger.info(f"Cleaned up dead process PID={pid} type={info.type}")

    def _cleanup_old_processes(self):
        """Kill processes yang berjalan terlalu lama (possible stuck)."""
        now = time.time()
        with self._process_lock:
            old_pids = []
            
            for pid, info in self._processes.items():
                age = now - info.created
                if age > self._max_process_age:
                    old_pids.append(pid)
            
            for pid in old_pids:
                info = self._processes.get(pid)
                if info:
                    logger.warning(
                        f"Killing old process PID={pid} type={info.type} "
                        f"age={int(now - info.created)}s"
                    )
                    try:
                        info.process.kill()
                        info.process.wait(timeout=2)
                    except:
                        pass
                    self._processes.pop(pid, None)

    def _cleanup_zombies(self):
        """Find dan cleanup zombie/orphan ffmpeg processes owned by this service."""
        try:
            parent = psutil.Process()
            for proc in parent.children(recursive=True):
                try:
                    info = proc.as_dict(attrs=['pid', 'name', 'status', 'create_time'])
                    name = (info.get('name') or '').lower()
                    if 'ffmpeg' not in name:
                        continue

                    pid = info['pid']

                    # Skip if we're already tracking it
                    with self._process_lock:
                        if pid in self._processes:
                            continue

                    status = info.get('status')
                    created = info.get('create_time') or time.time()
                    is_old = (time.time() - created) > self._max_process_age

                    if status == psutil.STATUS_ZOMBIE:
                        logger.warning(f"Found zombie child ffmpeg process PID={pid}")
                        proc.kill()
                    elif is_old:
                        logger.warning(f"Found old untracked child ffmpeg process PID={pid}")
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as e:
            logger.error(f"Zombie cleanup error: {e}")

    def shutdown(self, timeout: float = 10.0):
        """Graceful shutdown - terminate all processes."""
        logger.info("ProcessManager shutdown initiated")
        
        # Signal cleanup thread to stop
        self._shutdown_event.set()
        
        # Terminate all tracked processes
        with self._process_lock:
            pids = list(self._processes.keys())
        
        for pid in pids:
            self.terminate_process(pid, timeout=2)
        
        # Wait for cleanup thread
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)
        
        logger.info("ProcessManager shutdown complete")

    @property
    def stats(self) -> Dict:
        """Get statistics tentang tracked processes."""
        with self._process_lock:
            now = time.time()
            return {
                "total_processes": len(self._processes),
                "processes_by_type": self._count_by_type(),
                "oldest_process_age": self._get_oldest_age(now),
                "processes": [
                    {
                        "pid": pid,
                        "type": info.type,
                        "stream_id": info.stream_id,
                        "age_seconds": int(now - info.created),
                    }
                    for pid, info in self._processes.items()
                ]
            }

    def _count_by_type(self) -> Dict[str, int]:
        """Count processes by type."""
        counts = {}
        for info in self._processes.values():
            counts[info.type] = counts.get(info.type, 0) + 1
        return counts

    def _get_oldest_age(self, now: float) -> Optional[int]:
        """Get age of oldest process in seconds."""
        if not self._processes:
            return None
        oldest = min(info.created for info in self._processes.values())
        return int(now - oldest)


# Global singleton instance
process_manager = ProcessManager()


def cleanup_on_shutdown():
    """Called saat aplikasi shutdown."""
    process_manager.shutdown()


# Register cleanup handler
import atexit
atexit.register(cleanup_on_shutdown)
