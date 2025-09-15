#!/usr/bin/env python3
"""
ScummVM Game Downloader and Uploader
Rewritten from JavaScript using only standard library
"""

import os
import json
import sys
import urllib.request
import urllib.parse
import zipfile
import shutil
import subprocess
import argparse
import json
from pathlib import Path
import tempfile

# Constants
SHEET_URL = 'https://docs.google.com/spreadsheets/d/e/2PACX-1vQamumX0p-DYQa5Umi3RxX-pHM6RZhAj1qvUP0jTmaqutN9FwzyriRSXlO9rq6kR60pGIuPvCDzZL3s/pub?output=tsv'
SHEET_IDS = {
    'platforms': '1061029686',
    'compatibility': '1989596967', 
    'games': '1775285192',
    'engines': '0',
    'companies': '226191984',
    'versions': '1225902887',
    'game_demos': '1303420306',
    'series': '1095671818',
    'screenshots': '168506355',
    'scummvm_downloads': '1057392663',
    'game_downloads': '810295288',
    'director_demos': '1256563740',
}

class GameDownloader:
    def __init__(self, download_dir="games", scp_server=None, scp_path=None, scp_port=None):
        self.games = {}
        self.all_download_urls = []  # List to store all URLs we want to download
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)
        self.scp_server = scp_server
        self.scp_path = scp_path
        self.scp_port = scp_port
        self.compatible_game_ids = set()  # Cache for compatible game IDs
        self.games_metadata = []  # List to store game metadata for JSON output
        self.platforms_data = {}  # Cache for platform data
        self.games_data = {}  # Cache for games data
        self.processed_games_metadata = []  # List to store metadata for actually processed games
        self.metadata = {}

    def _load_and_apply_metadata(self):
        """Load metadata from ../assets/metadata.json and apply overrides to existing games"""
        # Load metadata file
        metadata_path = Path(__file__).parent.parent / "assets" / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
        
        # Apply metadata overrides to existing games and add metadata-only games
        for relative_path, metadata_entry in self.metadata.items():
            # Skip if skip: true is set
            if metadata_entry.get("skip", False):
                continue
            
            # For metadata.json entries, we need at least an 'id' to create new entries
            if 'id' not in metadata_entry:
                continue
                
            # Use the unified method to add or update metadata
            self.add_or_update_game_metadata(
                game_id=metadata_entry.get('id'),
                relative_path=relative_path,
                description=metadata_entry.get('description'),
                platform=metadata_entry.get('platform'),
                metadata_overrides=metadata_entry
            )
        
    def add_or_update_game_metadata(self, game_id=None, relative_path=None, description=None, 
                                   download_url=None, languages=None, platform=None, 
                                   metadata_overrides=None):
        """
        Unified method to add new game metadata or update existing metadata.
        
        Args:
            game_id: Game identifier
            relative_path: Path where game will be stored
            description: Game description
            download_url: URL to download the game
            languages: List of supported languages
            platform: Platform name
            metadata_overrides: Dict of metadata properties to override/add
        """
        # Find existing metadata by relative_path if it exists
        existing_metadata = None
        existing_index = None
        
        for i, metadata in enumerate(self.games_metadata):
            if metadata.get('relative_path') == relative_path:
                existing_metadata = metadata
                existing_index = i
                break
        
        if existing_metadata:
            # Update existing metadata
            game_metadata = existing_metadata.copy()
        else:
            # Create new metadata with base properties
            game_metadata = {
                'id': game_id,
                'relative_path': relative_path,
                'description': description,
            }
            
            # Add optional properties if provided
            if download_url:
                game_metadata['download_url'] = download_url
            if languages:
                # Remove 'en' (it's often wrong - should be en_US or en_GB to launch game)     
                if 'en' in languages:
                    languages.remove('en')
                if languages:
                    game_metadata['languages'] = languages

            if platform:
                game_metadata['platform'] = platform
        # Apply metadata overrides if provided
        if metadata_overrides:
            for key, value in metadata_overrides.items():
                game_metadata[key] = value
    
        # Update existing or add new metadata
        if existing_metadata:
            self.games_metadata[existing_index] = game_metadata
        else:
            self.games_metadata.append(game_metadata)
        
        return game_metadata

    
    def _temp_print(self, message, file=sys.stderr):
        """Print a temporary message that will be overwritten, padded to terminal width"""
        try:
            # Get terminal width, default to 80 if not available
            terminal_width = os.get_terminal_size().columns
        except OSError:
            terminal_width = 80
        
        # Pad the message with spaces to clear any previous text
        padded_message = message.ljust(terminal_width)
        print(padded_message, end='\r', flush=True, file=file)
    
    def _print(self, message, file=sys.stderr):
        """Print a permanent message, padded to terminal width"""
        try:
            # Get terminal width, default to 80 if not available
            terminal_width = os.get_terminal_size().columns
        except OSError:
            terminal_width = 80
        
        # Pad the message with spaces to clear any previous text
        padded_message = message.ljust(terminal_width)
        print(padded_message, file=file)
    
    def _build_ssh_command(self, base_command='ssh'):
        """Build SSH/SCP command with authentication options and persistent connection"""
        cmd = []
        env = os.environ.copy()
        control_path = "/tmp/scummvm-ssh-%r@%h:%p"

        # Check for password authentication first
        ssh_password = os.environ.get('SSH_PASSWORD')
        if ssh_password:
            cmd.extend(['sshpass', '-e', base_command])
            env['SSHPASS'] = ssh_password
        else:
            cmd.append(base_command)

        # Add SSH key if specified in environment (and no password)
        ssh_key = os.environ.get('SSH_KEY_PATH')
        if ssh_key and not ssh_password:
            cmd.extend(['-i', ssh_key])

        # Add port if specified
        if self.scp_port:
            if base_command == 'scp':
                cmd.extend(['-P', str(self.scp_port)])
            else:  # ssh
                cmd.extend(['-p', str(self.scp_port)])

        # Add persistent connection options
        cmd.extend([
            '-o', 'ControlMaster=auto',
            '-o', f'ControlPath={control_path}',
            '-o', 'ControlPersist=600'
        ])

        # Add other SSH options for non-interactive mode
        if ssh_password:
            cmd.extend(['-o', 'StrictHostKeyChecking=no'])
        else:
            cmd.extend([
                '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=no'
            ])

        return cmd, env
    
    def _build_controlpath_ssh_command(self, base_command='ssh'):
        """Build SSH/SCP command that reuses the persistent connection (ControlPath only)."""
        cmd = [base_command]
        control_path = "/tmp/scummvm-ssh-%r@%h:%p"
        
        # Add port if specified
        if self.scp_port:
            if base_command == 'scp':
                cmd.extend(['-P', str(self.scp_port)])
            else:
                cmd.extend(['-p', str(self.scp_port)])
        
        # Only add ControlPath for reusing the connection
        cmd.extend(['-o', f'ControlPath={control_path}'])
        
        return cmd
    
    def _open_ssh_connection(self):
        """Open a persistent SSH connection. Exit if connection fails."""
        ssh_cmd, env = self._build_ssh_command()
        # Insert -MNf right after the ssh command (before any options)
        # Find the position of the actual ssh command (could be 'ssh' or after 'sshpass -e')
        ssh_pos = 0
        for i, arg in enumerate(ssh_cmd):
            if arg in ['ssh', 'scp']:
                ssh_pos = i
                break
        
        # Insert -MNf right after the ssh command
        ssh_cmd.insert(ssh_pos + 1, '-MNf')
        ssh_cmd.append(f"{self.scp_server}")
        subprocess.run(ssh_cmd, check=True, env=env)
        self._temp_print("Opened SSH connection")
       

    def _close_ssh_connection(self):
        """Close the persistent SSH connection"""
        control_path = "/tmp/scummvm-ssh-%r@%h:%p"
        close_cmd = [
            "ssh", "-O", "exit",
            "-o", f"ControlPath={control_path}",
            f"{self.scp_server}"
        ]
        try:
            result = subprocess.run(close_cmd, check=False, capture_output=True)
            if result.returncode == 0:
                self._temp_print("Closed SSH connection")
            elif result.returncode == 255:
                # Connection already closed or control socket doesn't exist - this is normal
                self._temp_print("SSH connection already closed")
            else:
                # Only show warning for unexpected errors
                stderr_output = result.stderr.decode('utf-8', errors='ignore').strip()
                self._print(f"Warning: Could not close SSH connection (exit code {result.returncode}): {stderr_output}")
        except Exception as e:
            self._print(f"Warning: Could not close SSH connection: {e}")
        
    def get_google_sheet(self, url):
        """Fetch Google Sheet with redirect handling"""
        try:
            # Handle redirects manually
            req = urllib.request.Request(url)
            response = urllib.request.urlopen(req)
            
            # Check if we got a redirect
            if response.getcode() in [301, 302, 303, 307, 308]:
                redirect_url = response.headers.get('Location')
                if redirect_url:
                    response = urllib.request.urlopen(redirect_url)
            
            return response.read().decode('utf-8')
        except Exception as e:
            print(f"Error fetching {url}: {e}", file=sys.stderr)
            raise
    
    def parse_tsv(self, text):
        """Parse TSV data into list of dictionaries"""
        lines = text.split('\r\n')
        if not lines:
            return []
        
        headers = lines[0].split('\t')
        result = []
        
        for i in range(1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            values = line.split('\t')
            row = {}
            for col, value in enumerate(values):
                if col < len(headers):
                    row[headers[col]] = value
            result.append(row)
        
        return result
    
    def get_compatible_games(self):
        """Fetch compatible game IDs from Google Sheets"""
        self._temp_print("Fetching list of compatible games")
        url = f"{SHEET_URL}&gid={SHEET_IDS['compatibility']}"
        body = self.get_google_sheet(url)
        
        games_data = self.parse_tsv(body)
        for game in games_data:
            # Try multiple possible column names for game ID
            game_id = game.get('id', '') or game.get('game_id', '') or game.get('gameid', '')
            if game_id:
                self.compatible_game_ids.add(game_id)
        
        self._print(f"Found {len(self.compatible_game_ids)} compatible games")
    
    def get_platforms_data(self):
        """Fetch platform data from Google Sheets"""
        self._temp_print("Fetching platform data")
        url = f"{SHEET_URL}&gid={SHEET_IDS['platforms']}"
        body = self.get_google_sheet(url)
        
        platforms_list = self.parse_tsv(body)
        for platform in platforms_list:
            platform_id = platform.get('id', '')
            if platform_id:
                self.platforms_data[platform_id] = platform
        
        self._temp_print(f"Loaded {len(self.platforms_data)} platforms")
    
    def get_games_data(self):
        """Fetch games data from Google Sheets"""
        self._temp_print("Fetching games data")
        url = f"{SHEET_URL}&gid={SHEET_IDS['games']}"
        body = self.get_google_sheet(url)
        
        games_list = self.parse_tsv(body)
        for game in games_list:
            game_id = game.get('id', '')
            if game_id:
                self.games_data[game_id] = game
        
        self._temp_print(f"Loaded {len(self.games_data)} games")
    
    def _extract_languages(self, data):
        """Extract languages from various language columns"""
        languages = []
        
        # Check different possible language column names
        lang_columns = ['lang', 'language', 'language1', 'language2', 'language3']
        
        for col in lang_columns:
            lang_value = data.get(col, '').strip()
            if lang_value and lang_value not in languages:
                languages.append(lang_value)
        return languages  # Do not fall back to English; return empty if none found
    
    def _get_relative_path(self, url, filename):
        """Get the relative path for a game file"""
        # Extract just the filename from the URL if needed
        if not filename:
            filename = url[url.rfind('/') + 1:]
        elif filename.startswith('/'):
            filename = filename[filename.rfind('/') + 1:]
            
        if filename.endswith('.zip'):
            return filename[:-4]  # Remove .zip extension for folder name
        else:
            return filename
    
    def get_game_downloads(self):
        """Fetch game downloads from Google Sheets"""
        self._temp_print("Fetching list of game downloads")
        url = f"{SHEET_URL}&gid={SHEET_IDS['game_downloads']}"
        body = self.get_google_sheet(url)
        
        unique_urls = set()
        skipped_games_count = 0
        skipped_addons_count = 0
        for download in self.parse_tsv(body):
            game_id = download.get('game_id', '')
            game_name = download.get('name', '')
            category = download.get('category', '')
            
            # Skip entries with "addon" in the name (case-insensitive)
            if 'addon' in game_name.lower() or 'manuals' in game_name.lower() or category != 'games':
                skipped_addons_count += 1
                continue
                        
            # Allow specifying game names without target/engine name
            short_name = game_id[game_id.rfind(':') + 1:] if ':' in game_id else game_id
            
            # Skip games not found in compatibility sheet
            if game_id not in self.compatible_game_ids:
                skipped_games_count += 1
                continue
            
            # Track unique URLs for compatible games
            download_url = f"/frs/extras/{download['url']}"
            unique_urls.add(download_url)
            
            # Add to list of all URLs to download
            self.all_download_urls.append(download_url)
            
            # Always add to games dictionary (allow multiple URLs per game_id)
            self.games[game_id] = download_url
            self.games[short_name] = download_url
            
            # Add filename variants
            filename = download['url'][download['url'].rfind('/'):]
            self.games[f"{game_id}{filename}"] = download_url
            self.games[f"{short_name}{filename}"] = download_url
            
            # Collect metadata for JSON output
            game_info = self.games_data.get(game_id, {})
            filename_from_url = download['url'][download['url'].rfind('/') + 1:]
            relative_path = self._get_relative_path(download_url, filename_from_url)
            
            # Get description from the "name" column in game_downloads table
            description = game_name  # Use the "name" column from game_downloads
            if description.startswith("SLUDGE engine game."):
                description = description[len("SLUDGE engine game."):].strip()
                if not description:  # If description is empty after stripping
                    description = "Freeware"
            
            # Extract languages
            languages = self._extract_languages(download)
            
            # Get platform info
            platform_id = download.get('platform', '')
            platform_name = self.platforms_data.get(platform_id, {}).get('name', platform_id)
            
            # Create and add game metadata
            self.add_or_update_game_metadata(
                game_id=game_id,
                relative_path=relative_path,
                description=description,
                download_url=f"https://downloads.scummvm.org{download_url}",
                languages=languages,
                platform=platform_name
            )
        
        summary_parts = [f"Found {len(unique_urls)} compatible game downloads"]
        if skipped_games_count > 0:
            summary_parts.append(f"{skipped_games_count} skipped as incompatible")
        if skipped_addons_count > 0:
            summary_parts.append(f"{skipped_addons_count} addons skipped")
  
        
        summary = f"{summary_parts[0]} ({', '.join(summary_parts[1:])})" if len(summary_parts) > 1 else summary_parts[0]
        self._print(summary)
    
    def get_demos(self):
        """Fetch game demos from Google Sheets"""
        self._temp_print("Fetching list of game demos")
        url = f"{SHEET_URL}&gid={SHEET_IDS['game_demos']}"
        body = self.get_google_sheet(url)
        
        unique_urls = set()
        skipped_demos_count = 0
        for download in self.parse_tsv(body):
            game_id = download.get('id', '')
            
            # Allow specifying game names without target/engine name
            short_name = game_id[game_id.rfind(':') + 1:] if ':' in game_id else game_id
            
            # Skip games not found in compatibility sheet
            if game_id not in self.compatible_game_ids:
                skipped_demos_count += 1
                continue
            
            # Track unique URLs for compatible games
            demo_url = download['url']
            unique_urls.add(demo_url)
            
            # Add to list of all URLs to download
            self.all_download_urls.append(demo_url)
            
            # Always add to games dictionary (allow multiple URLs per game_id)
            self.games[game_id] = demo_url
            self.games[short_name] = demo_url
            
            filename = download['url'][download['url'].rfind('/'):]
            self.games[f"{game_id}{filename}"] = demo_url
            self.games[f"{short_name}{filename}"] = demo_url
            
            # Collect metadata for JSON output
            game_info = self.games_data.get(game_id, {})
            filename_from_url = download['url'][download['url'].rfind('/') + 1:]
            relative_path = self._get_relative_path(demo_url, filename_from_url)
            
            # Get description from the "category" column in game_demos table
            category = download.get('category', '')
            platform_id = download.get('platform', '')
            platform_name = self.platforms_data.get(platform_id, {}).get('name', platform_id)
            # Format description as '<Platform> <description> Demo'
            description = f"{platform_name} {category} Demo".strip()

            # Extract languages
            languages = self._extract_languages(download)

            # Create and add game metadata
            self.add_or_update_game_metadata(
                game_id=game_id,
                relative_path=relative_path,
                description=description,
                download_url=demo_url,
                languages=languages,
                platform=platform_name
            )
        
        self._print(f"Found {len(unique_urls)} compatible demos ({skipped_demos_count} skipped as incompatible)")
    
    def get_director_demos(self):
        """Fetch director demos from Google Sheets"""
        self._temp_print("Fetching list of director demos")
        url = f"{SHEET_URL}&gid={SHEET_IDS['director_demos']}"
        body = self.get_google_sheet(url)
        
        if not body:
            raise Exception('Failed to fetch director demos')
        
        unique_urls = set()
        skipped_director_demos_count = 0
        for download in self.parse_tsv(body):
            game_id = download.get('id', '')
            
            # Allow specifying game names without target/engine name
            short_name = game_id[game_id.rfind(':') + 1:] if ':' in game_id else game_id
            
            # Skip games not found in compatibility sheet
            if game_id not in self.compatible_game_ids:
                skipped_director_demos_count += 1
                continue
            
            # Track unique URLs for compatible games
            director_demo_url = download['url']
            unique_urls.add(director_demo_url)
            
            # Add to list of all URLs to download
            self.all_download_urls.append(director_demo_url)
            
            # Always add to games dictionary (allow multiple URLs per game_id)
            self.games[game_id] = director_demo_url
            self.games[short_name] = director_demo_url
            
            filename = download['url'][download['url'].rfind('/'):]
            self.games[f"{game_id}{filename}"] = director_demo_url
            self.games[f"{short_name}{filename}"] = director_demo_url
            
            # Collect metadata for JSON output
            filename_from_url = download['url'][download['url'].rfind('/') + 1:]
            relative_path = self._get_relative_path(director_demo_url, filename_from_url)
            
            # Get platform information
            platform_id = download.get('platform', '')
            platform_name = self.platforms_data.get(platform_id, {}).get('name', platform_id)
            # Use title from director demos as description (this is effectively the "category" for director demos)
            title = download.get('title', '')
            # Format description as '<Platform> <description> Demo'
            description = f"{platform_name} {title} Demo".strip()

            # Extract languages
            languages = self._extract_languages(download)

            # Create and add game metadata
            self.add_or_update_game_metadata(
                game_id=game_id,
                relative_path=relative_path,
                description=description,
                download_url=director_demo_url,
                languages=languages,
                platform=platform_name
            )
        
        self._print(f"Found {len(unique_urls)} compatible director demos ({skipped_director_demos_count} skipped as incompatible)")
    
    def download_file(self, url, filename):
        """Download a file from URL with atomic operation"""
        filepath = self.download_dir / filename
        temp_filepath = self.download_dir / f"{filename}.downloading"
        
        try:
            # URL encode the path to handle spaces and special characters
            parsed_url = urllib.parse.urlparse(url)
            encoded_path = urllib.parse.quote(parsed_url.path, safe='/')
            encoded_url = urllib.parse.urlunparse((
                parsed_url.scheme, parsed_url.netloc, encoded_path,
                parsed_url.params, parsed_url.query, parsed_url.fragment
            ))
            
            self._temp_print(f"Downloading {encoded_url}")
            
            # Download to temporary file first
            urllib.request.urlretrieve(encoded_url, temp_filepath)
            
            # Only move to final location if download completed successfully
            temp_filepath.rename(filepath)
            self._temp_print(f"Download completed: {filename}")

            return filepath
        except Exception as e:
            # Clean up temp file if download failed
            if temp_filepath.exists():
                temp_filepath.unlink()
                self._print(f"Cleaned up incomplete download: {temp_filepath}")
            self._print(f"\033[31mError downloading {url}: {e}\033[0m")
            
    
    def extract_zip(self, zip_path):
        """Extract zip file and return the extracted folder path"""
        zip_path = Path(zip_path)
        extract_dir = self.download_dir / zip_path.stem

        if extract_dir.exists():
            self._print(f"Directory {extract_dir} already exists, skipping extraction")
            return extract_dir

        self._temp_print(f"Extracting {zip_path} to {extract_dir}")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            all_names = zip_ref.namelist()
            top_dir = zip_path.stem + "/"
            # Check if all files are under a single top-level directory matching the archive name
            # Filter out __MACOSX and analyze top-level entries
            root_files = []
            root_folders = []
            
            for name in all_names:
                # Skip __MACOSX folder and its contents
                if name.startswith('__MACOSX/'):
                    continue
                
                # Check if it's a root entry
                parts = name.split('/')
                if len(parts) == 1 and parts[0]:  # Root file
                    root_files.append(name)
                elif len(parts) > 1 and parts[0] and not parts[1]:  # Root folder (ends with /)
                    root_folders.append(parts[0])
            print(root_folders)
            # Remove duplicates from root folders list
            root_folders = list(set(root_folders))
            
            # If there are no root files and exactly one root folder, extract that folder's contents
            if not root_files and len(root_folders) == 1:
                single_folder = root_folders[0] + '/'
                for member in zip_ref.infolist():
                    member_path = Path(member.filename)
                    # Skip directory entries
                    if member.is_dir() or (len(member_path.parts) > 0 and member_path.parts[0] == '__MACOSX'):
                        continue
                    # Remove the top_dir prefix
                    print(member_path, single_folder)
                    rel_path = member_path.relative_to(single_folder)
                    target_path = extract_dir / rel_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zip_ref.open(member) as source, open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)
            else:
                print(f"Extracting single folder")
                print(root_files)
                print(root_folders)
                print("----")
                print("----")
                # Standard extraction
                zip_ref.extractall(extract_dir)

        # Remove the zip file after extraction
        zip_path.unlink()
        self._temp_print(f"Removed {zip_path}")

        return extract_dir
    
    def get_remote_folders(self):
        """Get list of all direct child folders on remote server
        Returns a set of folder names"""
        if not self.scp_server or not self.scp_path:
            return set()
        
        try:
            ssh_cmd = self._build_controlpath_ssh_command()
            env = os.environ.copy()
            ssh_cmd.extend([
                self.scp_server,
                f'ls -1 "{self.scp_path}"'
            ])
            
            result = subprocess.run(ssh_cmd, capture_output=True, check=False, env=env, timeout=30, text=True)
            
            if result.returncode == 0:
                folders = set()
                for line in result.stdout.strip().split('\n'):
                    line = line.strip()
                    if line:
                        # Verify this is actually a directory
                        test_cmd = self._build_controlpath_ssh_command()
                        test_cmd.extend([
                            self.scp_server,
                            f'test -d "{self.scp_path}/{line}"'
                        ])
                        test_result = subprocess.run(test_cmd, capture_output=True, check=False, env=env, timeout=10)
                        if test_result.returncode == 0:
                            folders.add(line)
                return folders
            else:
                raise Exception(f"Remote command failed with return code {result.returncode}: {result.stderr}")
                
        except subprocess.TimeoutExpired:
            raise Exception("Connection timeout while getting remote folders")
        except Exception as e:
            raise Exception(f"Error getting remote folders: {e}")
    
    def folder_exists_on_remote(self, folder_name, remote_folders_set=None):
        """Check if folder exists on remote server
        If remote_folders_set is provided, removes the folder from the set when found
        Returns True if folder exists, False if it doesn't exist
        Raises exception if cannot connect to remote server"""
        if not self.scp_server or not self.scp_path:
            return False
        
        # Use pre-fetched folder set if provided (more efficient)
        if remote_folders_set is not None:
            if folder_name in remote_folders_set:
                remote_folders_set.remove(folder_name)
                return True
            return False
        
        # Fallback to individual SSH check (less efficient)
        try:
            ssh_cmd = self._build_controlpath_ssh_command()
            env = os.environ.copy()
            ssh_cmd.extend([
                self.scp_server,
                f'test -d "{self.scp_path}/{folder_name}"'
            ])
            
            result = subprocess.run(ssh_cmd, capture_output=True, check=False, env=env, timeout=30)
            
            # Check for connection/authentication failures
            if result.returncode == 255:  # SSH connection failure
                stderr_output = result.stderr.decode('utf-8', errors='ignore').lower()
                if any(error in stderr_output for error in [
                    'connection refused', 'no route to host', 'connection timed out',
                    'permission denied', 'authentication failed', 'could not resolve hostname'
                ]):
                    raise Exception(f"Failed to connect to remote server: {result.stderr.decode('utf-8', errors='ignore').strip()}")
            
            # Return code 0 means folder exists, 1 means it doesn't exist, 255 means connection error
            if result.returncode in [0, 1]:
                return result.returncode == 0
            else:
                # Other return codes indicate system errors
                raise Exception(f"Remote command failed with return code {result.returncode}: {result.stderr.decode('utf-8', errors='ignore').strip()}")
                
        except subprocess.TimeoutExpired:
            raise Exception("Connection timeout while checking remote folder")
        except Exception as e:
            if "Failed to connect" in str(e) or "Connection timeout" in str(e) or "Remote command failed" in str(e):
                raise  # Re-raise connection/command errors
            else:
                raise Exception(f"Error checking remote folder: {e}")
    
    def upload_folder(self, folder_path):
        """Upload folder to remote server via SCP with atomic upload handling
        Returns True if upload occurred, False if skipped"""
        if not self.scp_server or not self.scp_path:
            self._print("No SCP server configured, skipping upload")
            return False
        
        folder_path = Path(folder_path)
        folder_name = folder_path.name
        
        # Upload to temporary location first for atomic operation
        temp_name = f"{folder_name}.uploading"
        
        # Clean up any existing temp folder first
        try:
            cleanup_cmd = self._build_controlpath_ssh_command()
            env = os.environ.copy()
            cleanup_cmd.extend([
                self.scp_server,
                f'rm -rf "{self.scp_path}/{temp_name}"'
            ])
            
            subprocess.run(cleanup_cmd, check=False, env=env, capture_output=True)
            self._temp_print(f"Cleaned up any existing temp folder {temp_name}")
        except Exception:
            pass  # Ignore cleanup errors
        
        try:
            self._temp_print(f"Uploading {folder_path} to {self.scp_server}:{self.scp_path}/{temp_name}")
            
            # Prepare SCP command - upload to temporary name
            scp_cmd = self._build_controlpath_ssh_command('scp')
            env = os.environ.copy()
            scp_cmd.append('-r')  # Add recursive flag for SCP
            
            # Upload to temp location
            scp_cmd.extend([
                str(folder_path),
                f"{self.scp_server}:{self.scp_path}/{temp_name}"
            ])
            
            # Upload to temp location
            subprocess.run(scp_cmd, check=True, env=env)
            
            # Prepare SSH command for atomic move
            ssh_cmd = self._build_controlpath_ssh_command()
            ssh_cmd.extend([
                self.scp_server,
                f'mv "{self.scp_path}/{temp_name}" "{self.scp_path}/{folder_name}"'
            ])
            
            # Move to final location atomically
            subprocess.run(ssh_cmd, check=True, env=env)
            
            self._print(f"\033[1;32mGame {folder_name} successfully uploaded\033[0m")
            
            return True
            
        except subprocess.CalledProcessError as e:
            self._print(f"Upload failed for {folder_name}: {e}")
            # Clean up temp folder on remote if it exists
            try:
                cleanup_cmd = self._build_controlpath_ssh_command()
                env = os.environ.copy()
                cleanup_cmd.extend([
                    self.scp_server,
                    f'rm -rf "{self.scp_path}/{temp_name}"'
                ])
                subprocess.run(cleanup_cmd, check=False, env=env)
                self._print(f"Cleaned up failed upload temp folder {temp_name}")
            except Exception:
                pass
            return False
    
    def build_http_index(self):
        """Build HTTP index locally from remote directory listing and upload index.json files only if they don't exist"""
        if not self.scp_server or not self.scp_path:
            return
        
        try:
            self._temp_print("Getting remote directory listing...")
            
            # Get recursive directory listing from remote server
            ssh_cmd = self._build_controlpath_ssh_command()
            env = os.environ.copy()
            
            # Use find to get all directories and files with their sizes
            find_command = f'cd "{self.scp_path}" && find . -type f -exec stat -c "%s %n" {{}} \\; 2>/dev/null'
            ssh_cmd.extend([self.scp_server, find_command])
            
            result = subprocess.run(ssh_cmd, capture_output=True, check=False, env=env, text=True)
            
            if result.returncode != 0:
                self._print(f"Warning: Could not get remote directory listing: {result.stderr}")
                return
            
            # Parse the output to build directory structure
            file_tree = {}
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                    
                parts = line.strip().split(' ', 1)
                if len(parts) != 2:
                    continue
                    
                size_str, filepath = parts
                try:
                    size = int(size_str)
                except ValueError:
                    continue
                
                # Remove leading ./
                if filepath.startswith('./'):
                    filepath = filepath[2:]
                
                # Skip hidden files and existing index.json files
                if any(part.startswith('.') for part in filepath.split('/')) or filepath.endswith('index.json'):
                    continue
                
                # Build nested directory structure
                path_parts = filepath.split('/')
                current_tree = file_tree
                
                # Navigate/create directory structure
                for i, part in enumerate(path_parts[:-1]):
                    if part not in current_tree:
                        current_tree[part] = {}
                    current_tree = current_tree[part]
                
                # Add file with size
                filename = path_parts[-1]
                current_tree[filename] = size
            
            # Generate index.json files for each directory (only if they don't exist)
            self._generate_index_files(file_tree, self.scp_path)
            
            self._temp_print("HTTP index built successfully")
            
        except Exception as e:
            self._print(f"Error building HTTP index: {e}")
    
    def _generate_index_files(self, tree, remote_path, current_path=""):
        """Recursively generate and upload index.json files for directory tree (always overwrite root, only create missing subfolders)"""
        try:
            # Check if index.json already exists in this directory
            remote_index_path = f"{remote_path}/{current_path}/index.json" if current_path else f"{remote_path}/index.json"
            
            # Always overwrite root index.json, but only create missing subfolder index.json files
            should_create_index = current_path == ""  # Root directory - always overwrite
            
            if not should_create_index:
                # For subfolders, test if index.json exists on remote
                ssh_cmd = self._build_controlpath_ssh_command()
                env = os.environ.copy()
                ssh_cmd.extend([
                    self.scp_server,
                    f'test -f "{remote_index_path}"'
                ])
                
                result = subprocess.run(ssh_cmd, capture_output=True, check=False, env=env, timeout=10)
                should_create_index = result.returncode != 0  # Create if doesn't exist
                
                if not should_create_index:
                    self._temp_print(f"Skipping existing index.json in {current_path}")
            
            if should_create_index:
                # Create and upload index.json (always for root, only if missing for subfolders)
                # Create a simplified tree where directories are empty objects
                simplified_tree = {}
                for key, value in tree.items():
                    if isinstance(value, dict):  # It's a directory
                        simplified_tree[key] = {}  # Empty object for directories
                    else:  # It's a file
                        simplified_tree[key] = value  # Keep file size
                
                index_content = json.dumps(simplified_tree, indent=2, ensure_ascii=False)
                
                # Create temporary local index.json file
                temp_index_file = self.download_dir / "temp_index.json"
                with open(temp_index_file, 'w', encoding='utf-8') as f:
                    f.write(index_content)
                
                # Upload index.json to remote directory
                scp_cmd = self._build_controlpath_ssh_command('scp')
                env = os.environ.copy()
                scp_cmd.extend([
                    str(temp_index_file),
                    f"{self.scp_server}:{remote_index_path}"
                ])
                
                subprocess.run(scp_cmd, check=True, env=env)
                if current_path == "":
                    self._temp_print(f"Uploaded/updated root index.json")
                else:
                    self._temp_print(f"Uploaded index.json to {current_path}")
                
                # Clean up temp file
                temp_index_file.unlink()
            
            # Recursively process subdirectories
            for key, value in tree.items():
                if isinstance(value, dict):  # It's a directory
                    subdir_path = f"{current_path}/{key}" if current_path else key
                    # Create a copy of the tree with only this subdirectory's contents
                    subdir_tree = value.copy()
                    self._generate_index_files(subdir_tree, remote_path, subdir_path)
                    
        except subprocess.TimeoutExpired:
            self._print(f"Timeout checking index.json in {current_path or 'root'}")
        except Exception as e:
            self._print(f"Error generating index for {current_path or 'root'}: {e}")
            # Clean up temp file if it exists
            temp_index_file = self.download_dir / "temp_index.json"
            if temp_index_file.exists():
                temp_index_file.unlink()
    
    def generate_games_json(self):
        """Generate games.json file with all available games metadata"""
        try:
            output_file = Path.cwd() / "games.json"
            
            # Sort games by id for consistent output
            sorted_games = sorted(self.games_metadata, key=lambda x: x['id'].lower())
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(sorted_games, f, indent=2, ensure_ascii=False)
            
            self._print(f"Generated games.json with {len(sorted_games)} games")
            
        except Exception as e:
            self._print(f"Error generating games.json: {e}")
    
    def generate_processed_games_json(self):
        """Generate games.json file with only processed games metadata"""
        try:
            output_file = Path.cwd() / "games.json"
            
            # Sort games by id for consistent output
            sorted_games = sorted(self.processed_games_metadata, key=lambda x: x['id'].lower())
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(sorted_games, f, indent=2, ensure_ascii=False)
            
            self._print(f"Generated games.json with {len(sorted_games)} processed games")
            
        except Exception as e:
            self._print(f"Error generating games.json: {e}")
    
    def _get_target_name_for_game_id(self, game_id):
        """Get the target filename/foldername that would be created for a game_id"""
        if game_id.startswith('http'):
            url = game_id
            filename = url[url.rfind('/') + 1:]
        elif game_id not in self.games:
            return game_id  # Fallback to game_id if not found
        else:
            url = f"https://downloads.scummvm.org{self.games[game_id]}"
            temp_game_id = game_id
            if '/' in temp_game_id:
                temp_game_id = temp_game_id[:temp_game_id.rfind('/')]
            temp_game_id = temp_game_id[temp_game_id.rfind(':') + 1:]  # Remove target from target:gameId
            filename = url[url.rfind('/') + 1:]
            if not filename.startswith(temp_game_id):
                filename = f"{temp_game_id}-{filename}"
        
        # Return the target name (folder name after extraction or file name)
        if filename.endswith('.zip'):
            return filename[:-4]  # Remove .zip extension
        else:
            return filename
    
    def download_and_process_games(self, game_ids, max_transfers=None):
        """Download, extract, and optionally upload games, using persistent SSH connection"""
        transfer_count = 0
        any_uploads_occurred = False

        # Get all remote folders once at the beginning
        try:
            remote_folders = self.get_remote_folders()
            self._print(f"Found {len(remote_folders)} folders on remote server")
        except Exception as e:
            self._print(f"Error getting remote folders: {e}")
            return

        # Create a unified list of all games to process
        # Start with Google Sheets data, then merge in metadata (metadata takes precedence)
        games_dict = {}  # key: target_name, value: game_data
        
        # First, add downloadable games from game_ids (Google Sheets data)
        for game_id in game_ids:
            if game_id.startswith('http'):
                url = game_id
                filename = url[url.rfind('/') + 1:]
                target_name = filename[:-4] if filename.endswith('.zip') else filename
                games_dict[target_name] = {
                    'game_id': game_id,
                    'url': url,
                    'filename': filename,
                    'target_name': target_name,
                    'source': 'google_sheets'
                }
            elif game_id not in self.games:
                self._print(f"GameID {game_id} not known")
                sys.exit(1)
            else:
                url = f"https://downloads.scummvm.org{self.games[game_id]}"
                if '/' in game_id:
                    game_id = game_id[:game_id.rfind('/')]
                game_id = game_id[game_id.rfind(':') + 1:]  # Remove target from target:gameId
                filename = url[url.rfind('/') + 1:]
                if not filename.startswith(game_id):
                    filename = f"{game_id}-{filename}"
                target_name = filename[:-4] if filename.endswith('.zip') else filename
                games_dict[target_name] = {
                    'game_id': game_id,
                    'url': url,
                    'filename': filename,
                    'target_name': target_name,
                    'source': 'google_sheets'
                }

        # Then, merge in games from metadata (metadata takes precedence)
        for metadata in self.games_metadata:
            target_name = metadata['relative_path']
            if target_name in self.metadata and not self.metadata[target_name].get("skip", False):
                download_url = metadata.get('download_url', '')
                
                if target_name in games_dict:
                    # Merge metadata into existing Google Sheets entry
                    games_dict[target_name].update({
                        'download_url': download_url,
                        'metadata': metadata,
                        'source': 'merged'
                    })
                else:
                    # New entry from metadata only
                    games_dict[target_name] = {
                        'target_name': target_name,
                        'download_url': download_url,
                        'metadata': metadata,
                        'source': 'metadata_only'
                    }

        # Convert dict back to list for processing
        all_games_to_process = list(games_dict.values())

        # Process all games in the unified list
        for game_data in all_games_to_process:
            # Determine if this game can be downloaded from ScummVM
            has_scummvm_download = False
            url = None
            filename = None
            should_process_metadata = False
            
            # Check for download URL in priority order: url (Google Sheets) -> download_url (metadata)
            if 'url' in game_data:
                # From Google Sheets (game_ids)
                url = game_data['url']
                filename = game_data['filename']
                has_scummvm_download = url.startswith('https://downloads.scummvm.org/frs')
            elif 'download_url' in game_data and game_data['download_url']:
                # From metadata - convert relative URL to absolute if needed
                download_url = game_data['download_url']
                if download_url.startswith('/frs'):
                    url = f"https://downloads.scummvm.org{download_url}"
                    has_scummvm_download = True
                elif download_url.startswith('https://downloads.scummvm.org/frs'):
                    url = download_url
                    has_scummvm_download = True
                else:
                    has_scummvm_download = False
                
                if has_scummvm_download:
                    filename = url[url.rfind('/') + 1:]

            # Get target name (should be available in all entries now)
            target_name = game_data['target_name']

            # Check if already exists on remote server
            exists_on_remote = self.folder_exists_on_remote(target_name, remote_folders)
            
            # Check skip flag - but include skipped games that exist on remote
            is_skipped = self.metadata.get(target_name, {}).get("skip", False)
            if is_skipped and not exists_on_remote:
                self._print(f"\033[93mSkipping game based on metadata skip flag: {target_name}\033[0m")
            elif is_skipped and exists_on_remote:
                self._print(f"\033[93mGame {target_name} is marked as skip but exists on remote server, including in export\033[0m")
                should_process_metadata = True
                # Clean up any local files for completed remote games
                if has_scummvm_download and filename:
                    local_folder_path = self.download_dir / target_name
                    local_zip_path = self.download_dir / filename
                    if local_folder_path.exists():
                        if local_folder_path.is_dir():
                            shutil.rmtree(local_folder_path)
                        else:
                            local_folder_path.unlink()
                    if local_zip_path and local_zip_path.exists():
                        local_zip_path.unlink()
            elif exists_on_remote:
                self._print(f"\033[92mGame {target_name} already exists on remote server, skipping\033[0m")
                should_process_metadata = True
                # Clean up any local files for completed remote games
                if has_scummvm_download and filename:
                    local_folder_path = self.download_dir / target_name
                    local_zip_path = self.download_dir / filename
                    if local_folder_path.exists():
                        if local_folder_path.is_dir():
                            shutil.rmtree(local_folder_path)
                        else:
                            local_folder_path.unlink()
                    if local_zip_path and local_zip_path.exists():
                        local_zip_path.unlink()
            else:
                # Game doesn't exist on remote - need to process it
                if not has_scummvm_download:
                    self._print(f"\033[91mGame {target_name} does not exist on remote server and has no downloadable URL\033[0m")
                    raise FileNotFoundError(f"Game {target_name} not found on remote server and cannot be downloaded")
                
                # From here on, we know the game has a ScummVM download URL and doesn't exist on remote
                local_folder_path = self.download_dir / target_name
                local_zip_path = self.download_dir / filename if filename.endswith('.zip') else None

                # Check if we've reached the transfer limit
                allow_transfers = max_transfers is None or transfer_count < max_transfers
                if not allow_transfers:
                    self._print(f"Reached transfer limit of {max_transfers}, skipping downloads/uploads but continuing to process games")

                # Check if extracted folder already exists locally
                if filename.endswith('.zip') and local_folder_path.exists():
                    # Upload since we know it doesn't exist on remote (but only if transfers allowed)
                    if allow_transfers and self.upload_folder(local_folder_path):
                        transfer_count += 1
                        any_uploads_occurred = True

                    should_process_metadata = True
                    # Clean up local folder
                    if local_folder_path.exists():
                        if local_folder_path.is_dir():
                            shutil.rmtree(local_folder_path)
                        else:
                            local_folder_path.unlink()
                else:
                    # Need to download and process the game
                    file_path = self.download_dir / filename
                    temp_file_path = self.download_dir / f"{filename}.downloading"

                    # Clean up any stale temp download files
                    if temp_file_path.exists():
                        temp_file_path.unlink()
                        self._print(f"Cleaned up stale temp download: {temp_file_path}")

                    # Download file if needed and transfers are allowed
                    downloaded_file = None
                    if allow_transfers:
                        if not file_path.exists():
                            downloaded_file = self.download_file(url, filename)
                        else:
                            downloaded_file = file_path

                        # Extract if it's a zip file
                        if filename.endswith('.zip'):
                            extracted_folder = self.extract_zip(downloaded_file)
                            # Upload the extracted folder
                            if self.upload_folder(extracted_folder):
                                transfer_count += 1
                                any_uploads_occurred = True

                        should_process_metadata = True
                    else:
                        # Clean up any local files when transfers not allowed
                        if file_path.exists():
                            file_path.unlink()
                        if local_folder_path and local_folder_path.exists():
                            if local_folder_path.is_dir():
                                shutil.rmtree(local_folder_path)
                            else:
                                local_folder_path.unlink()
                        if local_zip_path and local_zip_path.exists():
                            local_zip_path.unlink()

            # Add to processed games list (single place for all cases)
            if should_process_metadata:
                # Find the metadata for this game by target_name (relative_path)
                game_metadata = None
                for metadata in self.games_metadata:
                    if metadata.get('relative_path') == target_name:
                        game_metadata = metadata
                        break
                
                if game_metadata:
                    self.processed_games_metadata.append(game_metadata)
                else:
                    self._print(f"Warning: No metadata found for {target_name}")

        # Check for orphaned folders on remote server
        if remote_folders:
            orphaned_folders = list(remote_folders)
            self._print(f"\033[91mError: Found {len(orphaned_folders)} orphaned folders on remote server that don't correspond to any game:\033[0m")
            for folder in sorted(orphaned_folders):
                self._print(f"  - {folder}")
            raise Exception(f"Orphaned folders found on remote server: {', '.join(sorted(orphaned_folders))}")

        # Generate games.json file with only processed games before building HTTP index
        if self.processed_games_metadata:
            self.generate_processed_games_json()
        else:
            self._print("No games were processed, skipping games.json generation")

        # Build HTTP index once after all uploads are complete
        self._print("Building HTTP index after all uploads...")
        self.build_http_index()
    
    

