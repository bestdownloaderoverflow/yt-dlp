#!/usr/bin/env python3
"""
VPN Manager for yt-dlp-stream + Gluetun
Handles VPN reconnection when IP is blocked by TikTok/YouTube/Twitter
"""

import asyncio
import httpx
import logging
import os
import time
from typing import Dict, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VPNManager:
    """Manages Gluetun VPN connection for yt-dlp-stream"""

    def __init__(self):
        self.host = os.getenv('GLUETUN_HOST', 'localhost')
        self.port = int(os.getenv('GLUETUN_PORT', '8000'))
        self.username = os.getenv('GLUETUN_USERNAME', 'admin')
        self.password = os.getenv('GLUETUN_PASSWORD', 'secretpassword')
        self.last_reconnect = 0
        self.reconnect_cooldown = 30  # Minimum seconds between reconnects
        self.base_url = f"http://{self.host}:{self.port}"

    def get_auth(self) -> tuple:
        """Get authentication credentials"""
        return (self.username, self.password)

    async def get_status(self) -> Optional[Dict]:
        """Get current VPN status"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Get VPN status
                response = await client.get(
                    f'{self.base_url}/v1/vpn/status',
                    auth=self.get_auth()
                )

                if response.status_code == 200:
                    status_data = response.json()

                    # Get public IP
                    ip_response = await client.get(
                        f'{self.base_url}/v1/publicip/ip',
                        auth=self.get_auth()
                    )

                    if ip_response.status_code == 200:
                        ip_data = ip_response.json()
                        status_data['public_ip'] = ip_data.get('public_ip', 'unknown')

                    logger.info(f"VPN Status: {status_data}")
                    return status_data
                else:
                    logger.error(f"Failed to get VPN status: {response.status_code}")
                    return None

        except Exception as e:
            logger.error(f"Error getting VPN status: {e}")
            return None

    async def reconnect(self) -> bool:
        """Reconnect VPN to get new IP"""
        # Check cooldown
        now = time.time()
        if now - self.last_reconnect < self.reconnect_cooldown:
            logger.warning(f"Reconnect cooldown active, waiting...")
            await asyncio.sleep(self.reconnect_cooldown - (now - self.last_reconnect))

        try:
            logger.info("🔄 Triggering VPN reconnect...")

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Stop VPN
                logger.info("Stopping VPN...")
                stop_response = await client.put(
                    f'{self.base_url}/v1/vpn/status',
                    auth=self.get_auth(),
                    json={'status': 'stopped'}
                )

                if stop_response.status_code != 200:
                    logger.error(f"❌ Failed to stop VPN: {stop_response.status_code}")
                    return False

                # Wait for VPN to stop
                await asyncio.sleep(2)

                # Step 2: Start VPN (this will get a new IP)
                logger.info("Starting VPN with new IP...")
                start_response = await client.put(
                    f'{self.base_url}/v1/vpn/status',
                    auth=self.get_auth(),
                    json={'status': 'running'}
                )

                if start_response.status_code == 200:
                    logger.info("✅ VPN reconnect triggered")
                    self.last_reconnect = time.time()

                    # Wait for VPN to establish
                    await asyncio.sleep(5)

                    # Verify new IP
                    new_status = await self.get_status()
                    if new_status:
                        logger.info(f"🔄 New VPN IP: {new_status.get('public_ip', 'unknown')}")

                    return True
                else:
                    logger.error(f"❌ Failed to start VPN: {start_response.status_code}")
                    return False

        except Exception as e:
            logger.error(f"❌ Error reconnecting VPN: {e}")
            return False

    async def handle_403_error(self) -> bool:
        """Handle 403 error by reconnecting VPN"""
        logger.warning("🚨 Handling 403 error - rotating VPN IP...")
        return await self.reconnect()


# Singleton instance
_vpn_manager = None
_vpn_manager_lock = asyncio.Lock()


async def get_vpn_manager() -> VPNManager:
    """Get or create VPN manager singleton (async-safe)."""
    global _vpn_manager
    if _vpn_manager is None:
        async with _vpn_manager_lock:
            if _vpn_manager is None:
                _vpn_manager = VPNManager()
    return _vpn_manager


def get_vpn_manager_sync() -> VPNManager:
    """Get or create VPN manager singleton (sync, non-concurrent contexts only)."""
    global _vpn_manager
    if _vpn_manager is None:
        _vpn_manager = VPNManager()
    return _vpn_manager


async def main():
    """CLI for VPN management"""
    import sys

    manager = VPNManager()

    if len(sys.argv) < 2:
        print("Usage: python vpn_manager.py <command>")
        print("\nCommands:")
        print("  status      - Show VPN status")
        print("  reconnect   - Reconnect VPN (get new IP)")
        print("  handle-403  - Handle 403 error (reconnect)")
        return

    command = sys.argv[1]

    if command == 'status':
        status = await manager.get_status()
        if status:
            print(f"\nVPN Status: {status.get('status', 'unknown')}")
            print(f"Public IP: {status.get('public_ip', 'unknown')}")
            print(f"Connected: {'✅' if status.get('status') == 'running' else '❌'}")
        else:
            print("❌ Failed to get status")

    elif command == 'reconnect':
        success = await manager.reconnect()
        print(f"Reconnect {'successful ✅' if success else 'failed ❌'}")

    elif command == 'handle-403':
        success = await manager.handle_403_error()
        print(f"403 handling {'successful ✅' if success else 'failed ❌'}")

    else:
        print("Invalid command")


if __name__ == '__main__':
    asyncio.run(main())
