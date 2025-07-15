#!/usr/bin/env python3
import os
import sys
import subprocess
import json
import argparse
import re
import shlex
import platform
import shutil
from pathlib import Path
from datetime import datetime

# Fix for Windows Unicode encoding issues
if platform.system() == "Windows":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

class OrphanedProxyCleanup:
    def __init__(self, proxy_path, dry_run=True, force_accept=False, recursive=True):
        self.proxy_path = Path(proxy_path)
        self.dry_run = dry_run
        self.force_accept = force_accept
        self.recursive = recursive
        self.video_extensions = {'.mp4', '.mov', '.mxf', '.avi', '.mkv'}
        self.stats = {
            'proxy_files_found': 0,
            'orphaned_proxies': 0,
            'deleted_files': 0,
            'errors': 0
        }
        self.orphaned_proxies = []
        self.log_messages = []
        
        # Check for required tools
        self._check_requirements()
        
    def _check_requirements(self):
        """Check if ffprobe is installed"""
        if not shutil.which('ffprobe'):
            print("❌ Missing required tool: ffprobe")
            print("\nInstallation instructions:")
            if platform.system() == 'Darwin':  # macOS
                print("Using Homebrew: brew install ffmpeg")
            elif platform.system() == 'Windows':
                print("Using Chocolatey: choco install ffmpeg")
            else:
                print("Install ffmpeg package for your distribution")
            sys.exit(1)
    
    def _log(self, message):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.log_messages.append(log_message)
    
    def _is_proxy_valid(self, proxy_path):
        """Check if existing proxy is valid (reused from proxy_generator.py)"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,codec_name',
                '-of', 'json',
                str(proxy_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
            # Check if we got valid video stream data
            if 'streams' in data and len(data['streams']) > 0:
                return True
            else:
                return False
                
        except (subprocess.CalledProcessError, json.JSONDecodeError, Exception):
            return False
    
    def _detect_sony_proxy_pair(self, proxy_path):
        """Detect Sony proxy pattern (reused from proxy_generator.py)"""
        proxy_path = Path(proxy_path)
        base_name = proxy_path.stem
        extension = proxy_path.suffix
        
        # Check if this appears to be a Sony proxy (has suffix pattern like S03, S02, etc.)
        proxy_pattern = re.compile(r'S\d+$')
        
        if proxy_pattern.search(base_name):
            # This is a Sony proxy file
            # Extract the original base name by removing the S## suffix
            original_base_name = re.sub(r'S\d+$', '', base_name)
            return True, original_base_name, extension
        else:
            return False, None, None
    
    def _is_proxy_file(self, file_path):
        """Determine if a file is a proxy based on naming patterns"""
        file_path = Path(file_path)
        
        # Check file extension
        if file_path.suffix.lower() not in self.video_extensions:
            return False
            
        # Check for standard proxy pattern
        if '_proxy' in file_path.stem.lower():
            return True
            
        # Check for Sony proxy pattern
        is_sony_proxy, _, _ = self._detect_sony_proxy_pair(file_path)
        return is_sony_proxy
    
    def _get_original_filename_from_proxy(self, proxy_path):
        """Get the expected original filename from a proxy file"""
        proxy_path = Path(proxy_path)
        
        # Handle Sony proxy pattern
        is_sony_proxy, original_base_name, extension = self._detect_sony_proxy_pair(proxy_path)
        if is_sony_proxy:
            return f"{original_base_name}{extension}"
        
        # Handle standard proxy pattern
        if '_proxy' in proxy_path.stem.lower():
            # Remove _proxy suffix and get original name
            original_stem = proxy_path.stem
            # Find the last occurrence of _proxy (case insensitive)
            proxy_index = original_stem.lower().rfind('_proxy')
            if proxy_index != -1:
                original_stem = original_stem[:proxy_index]
            return f"{original_stem}{proxy_path.suffix}"
        
        return None
    
    def _search_for_original_file(self, expected_filename, search_root):
        """Search for original file in sibling directories"""
        search_root = Path(search_root)
        
        # Search in the root directory and all subdirectories
        for root, dirs, files in os.walk(search_root):
            root_path = Path(root)
            
            # Skip proxy directories
            if root_path.name.lower() in ['proxies', 'proxy']:
                continue
                
            # Skip any directory that contains "archive" in its path (case insensitive)
            if 'archive' in str(root_path).lower():
                continue
                
            # Check for exact filename match
            for file in files:
                if file.lower() == expected_filename.lower():
                    candidate_path = root_path / file
                    # Validate it's a video file
                    if candidate_path.suffix.lower() in self.video_extensions:
                        return candidate_path
        
        return None
    
    def _find_orphaned_proxies(self):
        """Find all orphaned proxy files in the proxy directory"""
        if not self.proxy_path.exists():
            self._log(f"❌ Proxy directory does not exist: {self.proxy_path}")
            return []
        
        if not self.proxy_path.is_dir():
            self._log(f"❌ Proxy path is not a directory: {self.proxy_path}")
            return []
        
        self._log(f"🔍 Scanning proxy directory: {self.proxy_path}")
        
        # Get parent directory to search for originals
        search_root = self.proxy_path.parent
        self._log(f"🔍 Searching for originals in: {search_root}")
        
        orphaned_proxies = []
        
        # Scan all files in proxy directory
        for file_path in self.proxy_path.iterdir():
            if not file_path.is_file():
                continue
                
            # Check if it's a proxy file
            if not self._is_proxy_file(file_path):
                continue
                
            self.stats['proxy_files_found'] += 1
            
            # Validate the proxy file
            if not self._is_proxy_valid(file_path):
                self._log(f"⚠️  Invalid proxy file (skipping): {file_path.name}")
                continue
            
            # Get expected original filename
            expected_original = self._get_original_filename_from_proxy(file_path)
            if not expected_original:
                self._log(f"⚠️  Could not determine original filename for: {file_path.name}")
                continue
            
            # Search for the original file
            original_path = self._search_for_original_file(expected_original, search_root)
            
            if original_path:
                self._log(f"✅ Found original for {file_path.name}: {original_path}")
            else:
                self._log(f"❌ ORPHANED: {file_path.name} (expected: {expected_original})")
                orphaned_proxies.append({
                    'proxy_path': file_path,
                    'expected_original': expected_original,
                    'size_mb': file_path.stat().st_size / (1024 * 1024)
                })
                self.stats['orphaned_proxies'] += 1
        
        return orphaned_proxies
    
    def _confirm_deletion(self, orphaned_proxies):
        """Confirm deletion with user"""
        if not orphaned_proxies:
            return True
            
        if self.force_accept:
            return True
        
        print(f"\n🗑️  Found {len(orphaned_proxies)} orphaned proxy files:")
        print("=" * 80)
        
        total_size = 0
        for i, orphan in enumerate(orphaned_proxies, 1):
            size_mb = orphan['size_mb']
            total_size += size_mb
            print(f"{i:3d}. {orphan['proxy_path'].name} ({size_mb:.1f}MB)")
            print(f"     Expected original: {orphan['expected_original']}")
            print()
        
        print(f"Total size to be deleted: {total_size:.1f}MB")
        print("=" * 80)
        
        if self.dry_run:
            print("👀 DRY RUN MODE - No files will be deleted")
            return True
        
        while True:
            response = input("\nDo you want to delete these orphaned proxy files? (y/N): ").strip().lower()
            if response in ['y', 'yes']:
                return True
            elif response in ['n', 'no', '']:
                return False
            else:
                print("Please enter 'y' for yes or 'n' for no.")
    
    def _delete_orphaned_proxies(self, orphaned_proxies):
        """Delete the orphaned proxy files"""
        if not orphaned_proxies:
            return
            
        for orphan in orphaned_proxies:
            proxy_path = orphan['proxy_path']
            
            if self.dry_run:
                self._log(f"🔥 [DRY RUN] Would delete: {proxy_path.name}")
                continue
                
            try:
                proxy_path.unlink()
                self._log(f"🗑️  Deleted: {proxy_path.name}")
                self.stats['deleted_files'] += 1
            except Exception as e:
                self._log(f"❌ Error deleting {proxy_path.name}: {str(e)}")
                self.stats['errors'] += 1
    
    def _print_final_stats(self):
        """Print final statistics"""
        print(f"\n📊 CLEANUP SUMMARY:")
        print("=" * 40)
        print(f"Proxy files found: {self.stats['proxy_files_found']}")
        print(f"Orphaned proxies: {self.stats['orphaned_proxies']}")
        
        if self.dry_run:
            print(f"Files that would be deleted: {self.stats['orphaned_proxies']}")
        else:
            print(f"Files deleted: {self.stats['deleted_files']}")
            print(f"Errors: {self.stats['errors']}")
        
        print("=" * 40)
    
    def _prompt_for_actual_deletion(self):
        """Prompt user to proceed with actual deletion after dry run"""
        print(f"\n🤔 DRY RUN COMPLETE")
        print("=" * 60)
        print(f"Found {len(self.orphaned_proxies)} orphaned proxy files ({sum(orphan['size_mb'] for orphan in self.orphaned_proxies):.1f}MB total)")
        print("\nWould you like to proceed with the actual deletion now?")
        print("This will permanently delete the orphaned proxy files listed above.")
        
        while True:
            response = input("\nProceed with deletion? (y/N): ").strip().lower()
            if response in ['y', 'yes']:
                print("\n🗑️  Proceeding with actual deletion...")
                self.dry_run = False  # Switch off dry run mode
                self._delete_orphaned_proxies(self.orphaned_proxies)
                
                # Print updated final statistics
                print(f"\n📊 DELETION COMPLETE:")
                print("=" * 40)
                print(f"Files deleted: {self.stats['deleted_files']}")
                print(f"Errors: {self.stats['errors']}")
                print("=" * 40)
                break
            elif response in ['n', 'no', '']:
                print("❌ Deletion cancelled. Orphaned proxy files remain unchanged.")
                break
            else:
                print("Please enter 'y' for yes or 'n' for no.")
    
    def run(self):
        """Main cleanup process"""
        self._log("🧹 Starting orphaned proxy cleanup...")
        
        # Find orphaned proxies
        orphaned_proxies = self._find_orphaned_proxies()
        
        if not orphaned_proxies:
            self._log("🎉 No orphaned proxy files found!")
            return
        
        # Store orphaned proxies for potential reuse
        self.orphaned_proxies = orphaned_proxies
        
        # Confirm deletion
        if self._confirm_deletion(orphaned_proxies):
            self._delete_orphaned_proxies(orphaned_proxies)
        else:
            self._log("❌ Cleanup cancelled by user")
        
        # Print final statistics
        self._print_final_stats()
        
        # If this was a dry run and we found orphans, prompt to do actual deletion
        if self.dry_run and orphaned_proxies and not self.force_accept:
            self._prompt_for_actual_deletion()

def _clean_path_input(path_input):
    """Clean path input to handle copy-paste scenarios with quotes (reused from proxy_generator.py)"""
    if not path_input:
        return path_input
    
    # First, strip any leading/trailing whitespace
    cleaned = path_input.strip()
    
    # If the input is already quoted, use shlex to handle it properly
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        try:
            parsed = shlex.split(cleaned)
            return parsed[0] if len(parsed) == 1 else ' '.join(parsed)
        except ValueError:
            # If shlex fails, manually strip quotes
            return cleaned[1:-1] if len(cleaned) >= 2 else cleaned
    
    # For Windows, check if this looks like a drive path (e.g., C:\, D:\, F:\)
    # If so, treat the entire input as a single path regardless of spaces
    if platform.system() == "Windows":
        # Check for Windows drive letter pattern (e.g., C:, D:, F:)
        if len(cleaned) >= 2 and cleaned[1] == ':' and cleaned[0].isalpha():
            # This looks like a Windows path, return as-is
            return cleaned
        # Also check for UNC paths (\\server\share)
        elif cleaned.startswith('\\\\'):
            return cleaned
    
    # For non-Windows or paths that don't look like drive paths, try shlex
    try:
        parsed = shlex.split(cleaned)
        return parsed[0] if len(parsed) == 1 else ' '.join(parsed)
    except ValueError:
        # If shlex fails, return the cleaned input as-is
        return cleaned

def main():
    print("=" * 80)
    print("🧹 ORPHANED PROXY CLEANUP UTILITY")
    print("=" * 80)
    
    parser = argparse.ArgumentParser(description='Clean up orphaned proxy files')
    parser.add_argument('proxy_path', 
                        help='Path to the proxy directory')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Show what would be deleted without deleting (default: True)')
    parser.add_argument('--no-dry-run', action='store_true',
                        help='Actually delete files (turns off dry-run)')
    parser.add_argument('--force-accept', action='store_true', default=False,
                        help='Skip confirmation prompts (default: False)')
    parser.add_argument('--recursive', action='store_true', default=True,
                        help='Search sibling directories recursively (default: True)')
    
    args = parser.parse_args()
    
    # Handle dry-run logic
    if args.no_dry_run:
        dry_run = False
    else:
        dry_run = args.dry_run
    
    # Clean the path input
    proxy_path = _clean_path_input(args.proxy_path)
    
    # Validate path
    proxy_path_obj = Path(proxy_path).expanduser().resolve()
    if not proxy_path_obj.exists():
        print(f"❌ Error: Path '{proxy_path_obj}' does not exist")
        sys.exit(1)
    
    if not proxy_path_obj.is_dir():
        print(f"❌ Error: Path '{proxy_path_obj}' is not a directory")
        sys.exit(1)
    
    # Display settings
    print(f"📁 Proxy directory: {proxy_path_obj}")
    print(f"🔍 Recursive search: {'Yes' if args.recursive else 'No'}")
    print(f"👀 Dry run mode: {'Yes' if dry_run else 'No'}")
    print(f"🤖 Force accept: {'Yes' if args.force_accept else 'No'}")
    print("=" * 80)
    
    # Create and run cleanup
    cleanup = OrphanedProxyCleanup(
        proxy_path=proxy_path_obj,
        dry_run=dry_run,
        force_accept=args.force_accept,
        recursive=args.recursive
    )
    
    cleanup.run()

if __name__ == '__main__':
    main()