def main():
    parser = argparse.ArgumentParser(
        description='Download ScummVM games and demos',
        epilog='''
Environment Variables:
  SSH_KEY_PATH    Path to SSH private key for SCP authentication
                  (e.g., ~/.ssh/id_rsa)
  SSH_PASSWORD    SSH password for authentication (requires sshpass)
                  Note: SSH keys are preferred over passwords for security
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('games', nargs='*', help='Game IDs to download (if none specified, downloads all)')
    parser.add_argument('--download-dir', default='games', help='Directory to download games to')
    parser.add_argument('--scp-server', help='SCP server for uploading (user@host)')
    parser.add_argument('--scp-path', help='Remote path for uploading games')
    parser.add_argument('--scp-port', type=int, help='SSH/SCP port (default: 22)')
    parser.add_argument('--max-transfers', type=int, help='Maximum number of games to transfer (excluding skipped ones)')
    
    args = parser.parse_args()
    
    # Filter out testbed and playground3d
    game_ids = [g for g in args.games if g not in ['testbed', 'playground3d']]
    
    # Fallback to environment variables if CLI args are not provided
    scp_server = args.scp_server or (os.environ.get('SSH_USER') + '@' + os.environ.get('SSH_HOST') if os.environ.get('SSH_USER') and os.environ.get('SSH_HOST') else None)
    scp_path = args.scp_path or os.environ.get('SSH_PATH')
    scp_port = args.scp_port or (int(os.environ.get('SSH_PORT')) if os.environ.get('SSH_PORT') else None)
    downloader = GameDownloader(
        download_dir=args.download_dir,
        scp_server=scp_server,
        scp_path=scp_path,
        scp_port=scp_port
    )

    
    downloader._open_ssh_connection()

    try:
        """Main execution function"""

        """
        TODO: THis needs restructuring:
        - get all game metadata from the gsheet, mark all games that aren't in teh compatible table as skip
        - merge with metadata.json, override all fields
        - loop through the new list, sync everything that isn't marked skip. 
        - exit if a file that doesn't have a downloadUrl isn't present
        - exit if a file is present that isn't in the list 
        - exit if a file is present that's marked as skip
        - output list of all files present on the server

        skip download of wage-games-master-1.0.zip (it's a bundle)
        """

                
        # Fetch compatibility data first
        downloader.get_compatible_games()
        
        # Fetch platform and game reference data
        downloader.get_platforms_data()
        downloader.get_games_data()
        
        # Fetch all game lists
        downloader.get_game_downloads()
        downloader.get_demos()
        downloader.get_director_demos()
        
        # Load metadata and apply overrides to existing games and add new games from metadata
        downloader._load_and_apply_metadata()
        
        # If no game IDs specified, download all games
        if not args.games:
            # Use all collected URLs, but convert them to a format that download_and_process_games expects
            # Get unique URLs to avoid downloading the same file multiple times
            unique_urls = list(set(downloader.all_download_urls))
            # Convert URLs to full HTTP URLs for processing
            game_ids = []
            for url in unique_urls:
                if url.startswith('/frs/'):
                    full_url = f"https://downloads.scummvm.org{url}"
                else:
                    full_url = url
                game_ids.append(full_url)
        
        # Sort game_ids by their target filename/foldername for consistent processing order (case-insensitive)
        game_ids.sort(key=lambda x: downloader._get_target_name_for_game_id(x).lower())

        downloader.download_and_process_games(game_ids, args.max_transfers)



    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    finally:
        if scp_server and scp_path:
            try:
                downloader._close_ssh_connection()
            except Exception:
                pass


if __name__ == "__main__":
    main()