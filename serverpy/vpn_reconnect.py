#!/usr/bin/env python3
"""
VPN Reconnect Manager for Multi-Instance TikTok Downloader
Handles VPN reconnection when IP is blocked by TikTok
"""

import asyncio
import httpx
import logging
import os
import time
from typing import Dict, Optional, List

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VPNManager:
    """Manages VPN connections for multiple instances"""
    
    # Instance configurations
    INSTANCES = {
        'instance-sg': {
            'control_port': 8001,
            'region': 'singapore',
            'name': 'Singapore'
        },
        'instance-jp': {
            'control_port': 8002,
            'region': 'japan',
            'name': 'Japan'
        },
        'instance-us': {
            'control_port': 8003,
            'region': 'usa',
            'name': 'USA'
        }
    }
    
    def __init__(self):
        self.username = os.getenv('GLUETUN_USERNAME', 'admin')
        self.password = os.getenv('GLUETUN_PASSWORD', 'secretpassword')
        self.last_reconnect: Dict[str, float] = {}
        self.reconnect_cooldown = 30  # Minimum seconds between reconnects
    
    def get_auth(self) -> tuple:
        """Get authentication credentials"""
        return (self.username, self.password)
    
    async def get_instance_status(self, instance_id: str) -> Optional[Dict]:
        """Get VPN status for an instance"""
        if instance_id not in self.INSTANCES:
            logger.error(f"Unknown instance: {instance_id}")
            return None
        
        config = self.INSTANCES[instance_id]
        control_port = config['control_port']
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Get VPN status
                response = await client.get(
                    f'http://localhost:{control_port}/v1/vpn/status',
                    auth=self.get_auth()
                )
                
                if response.status_code == 200:
                    status_data = response.json()
                    
                    # Get public IP
                    ip_response = await client.get(
                        f'http://localhost:{control_port}/v1/publicip/ip',
                        auth=self.get_auth()
                    )
                    
                    if ip_response.status_code == 200:
                        ip_data = ip_response.json()
                        status_data['public_ip'] = ip_data.get('public_ip', 'unknown')
                    
                    logger.info(f"{config['name']} status: {status_data}")
                    return status_data
                else:
                    logger.error(f"Failed to get status for {instance_id}: {response.status_code}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error getting status for {instance_id}: {e}")
            return None
    
    async def reconnect_vpn(self, instance_id: str) -> bool:
        """Reconnect VPN for a specific instance"""
        if instance_id not in self.INSTANCES:
            logger.error(f"Unknown instance: {instance_id}")
            return False
        
        # Check cooldown
        now = time.time()
        last_reconnect = self.last_reconnect.get(instance_id, 0)
        if now - last_reconnect < self.reconnect_cooldown:
            logger.warning(f"Reconnect cooldown active for {instance_id}, skipping")
            return False
        
        config = self.INSTANCES[instance_id]
        control_port = config['control_port']
        
        try:
            logger.info(f"Triggering VPN reconnect for {config['name']} ({instance_id})")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Stop VPN
                logger.info(f"Stopping VPN for {config['name']}...")
                stop_response = await client.put(
                    f'http://localhost:{control_port}/v1/vpn/status',
                    auth=self.get_auth(),
                    json={'status': 'stopped'}
                )
                
                if stop_response.status_code != 200:
                    logger.error(f"❌ Failed to stop VPN for {config['name']}: {stop_response.status_code}")
                    return False
                
                # Wait for VPN to stop
                await asyncio.sleep(2)
                
                # Step 2: Start VPN (this will get a new IP)
                logger.info(f"Starting VPN for {config['name']}...")
                start_response = await client.put(
                    f'http://localhost:{control_port}/v1/vpn/status',
                    auth=self.get_auth(),
                    json={'status': 'running'}
                )
                
                if start_response.status_code == 200:
                    logger.info(f"✅ VPN reconnect triggered for {config['name']}")
                    self.last_reconnect[instance_id] = now
                    
                    # Wait for VPN to establish connection
                    await asyncio.sleep(5)
                    
                    # Verify new IP
                    new_status = await self.get_instance_status(instance_id)
                    if new_status:
                        logger.info(f"🔄 {config['name']} new IP: {new_status.get('public_ip', 'unknown')}")
                    
                    return True
                else:
                    logger.error(f"❌ Failed to start VPN for {config['name']}: {start_response.status_code}")
                    logger.error(f"Response: {start_response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error reconnecting VPN for {instance_id}: {e}")
            return False
    
    async def rotate_server(self, instance_id: str, new_country: Optional[str] = None) -> bool:
        """Rotate to a different VPN server"""
        if instance_id not in self.INSTANCES:
            logger.error(f"Unknown instance: {instance_id}")
            return False
        
        config = self.INSTANCES[instance_id]
        control_port = config['control_port']
        
        # Default rotation: Singapore -> Japan -> USA -> Singapore
        rotations = {
            'singapore': 'Japan',
            'japan': 'USA',
            'usa': 'Singapore'
        }
        
        target_country = new_country or rotations.get(config['region'], 'Singapore')
        
        try:
            logger.info(f"🌏 Rotating {config['name']} to {target_country}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Update server settings
                response = await client.put(
                    f'http://localhost:{control_port}/v1/settings',
                    auth=self.get_auth(),
                    json={
                        'vpn': {
                            'provider': {
                                'name': 'mullvad',
                                'server_selection': {
                                    'countries': [target_country]
                                }
                            }
                        }
                    }
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ Server rotation initiated for {config['name']}")
                    
                    # Trigger reconnect to apply new settings
                    await self.reconnect_vpn(instance_id)
                    return True
                else:
                    logger.error(f"❌ Failed to rotate server: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error rotating server: {e}")
            return False
    
    async def get_all_status(self) -> Dict[str, Optional[Dict]]:
        """Get status for all instances"""
        tasks = [
            self.get_instance_status(instance_id)
            for instance_id in self.INSTANCES.keys()
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        status = {}
        for instance_id, result in zip(self.INSTANCES.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Error getting status for {instance_id}: {result}")
                status[instance_id] = None
            else:
                status[instance_id] = result
        
        return status
    
    async def handle_403_error(self, instance_id: str) -> bool:
        """Handle 403 error by reconnecting VPN"""
        logger.warning(f"🚨 Handling 403 error for {instance_id}")
        
        # First, try simple reconnect
        if await self.reconnect_vpn(instance_id):
            return True
        
        # If that fails, try rotating server
        logger.info(f"🔄 Simple reconnect failed, trying server rotation...")
        return await self.rotate_server(instance_id)
    
    def print_status_table(self, status: Dict[str, Optional[Dict]]):
        """Print status in table format"""
        print("\n" + "="*70)
        print(f"{'Instance':<15} {'Region':<10} {'Status':<10} {'IP Address':<15} {'Connected'}")
        print("="*70)
        
        for instance_id, data in status.items():
            config = self.INSTANCES[instance_id]
            if data:
                vpn_status = data.get('status', 'unknown')
                public_ip = data.get('public_ip', 'unknown')
                connected = '✅' if vpn_status == 'running' else '❌'
                print(f"{instance_id:<15} {config['region']:<10} {vpn_status:<10} {public_ip:<15} {connected}")
            else:
                print(f"{instance_id:<15} {config['region']:<10} {'error':<10} {'unknown':<15} ❌")
        
        print("="*70 + "\n")


async def main():
    """CLI for VPN management"""
    import sys
    
    manager = VPNManager()
    
    if len(sys.argv) < 2:
        print("Usage: python vpn_reconnect.py <command> [instance_id]")
        print("\nCommands:")
        print("  status              - Show status of all instances")
        print("  reconnect <id>      - Reconnect VPN for instance")
        print("  rotate <id> [country] - Rotate server for instance")
        print("  handle-403 <id>     - Handle 403 error for instance")
        print("\nInstance IDs: instance-sg, instance-jp, instance-us")
        return
    
    command = sys.argv[1]
    
    if command == 'status':
        status = await manager.get_all_status()
        manager.print_status_table(status)
    
    elif command == 'reconnect' and len(sys.argv) >= 3:
        instance_id = sys.argv[2]
        success = await manager.reconnect_vpn(instance_id)
        print(f"Reconnect {'successful' if success else 'failed'}")
    
    elif command == 'rotate' and len(sys.argv) >= 3:
        instance_id = sys.argv[2]
        new_country = sys.argv[3] if len(sys.argv) >= 4 else None
        success = await manager.rotate_server(instance_id, new_country)
        print(f"Rotation {'successful' if success else 'failed'}")
    
    elif command == 'handle-403' and len(sys.argv) >= 3:
        instance_id = sys.argv[2]
        success = await manager.handle_403_error(instance_id)
        print(f"403 handling {'successful' if success else 'failed'}")
    
    else:
        print("Invalid command or missing arguments")


if __name__ == '__main__':
    asyncio.run(main())
