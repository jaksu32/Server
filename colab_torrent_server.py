# colab_torrent_server.py
import libtorrent as lt
import time
import datetime
import os
import shutil
from pathlib import Path
import sys
import threading
import json
from typing import Optional, Dict, List
import websockets
import asyncio
import base64
import socket
from urllib.parse import urlparse
import requests
import nest_asyncio

class TorrentClientColab:
    def __init__(self):
        self.ses = lt.session()
        self.ses.listen_on(6881, 6891)
        self.handle: Optional[lt.torrent_handle] = None
        self.is_downloading = False
        self.save_path = "/content/downloads"  # Temporary Colab storage
        self.download_thread = None
        self.websocket = None
        self.local_connection = None
        self.chunk_size = 8192  # 8KB chunks for file transfer
        
    def format_size(self, size_bytes: int) -> str:
        """Convert bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
    
    def format_time(self, seconds: int) -> str:
        """Convert seconds to human readable format"""
        if seconds < 0:
            return "calculating..."
        elif seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            seconds = seconds % 60
            return f"{minutes}m {seconds}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            seconds = seconds % 60
            return f"{hours}h {minutes}m {seconds}s"

    async def setup_local_connection(self, local_port: int):
        """Setup WebSocket connection to local machine"""
        try:
            # Get Colab's external URL using localtunnel
            response = requests.get('https://api.localtunnel.me/create-tunnel', 
                                 params={'port': local_port})
            tunnel_url = response.json()['url']
            print(f"Local tunnel URL: {tunnel_url}")
            
            # Create WebSocket server
            async with websockets.serve(self.handle_connection, "0.0.0.0", local_port):
                print(f"WebSocket server running on port {local_port}")
                await asyncio.Future()  # run forever
        except Exception as e:
            print(f"Error setting up local connection: {str(e)}")

    async def handle_connection(self, websocket, path):
        """Handle incoming WebSocket connection"""
        self.websocket = websocket
        try:
            async for message in websocket:
                data = json.loads(message)
                if data['type'] == 'start_download':
                    await self.handle_download_request(data)
                elif data['type'] == 'stop_download':
                    self.stop_download()
        except Exception as e:
            print(f"WebSocket error: {str(e)}")
        finally:
            self.websocket = None

    async def send_progress_update(self, progress_data: Dict):
        """Send progress update through WebSocket"""
        if self.websocket:
            try:
                await self.websocket.send(json.dumps({
                    'type': 'progress_update',
                    'data': progress_data
                }))
            except Exception as e:
                print(f"Error sending progress update: {str(e)}")

    async def send_file_chunk(self, filepath: str, dest_path: str):
        """Send file in chunks to local machine"""
        if not self.websocket:
            return
        
        try:
            file_size = os.path.getsize(filepath)
            chunks_sent = 0
            
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break
                    
                    chunks_sent += len(chunk)
                    progress = (chunks_sent / file_size) * 100
                    
                    await self.websocket.send(json.dumps({
                        'type': 'file_chunk',
                        'data': {
                            'chunk': base64.b64encode(chunk).decode('utf-8'),
                            'progress': progress,
                            'dest_path': dest_path,
                            'total_size': file_size
                        }
                    }))
                    
                    # Small delay to prevent overwhelming the connection
                    await asyncio.sleep(0.01)
                    
            await self.websocket.send(json.dumps({
                'type': 'file_complete',
                'data': {'dest_path': dest_path}
            }))
            
        except Exception as e:
            print(f"Error sending file: {str(e)}")
            await self.websocket.send(json.dumps({
                'type': 'error',
                'data': {'message': str(e)}
            }))

    def calculate_eta(self, status) -> int:
        """Calculate estimated time remaining in seconds"""
        if status.download_rate <= 0:
            return -1
        
        total_wanted = status.total_wanted
        total_done = status.total_wanted_done
        remaining_bytes = total_wanted - total_done
        
        if remaining_bytes <= 0:
            return 0
            
        return int(remaining_bytes / status.download_rate)

    async def download_loop(self):
        """Main download loop with progress display and WebSocket updates"""
        begin = time.time()
        last_progress = 0
        
        while self.is_downloading:
            if self.handle.status().state == lt.torrent_status.seeding:
                print("\nDownload Complete!")
                break
            
            s = self.handle.status()
            
            if abs(s.progress * 100 - last_progress) >= 0.1:
                last_progress = s.progress * 100
                state_str = ['queued', 'checking', 'downloading metadata',
                            'downloading', 'finished', 'seeding', 'allocating']
                
                eta = self.calculate_eta(s)
                progress_data = {
                    'progress': s.progress * 100,
                    'download_rate': s.download_rate / 1000,
                    'upload_rate': s.upload_rate / 1000,
                    'num_seeds': s.num_seeds,
                    'state': state_str[s.state],
                    'eta': self.format_time(eta),
                    'total_size': s.total_wanted,
                    'downloaded': s.total_wanted_done
                }
                
                # Send progress update through WebSocket
                await self.send_progress_update(progress_data)
                
                # Also display in Colab
                sys.stdout.write('\033[K')
                print(f'\rProgress: {s.progress * 100:.2f}% '
                      f'↓ {s.download_rate / 1000:.1f} kB/s '
                      f'↑ {s.upload_rate / 1000:.1f} kB/s '
                      f'Seeds: {s.num_seeds} '
                      f'ETA: {self.format_time(eta)} '
                      f'State: {state_str[s.state]}', end='')
                sys.stdout.flush()
            
            await asyncio.sleep(0.5)
        
        # If download completed successfully, transfer to local machine
        if self.is_downloading and self.websocket:
            download_path = os.path.join(self.save_path, self.handle.name())
            if os.path.exists(download_path):
                if os.path.isfile(download_path):
                    await self.send_file_chunk(download_path, self.handle.name())
                else:
                    # For directories, create zip first
                    zip_path = f"{download_path}.zip"
                    shutil.make_archive(download_path, 'zip', download_path)
                    await self.send_file_chunk(zip_path, f"{self.handle.name()}.zip")
                    os.remove(zip_path)  # Clean up zip file
        
        end = time.time()
        elapsed = end - begin
        print(f"\nCompleted in {int(elapsed // 60)}m {int(elapsed % 60)}s")

    async def handle_download_request(self, data: Dict):
        """Handle download request from local machine"""
        try:
            torrent_path_or_magnet = data['torrent_source']
            if torrent_path_or_magnet.startswith('magnet:'):
                self.handle = lt.add_magnet_uri(self.ses, torrent_path_or_magnet,
                                              {'save_path': self.save_path})
                print("Downloading metadata...")
                while not self.handle.has_metadata():
                    await asyncio.sleep(1)
                print("Metadata downloaded!")
            else:
                info = lt.torrent_info(torrent_path_or_magnet)
                self.handle = self.ses.add_torrent({
                    'ti': info,
                    'save_path': self.save_path
                })
            
            self.is_downloading = True
            await self.download_loop()
            
        except Exception as e:
            print(f"Error handling download request: {str(e)}")
            if self.websocket:
                await self.websocket.send(json.dumps({
                    'type': 'error',
                    'data': {'message': str(e)}
                }))

    def stop_download(self):
        """Stop the download"""
        if self.handle:
            self.is_downloading = False
            self.ses.remove_torrent(self.handle)
            self.handle = None
            print("\nDownload stopped")

# Local client code (save as local_client.py on your PC)
class LocalTorrentClient:
    def __init__(self, colab_url: str, download_dir: str):
        self.colab_url = colab_url
        self.download_dir = download_dir
        self.websocket = None
        self.current_file = None
        self.current_file_handle = None
        
    async def connect(self):
        """Connect to Colab server"""
        try:
            self.websocket = await websockets.connect(self.colab_url)
            print("Connected to Colab server")
            await self.handle_messages()
        except Exception as e:
            print(f"Connection error: {str(e)}")
    
    async def start_download(self, torrent_source: str):
        """Start download on Colab"""
        if self.websocket:
            await self.websocket.send(json.dumps({
                'type': 'start_download',
                'torrent_source': torrent_source
            }))
    
    async def handle_messages(self):
        """Handle incoming messages from Colab"""
        try:
            async for message in self.websocket:
                data = json.loads(message)
                
                if data['type'] == 'progress_update':
                    self.display_progress(data['data'])
                
                elif data['type'] == 'file_chunk':
                    await self.handle_file_chunk(data['data'])
                
                elif data['type'] == 'file_complete':
                    await self.complete_file(data['data'])
                
                elif data['type'] == 'error':
                    print(f"Error from server: {data['data']['message']}")
        
        except Exception as e:
            print(f"Error handling messages: {str(e)}")
    
    def display_progress(self, progress_data: Dict):
        """Display download progress"""
        sys.stdout.write('\033[K')
        print(f"\rProgress: {progress_data['progress']:.2f}% "
              f"↓ {progress_data['download_rate']:.1f} kB/s "
              f"↑ {progress_data['upload_rate']:.1f} kB/s "
              f"Seeds: {progress_data['num_seeds']} "
              f"ETA: {progress_data['eta']} "
              f"State: {progress_data['state']}", end='')
        sys.stdout.flush()
    
    async def handle_file_chunk(self, chunk_data: Dict):
        """Handle incoming file chunk"""
        try:
            dest_path = os.path.join(self.download_dir, chunk_data['dest_path'])
            
            if not self.current_file_handle:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                self.current_file_handle = open(dest_path, 'wb')
                self.current_file = dest_path
            
            chunk = base64.b64decode(chunk_data['chunk'])
            self.current_file_handle.write(chunk)
            
            # Display transfer progress
            progress = chunk_data['progress']
            sys.stdout.write('\033[K')
            print(f"\rTransferring to local machine: {progress:.1f}%", end='')
            sys.stdout.flush()
            
        except Exception as e:
            print(f"\nError handling file chunk: {str(e)}")
    
    async def complete_file(self, data: Dict):
        """Handle file transfer completion"""
        if self.current_file_handle:
            self.current_file_handle.close()
            self.current_file_handle = None
            print(f"\nFile saved to: {self.current_file}")
            self.current_file = None
