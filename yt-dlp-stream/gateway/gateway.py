#!/usr/bin/env python3
"""
Smart Gateway for yt-dlp-stream dengan VPN rotation.

Fitur:
- Load balancing ke 3 worker instances
- Auto-rotate VPN saat terkena rate limit YouTube
- Health check dan auto-restart
- Retry logic dengan failover
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin

import aiohttp
from aiohttp import web, ClientTimeout
import docker
from docker.errors import DockerException

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
GATEWAY_PORT = int(os.getenv('GATEWAY_PORT', '9111'))
WORKER_COUNT = int(os.getenv('WORKER_COUNT', '3'))
GLUETUN_PASSWORD = os.getenv('GLUETUN_PASSWORD', 'secretpassword')
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
RATE_LIMIT_COOLDOWN = int(os.getenv('RATE_LIMIT_COOLDOWN', '300'))  # 5 menit
HEALTH_CHECK_INTERVAL = 30  # seconds
UPTIME_RESTART_INTERVAL = 24 * 60 * 60  # 24 jam
IP_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


@dataclass
class Worker:
    """Worker instance state"""
    id: str
    host: str
    api_port: int
    control_port: int
    healthy: bool = True
    restarting: bool = False
    restart_scheduled: bool = False
    failures: int = 0
    started_at: float = field(default_factory=time.time)
    active_requests: int = 0
    last_rate_limit: float = 0

    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.api_port}"

    @property
    def control_url(self) -> str:
        return f"http://admin:{GLUETUN_PASSWORD}@{self.host}:{self.control_port}"


class WorkerRegistry:
    """Thread-safe worker registry"""

    def __init__(self, count: int):
        self._workers: List[Worker] = []
        now = time.time()
        for i in range(1, count + 1):
            self._workers.append(Worker(
                id=f"w{i}",
                host=f"gluetun-{i}",
                api_port=9487,  # All workers use same port (different hosts)
                control_port=8000,
                started_at=now
            ))
        self._lock = asyncio.Lock()
        logger.info(f"Initialized {count} workers")

    async def get_healthy_workers(self, exclude: Optional[Set[str]] = None) -> List[Worker]:
        """Get healthy workers that are not restarting"""
        async with self._lock:
            healthy = []
            for w in self._workers:
                if w.healthy and not w.restarting and not w.restart_scheduled:
                    if exclude is None or w.id not in exclude:
                        # Check rate limit cooldown
                        if time.time() - w.last_rate_limit > RATE_LIMIT_COOLDOWN:
                            healthy.append(w)
            return healthy

    async def get_worker(self, worker_id: str) -> Optional[Worker]:
        """Find worker by ID"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    return w
            return None

    async def mark_rate_limited(self, worker_id: str):
        """Mark worker as rate limited"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    w.last_rate_limit = time.time()
                    w.failures += 1
                    logger.warning(f"[{worker_id}] Marked as rate limited")
                    break

    async def mark_failure(self, worker_id: str):
        """Record a retryable worker failure"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    w.failures += 1
                    logger.warning(f"[{worker_id}] Marked as failed")
                    break

    async def schedule_restart(self, worker_id: str):
        """Schedule worker for restart"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    if not w.restarting and not w.restart_scheduled:
                        w.restart_scheduled = True
                        logger.info(f"[{worker_id}] Restart scheduled")
                        return True
                    return False
            return False

    async def update_restart_state(self, worker_id: str, restarting: bool):
        """Update worker restarting state"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    w.restarting = restarting
                    if restarting:
                        w.healthy = False
                    break

    async def mark_restarted(self, worker_id: str, success: bool):
        """Mark worker as restarted"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    w.restarting = False
                    w.restart_scheduled = False
                    if success:
                        w.healthy = True
                        w.failures = 0
                        w.started_at = time.time()
                        logger.info(f"[{worker_id}] Restarted successfully")
                    else:
                        w.healthy = False
                        w.failures += 1
                        logger.error(f"[{worker_id}] Restart failed")
                    break

    async def get_workers_needing_restart(self) -> List[str]:
        """Get workers needing 24h uptime restart"""
        async with self._lock:
            needing = []
            for w in self._workers:
                uptime = time.time() - w.started_at
                if uptime >= UPTIME_RESTART_INTERVAL and w.healthy and not w.restarting and not w.restart_scheduled:
                    needing.append(w.id)
            return needing

    async def is_worker_idle(self, worker_id: str) -> bool:
        """Check if worker has no active requests"""
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    return w.active_requests == 0
            return False

    async def increment_active(self, worker_id: str):
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    w.active_requests += 1
                    break

    async def decrement_active(self, worker_id: str):
        async with self._lock:
            for w in self._workers:
                if w.id == worker_id:
                    if w.active_requests > 0:
                        w.active_requests -= 1
                    break


class VPNRotator:
    """Handles VPN rotation via Docker"""

    def __init__(self, registry: WorkerRegistry):
        self.registry = registry
        try:
            self.docker = docker.DockerClient(base_url='unix://var/run/docker.sock')
            logger.info("Docker client initialized")
        except DockerException as e:
            logger.error(f"Failed to connect to Docker: {e}")
            self.docker = None

    async def restart_worker(self, worker_id: str):
        """Restart gluetun and ytdlp containers for worker"""
        if not self.docker:
            logger.error("Docker not available")
            return False

        await self.registry.update_restart_state(worker_id, True)

        gluetun_name = f"ytdlp-gluetun-{worker_id[1:]}"
        ytdlp_name = f"ytdlp-stream-{worker_id[1:]}"

        try:
            logger.info(f"[{worker_id}] Restarting {gluetun_name}...")

            # Restart gluetun
            gluetun = self.docker.containers.get(gluetun_name)
            gluetun.restart(timeout=30)
            await asyncio.sleep(10)

            logger.info(f"[{worker_id}] Restarting {ytdlp_name}...")

            # Restart ytdlp-stream
            ytdlp = self.docker.containers.get(ytdlp_name)
            ytdlp.restart(timeout=30)
            await asyncio.sleep(5)

            # Health check with retries (VPN + API may need time to settle)
            healthy = False
            for _ in range(6):
                if await self._health_check(worker_id):
                    healthy = True
                    break
                await asyncio.sleep(5)

            if healthy:
                await asyncio.sleep(30)  # Wait for stabilization
                await self.registry.mark_restarted(worker_id, True)
                return True
            else:
                logger.error(f"[{worker_id}] Health check failed after restart")
                await self.registry.mark_restarted(worker_id, False)
                return False

        except Exception as e:
            logger.error(f"[{worker_id}] Restart error: {e}")
            await self.registry.mark_restarted(worker_id, False)
            return False

    async def _health_check(self, worker_id: str) -> bool:
        """Health check via Gluetun control server"""
        worker = await self.registry.get_worker(worker_id)
        if not worker:
            return False

        timeout = ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1) Gluetun public IP endpoint (may return JSON or text/plain)
                url = f"{worker.control_url}/v1/publicip/ip"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        ip = None
                        try:
                            data = await resp.json(content_type=None)
                            if isinstance(data, dict):
                                ip = data.get('public_ip') or data.get('ip')
                            elif isinstance(data, str):
                                ip = data.strip()
                        except Exception:
                            text = await resp.text()
                            ip = (text or "").strip()

                        if ip:
                            logger.info(f"[{worker_id}] New VPN IP: {ip}")
                            # Accept non-empty IP-ish payloads from gluetun
                            if IP_RE.match(ip) or ':' in ip:
                                pass
                            return await self._api_health_check(session, worker_id, worker)

                # 2) Fallback direct API health check if public IP endpoint is flaky
                return await self._api_health_check(session, worker_id, worker)
        except Exception as e:
            logger.warning(f"[{worker_id}] Health check error: {e}")

        return False

    async def _api_health_check(self, session: aiohttp.ClientSession, worker_id: str, worker: Worker) -> bool:
        """Check worker API readiness."""
        try:
            async with session.get(f"{worker.api_url}/health") as resp:
                if resp.status == 200:
                    logger.info(f"[{worker_id}] Worker API healthy")
                    return True
        except Exception as e:
            logger.warning(f"[{worker_id}] API health check error: {e}")
        return False


class Gateway:
    """Main gateway server"""

    def __init__(self):
        self.registry = WorkerRegistry(WORKER_COUNT)
        self.rotator = VPNRotator(self.registry)
        self.app = web.Application()
        self._setup_routes()
        self._restart_tasks: Dict[str, asyncio.Task] = {}
        self._restart_tasks_lock = asyncio.Lock()

        # Background tasks
        self._restart_task = None
        self._uptime_task = None

    def _setup_routes(self):
        self.app.router.add_get('/', self.handle_root)
        self.app.router.add_get('/fetch', self.handle_fetch)
        self.app.router.add_get('/download', self.handle_download)
        self.app.router.add_get('/info', self.handle_info)
        self.app.router.add_get('/stream/video', self.handle_stream)
        self.app.router.add_get('/stream/video-chunked', self.handle_stream)
        self.app.router.add_get('/stream/mp3', self.handle_stream)
        self.app.router.add_get('/stream/mp3-chunked', self.handle_stream)
        self.app.router.add_get('/stream/m4a', self.handle_stream)
        self.app.router.add_post('/tiktok', self.handle_tiktok)
        self.app.router.add_get('/tiktok/download', self.handle_tiktok_download)
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/tunnel', self.handle_tunnel)

        # CORS
        self.app.middlewares.append(self._cors_middleware)

    def _build_forward_headers(self, request, method: str = 'GET') -> Dict[str, str]:
        """
        Build headers forwarded to worker to preserve client/request semantics.
        """
        headers: Dict[str, str] = {}
        passthrough = (
            'Accept',
            'Accept-Language',
            'User-Agent',
            'Range',
            'If-Range',
        )
        for key in passthrough:
            value = request.headers.get(key)
            if value:
                headers[key] = value

        incoming_xff = request.headers.get('X-Forwarded-For')
        remote_ip = request.remote
        if incoming_xff and remote_ip:
            headers['X-Forwarded-For'] = f'{incoming_xff}, {remote_ip}'
        elif incoming_xff:
            headers['X-Forwarded-For'] = incoming_xff
        elif remote_ip:
            headers['X-Forwarded-For'] = remote_ip

        if request.scheme:
            headers['X-Forwarded-Proto'] = request.scheme

        if method == 'POST':
            headers['Content-Type'] = request.headers.get('Content-Type', 'application/json')

        return headers

    @web.middleware
    async def _cors_middleware(self, request, handler):
        if request.method == 'OPTIONS':
            return web.Response(
                status=204,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, Accept',
                }
            )
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    async def handle_root(self, request):
        """Root endpoint - forward to a worker"""
        return await self._proxy_to_worker(request, '/')

    async def handle_fetch(self, request):
        """Fetch endpoint - forward to a worker with retry"""
        return await self._proxy_with_retry(request, '/fetch')

    async def handle_download(self, request):
        """Download endpoint - forward to worker based on key"""
        key = request.query.get('key', '')
        worker_id = self._extract_worker_id(key)

        return await self._proxy_with_retry(
            request,
            '/download',
            preferred_worker_id=worker_id,
        )

    async def handle_info(self, request):
        """Info endpoint"""
        return await self._proxy_with_retry(request, '/info')

    async def handle_stream(self, request):
        """Stream endpoints with VPN rotation on rate limit"""
        path = request.path
        return await self._proxy_with_rotation(request, path)

    async def handle_tiktok(self, request):
        """TikTok endpoint with retry"""
        return await self._proxy_with_retry(request, '/tiktok', method='POST')

    async def handle_tiktok_download(self, request):
        """TikTok download endpoint"""
        key = request.query.get('key', '')
        worker_id = self._extract_worker_id(key)
        return await self._proxy_with_retry(
            request,
            '/tiktok/download',
            preferred_worker_id=worker_id,
        )

    async def handle_health(self, request):
        """Health check endpoint"""
        healthy = await self.registry.get_healthy_workers()
        return web.json_response({
            'status': 'healthy' if len(healthy) > 0 else 'degraded',
            'workers': [
                {
                    'id': w.id,
                    'healthy': w.healthy,
                    'active_requests': w.active_requests,
                    'failures': w.failures
                }
                for w in healthy
            ]
        })

    async def handle_tunnel(self, request):
        """Tunnel endpoint - proxy download stream"""
        key = request.query.get('key', '')
        worker_id = self._extract_worker_id(key)

        if not worker_id:
            return web.json_response(
                {'error': 'Invalid key'},
                status=400
            )

        worker = await self.registry.get_worker(worker_id)
        if not worker or not worker.healthy:
            return web.json_response(
                {'error': 'Worker not available'},
                status=503
            )

        return await self._stream_from_worker(request, worker, '/tunnel')

    def _extract_worker_id(self, key: str) -> Optional[str]:
        """Extract worker ID from key (format: w1-xxx or w2-xxx)"""
        if not key:
            return None
        parts = key.split('-')
        if parts and parts[0] in ['w1', 'w2', 'w3']:
            return parts[0]
        return None

    async def _schedule_worker_restart(self, worker_id: str, *, rate_limited: bool = False):
        """Schedule a background restart for a failed worker."""
        if rate_limited:
            await self.registry.mark_rate_limited(worker_id)
        else:
            await self.registry.mark_failure(worker_id)

        scheduled = await self.registry.schedule_restart(worker_id)
        if not scheduled:
            logger.info(f"[{worker_id}] Restart already scheduled/running; skip duplicate schedule")
            return

        started = await self._ensure_restart_task(worker_id)
        if not started:
            logger.info(f"[{worker_id}] Restart task already in-flight; skip duplicate task")

    def _queue_restart_candidate(self, queued: Dict[str, bool], worker_id: str, *, rate_limited: bool = False):
        """
        Queue restart candidates for this request and merge severity.
        True means rate-limited restart path (takes precedence).
        """
        prev = queued.get(worker_id, False)
        queued[worker_id] = bool(prev or rate_limited)

    async def _flush_queued_restarts(self, queued: Dict[str, bool]):
        """Run queued restarts after the request has a successful failover."""
        for worker_id, rate_limited in queued.items():
            await self._schedule_worker_restart(worker_id, rate_limited=rate_limited)

    async def _ensure_restart_task(self, worker_id: str) -> bool:
        """Ensure only one restart task runs per worker at a time."""
        async with self._restart_tasks_lock:
            existing = self._restart_tasks.get(worker_id)
            if existing and not existing.done():
                return False

            task = asyncio.create_task(self.rotator.restart_worker(worker_id))
            self._restart_tasks[worker_id] = task
            task.add_done_callback(
                lambda t, wid=worker_id: asyncio.create_task(self._finalize_restart_task(wid, t))
            )
            return True

    async def _finalize_restart_task(self, worker_id: str, task: asyncio.Task):
        """Cleanup restart task registry and surface unexpected task errors."""
        async with self._restart_tasks_lock:
            current = self._restart_tasks.get(worker_id)
            if current is task:
                self._restart_tasks.pop(worker_id, None)
        try:
            task.result()
        except Exception as e:
            logger.error(f"[{worker_id}] Restart task crashed: {e}")

    async def _proxy_streaming_response(
        self,
        request,
        worker: Worker,
        path: str,
        method: str = 'GET',
    ) -> Dict:
        """Proxy response while preserving streaming semantics and headers."""
        url = f"{worker.api_url}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        timeout = ClientTimeout(total=0 if method == 'GET' else 60)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = self._build_forward_headers(request, method)

                if method == 'POST':
                    body = await request.read()
                    async with session.post(url, headers=headers, data=body) as resp:
                        if resp.status == 200:
                            response = web.StreamResponse(status=resp.status)
                            if resp.content_type:
                                response.content_type = resp.content_type

                            for key, value in resp.headers.items():
                                if key.lower() != 'transfer-encoding':
                                    response.headers[key] = value

                            await response.prepare(request)
                            async for chunk in resp.content.iter_chunked(65536):
                                await response.write(chunk)
                            await response.write_eof()
                            return {'success': True, 'response': response}

                        return await self._handle_response(resp, worker)
                else:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            response = web.StreamResponse(status=resp.status)
                            if resp.content_type:
                                response.content_type = resp.content_type

                            for key, value in resp.headers.items():
                                if key.lower() != 'transfer-encoding':
                                    response.headers[key] = value

                            await response.prepare(request)
                            async for chunk in resp.content.iter_chunked(65536):
                                await response.write(chunk)
                            await response.write_eof()
                            return {'success': True, 'response': response}

                        return await self._handle_response(resp, worker)

        except asyncio.TimeoutError:
            logger.warning(f"[{worker.id}] Request timeout")
            return {'success': False}
        except Exception as e:
            logger.warning(f"[{worker.id}] Request error: {e}")
            return {'success': False}

    async def _proxy_with_retry(
        self,
        request,
        path: str,
        method: str = 'GET',
        preferred_worker_id: Optional[str] = None,
    ) -> web.Response:
        """Proxy request with retry and failover"""
        tried = set()
        queued_restarts: Dict[str, bool] = {}

        for attempt in range(MAX_RETRIES):
            worker = None

            if preferred_worker_id and preferred_worker_id not in tried:
                preferred = await self.registry.get_worker(preferred_worker_id)
                if preferred and preferred.healthy and not preferred.restarting and not preferred.restart_scheduled:
                    worker = preferred

            if not worker:
                workers = await self.registry.get_healthy_workers(tried)

                if not workers:
                    logger.error("No healthy workers available")
                    break

                # Random selection
                worker = random.choice(workers)
            tried.add(worker.id)

            result = await self._proxy_streaming_response(request, worker, path, method)

            if result['success']:
                if queued_restarts:
                    await self._flush_queued_restarts(queued_restarts)
                return result['response']

            should_restart = result.get('should_restart', True)
            if should_restart:
                if result.get('is_rate_limit'):
                    logger.warning(f"[{worker.id}] Rate limit detected, rotating VPN...")
                    self._queue_restart_candidate(queued_restarts, worker.id, rate_limited=True)
                else:
                    logger.warning(f"[{worker.id}] Retryable failure, scheduling restart")
                    self._queue_restart_candidate(queued_restarts, worker.id)
            else:
                logger.warning(f"[{worker.id}] Retryable client-side failure; failover without restart")

        logger.error(f"All {MAX_RETRIES} attempts failed")
        return web.json_response(
            {
                'error': 'Service Unavailable',
                'detail': 'All workers failed or busy. Please try again later.'
            },
            status=503
        )

    async def _proxy_with_rotation(self, request, path: str) -> web.Response:
        """Proxy request with immediate VPN rotation on rate limit"""
        tried = set()
        queued_restarts: Dict[str, bool] = {}

        for attempt in range(MAX_RETRIES):
            workers = await self.registry.get_healthy_workers(tried)

            if not workers:
                break

            worker = random.choice(workers)
            tried.add(worker.id)

            await self.registry.increment_active(worker.id)

            try:
                result = await self._proxy_streaming_response(request, worker, path)

                if result['success']:
                    if queued_restarts:
                        await self._flush_queued_restarts(queued_restarts)
                    return result['response']

                should_restart = result.get('should_restart', True)
                if should_restart:
                    if result.get('is_rate_limit'):
                        logger.warning(f"[{worker.id}] Rate limit on stream, rotating...")
                        self._queue_restart_candidate(queued_restarts, worker.id, rate_limited=True)
                    else:
                        logger.warning(f"[{worker.id}] Stream failure, scheduling restart")
                        self._queue_restart_candidate(queued_restarts, worker.id)
                else:
                    logger.warning(f"[{worker.id}] Retryable client-side stream failure; failover without restart")
                continue

            finally:
                await self.registry.decrement_active(worker.id)

        return web.json_response(
            {'error': 'Service Unavailable', 'detail': 'All workers rate limited or failed'},
            status=503
        )

    async def _try_proxy_request(self, request, worker: Worker, path: str, method: str = 'GET') -> Dict:
        """Try to proxy request to a worker"""
        url = f"{worker.api_url}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        timeout = ClientTimeout(total=60)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = self._build_forward_headers(request, method)

                if method == 'POST':
                    body = await request.read()
                    async with session.post(url, headers=headers, data=body) as resp:
                        return await self._handle_response(resp, worker)
                else:
                    async with session.get(url, headers=headers) as resp:
                        return await self._handle_response(resp, worker)

        except asyncio.TimeoutError:
            logger.warning(f"[{worker.id}] Request timeout")
            return {'success': False}
        except Exception as e:
            logger.warning(f"[{worker.id}] Request error: {e}")
            return {'success': False}

    async def _handle_response(self, resp: aiohttp.ClientResponse, worker: Worker) -> Dict:
        """Handle worker response"""
        status = resp.status

        # Read body
        try:
            body = await resp.read()
            text = body.decode('utf-8', errors='replace')
        except:
            text = ''

        # Check for rate limit patterns
        is_rate_limit = False

        if status == 400 or status == 429 or status == 403:
            lower_text = text.lower()
            rate_limit_patterns = [
                "rate-limited",
                "rate limited",
                "this content isn't available, try again later",
                "session has been rate-limited",
                "too many requests",
                "sign in to confirm",
                "not a bot",
            ]
            if any(p in lower_text for p in rate_limit_patterns):
                is_rate_limit = True
                logger.warning(f"[{worker.id}] Rate limit detected in response")

            # User-requested policy: always failover on 400/403/429
            # even when the message doesn't match known rate-limit patterns.
            logger.warning(
                f"[{worker.id}] Received {status}; treating as retryable failover per policy"
            )
            # Do not restart worker on generic 400 (can be request-specific).
            # Restart only for stronger rate-limit signals (403/429 or matched patterns).
            should_restart = status in (403, 429) or is_rate_limit
            return {
                'success': False,
                'is_rate_limit': bool(is_rate_limit or status in (403, 429)),
                'status': status,
                'should_restart': should_restart,
            }

        if status == 200:
            # Create response
            response = web.Response(
                body=body,
                status=status,
                content_type=resp.content_type
            )
            return {'success': True, 'response': response}

        # For client errors other than 400/403/429, return as-is (don't retry)
        if 400 <= status < 500 and not is_rate_limit:
            response = web.Response(
                body=body,
                status=status,
                content_type=resp.content_type or 'application/json'
            )
            return {'success': True, 'response': response}

        return {'success': False, 'is_rate_limit': is_rate_limit, 'status': status}

    async def _proxy_to_worker(self, request, path: str) -> web.Response:
        """Simple proxy to random worker"""
        workers = await self.registry.get_healthy_workers()
        if not workers:
            return web.json_response({'error': 'No workers available'}, status=503)

        worker = random.choice(workers)
        result = await self._proxy_streaming_response(request, worker, path)

        if result['success']:
            return result['response']
        return web.json_response({'error': 'Worker failed'}, status=502)

    async def _stream_from_worker(self, request, worker: Worker, path: str) -> web.Response:
        """Stream response from worker"""
        url = f"{worker.api_url}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"

        await self.registry.increment_active(worker.id)

        try:
            timeout = ClientTimeout(total=0)  # No timeout for streaming
            async with aiohttp.ClientSession(timeout=timeout) as session:
                headers = self._build_forward_headers(request, 'GET')
                async with session.get(url, headers=headers) as resp:
                    # Check for rate limit in stream
                    if resp.status == 200:
                        content_length = resp.headers.get('content-length')
                        estimated = resp.headers.get('estimated-content-length')

                        # Detect silent failure (rate limit during stream)
                        if estimated and (not content_length or content_length == '0'):
                            logger.warning(f"[{worker.id}] Possible rate limit in stream")
                            await self._schedule_worker_restart(worker.id, rate_limited=True)

                    # Stream response
                    response = web.StreamResponse(status=resp.status)
                    response.content_type = resp.content_type or 'application/octet-stream'

                    # Copy headers
                    for key, value in resp.headers.items():
                        if key.lower() not in ['transfer-encoding', 'content-length']:
                            response.headers[key] = value

                    await response.prepare(request)

                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)

                    await response.write_eof()
                    return response

        except Exception as e:
            logger.error(f"[{worker.id}] Stream error: {e}")
            return web.json_response({'error': 'Stream failed'}, status=502)
        finally:
            await self.registry.decrement_active(worker.id)

    async def _restart_workers(self, worker_ids: List[str]):
        """Restart multiple workers sequentially"""
        for wid in worker_ids:
            await self._schedule_worker_restart(wid)
            await asyncio.sleep(5)

    async def _restart_scheduler(self):
        """Background task to execute scheduled restarts"""
        while True:
            await asyncio.sleep(10)

            for worker in self.registry._workers:
                if worker.restart_scheduled and not worker.restarting:
                    await self._ensure_restart_task(worker.id)

    async def _uptime_checker(self):
        """Check for workers needing 24h restart"""
        while True:
            await asyncio.sleep(60)

            needing_restart = await self.registry.get_workers_needing_restart()

            for wid in needing_restart:
                # Wait for idle
                max_wait = 300  # 5 minutes
                waited = 0
                while not await self.registry.is_worker_idle(wid) and waited < max_wait:
                    await asyncio.sleep(5)
                    waited += 5

                if await self.registry.is_worker_idle(wid):
                    logger.info(f"[Uptime] Restarting {wid} after 24h")
                    await self.registry.schedule_restart(wid)

    async def start_background_tasks(self, app):
        """Start background tasks"""
        self._restart_task = asyncio.create_task(self._restart_scheduler())
        self._uptime_task = asyncio.create_task(self._uptime_checker())
        logger.info("Background tasks started")

    async def stop_background_tasks(self, app):
        """Stop background tasks"""
        if self._restart_task:
            self._restart_task.cancel()
        if self._uptime_task:
            self._uptime_task.cancel()
        logger.info("Background tasks stopped")

    def run(self):
        """Run the gateway server"""
        self.app.on_startup.append(self.start_background_tasks)
        self.app.on_cleanup.append(self.stop_background_tasks)

        logger.info(f"Starting gateway on port {GATEWAY_PORT}")
        web.run_app(self.app, host='0.0.0.0', port=GATEWAY_PORT)


if __name__ == '__main__':
    gateway = Gateway()
    gateway.run()
