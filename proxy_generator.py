#!/usr/bin/env python3
import os
import sys
import subprocess
import json
import time
import platform
import argparse
import re
import shlex
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import shutil
import threading

# Fix for Windows Unicode encoding issues
if platform.system() == "Windows":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from codec_configuration import CodecConfiguration

def format_time_human(seconds):
    """Convert seconds to human-readable MM:SS format"""
    if seconds is None:
        return "N/A"
    minutes = int(seconds // 60)
    seconds_remainder = int(seconds % 60)
    return f"{minutes}:{seconds_remainder:02d}"

class ProxyGenerator:
    def __init__(self, source_path, scale="quarter", codec="prores", parallel=True, max_workers=None, shutdown=False, json_output=False, skip_existing=False):
        self.source_path = Path(source_path)
        self.scale = scale
        self.parallel = parallel
        self.max_workers = max_workers
        self.shutdown = shutdown
        self.json_output = json_output
        self.skip_existing = skip_existing
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create proxy_logs directory for logs and reports
        self.proxy_logs_dir = self.source_path.parent / "proxy_logs"
        self.proxy_logs_dir.mkdir(exist_ok=True)
        
        self.log_file = self.proxy_logs_dir / f"proxy-gen-logs-and-report-{self.timestamp}.txt"
        self.report_file = None
        self.video_extensions = {'.mp4', '.mov', '.mxf', '.avi', '.mkv'}

        self.GENERAL_PROXIES_DIR = Path("/Volumes/samsungt5-512gb-ssd-apple/video-editing/proxies-general")
        self.stats = {
            'total_files': 0,
            'transcoded': 0,
            'skipped': 0,
            'moved': 0,
            'sony_proxies_moved': 0,
            'start_time': time.time()
        }
        self.processed_files_details = []
        
        # User choice tracking for duplicate proxy handling
        self.user_choice_for_duplicates = None  # None, 'yes_to_all', 'skip_all'
        
        # Store pre-made decisions for conflicts (for parallel mode)
        self.conflict_decisions = {}  # {video_path: 'yes'|'skip'}

        # Initialize codec configuration
        self.codec_config = CodecConfiguration(codec)
        
        # Store run parameters for reporting
        self.run_params = {
            'scale': scale,
            'codec_requested': codec,
            'parallel': parallel,
            'max_workers_requested': max_workers,
            'shutdown': shutdown
        }
        
        # Thread-safe Sony proxy processing
        self.sony_proxy_lock = threading.Lock()
        self.processed_sony_proxies = set()  # Track already processed Sony proxies
        
        # Collect system information immediately
        self.system_info = self.collect_system_info()

        # Check for required tools
        self._check_requirements()

    def collect_system_info(self):
        """Collect detailed system information"""
        system_info = {
            "os": platform.system(),
            "os_version": platform.version(),
            "cpu": self._get_cpu_info(),
            "ffmpeg_version": self._get_ffmpeg_version(),
            "hw_accel_available": self.codec_config.hw_acceleration or "None",
            "hw_accel_tested": [],
            "selected_codec": self.codec_config.selected_codec,
            "available_codecs": list(self.codec_config.CODEC_PROFILES.keys())
        }
        
        # Test all hardware acceleration options and record results
        for accel in self.codec_config.HW_ACCEL_MAP.get(platform.system(), []):
            result = self.codec_config._check_ffmpeg_hw_support(accel)
            system_info["hw_accel_tested"].append({
                "accelerator": accel,
                "supported": result
            })
            
        return system_info

    def _get_cpu_info(self):
        """Get CPU information"""
        try:
            if platform.system() == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
                return winreg.QueryValueEx(key, "ProcessorNameString")[0]
            elif platform.system() == "Darwin":  # macOS
                cmd = ["sysctl", "-n", "machdep.cpu.brand_string"]
                return subprocess.check_output(cmd).decode().strip()
            else:  # Linux
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("model name"):
                            return line.split(":", 1)[1].strip()
            return "Unknown CPU"
        except Exception as e:
            return f"Could not determine CPU: {str(e)}"
            
    def _get_ffmpeg_version(self):
        """Get FFmpeg version information"""
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, check=True)
            # Extract just the first line which contains the version
            return result.stdout.split('\n')[0]
        except Exception:
            return "Unknown"

    def _get_filename_friendly_cpu(self):
        """Get a filename-friendly CPU name"""
        cpu_info = self.system_info.get('cpu', 'Unknown-CPU')
        
        # Clean up CPU name for filename use
        # Remove common words and make it more concise
        cpu_clean = cpu_info.replace('(R)', '').replace('(TM)', '').replace('  ', ' ')
        cpu_clean = cpu_clean.replace(' CPU', '').replace(' Processor', '')
        cpu_clean = cpu_clean.replace('Intel Core ', 'Intel-')
        cpu_clean = cpu_clean.replace('AMD Ryzen ', 'AMD-Ryzen-')
        cpu_clean = cpu_clean.replace('Apple ', 'Apple-')
        
        # Replace spaces and special characters with hyphens
        cpu_clean = re.sub(r'[^\w\-.]', '-', cpu_clean)
        cpu_clean = re.sub(r'-+', '-', cpu_clean)  # Remove multiple consecutive hyphens
        cpu_clean = cpu_clean.strip('-')  # Remove leading/trailing hyphens
        
        # Limit length to keep filename reasonable
        if len(cpu_clean) > 30:
            cpu_clean = cpu_clean[:30].rstrip('-')
            
        return cpu_clean

    def _check_requirements(self):
        """Check if ffmpeg and exiftool are installed"""
        def check_tool(tool):
            return shutil.which(tool) is not None

        missing_tools = []
        if not check_tool('ffmpeg'):
            missing_tools.append('ffmpeg')
        if not check_tool('exiftool'):
            missing_tools.append('exiftool')

        if missing_tools:
            print("Missing required tools:")
            for tool in missing_tools:
                print(f"- {tool}")
            print("\nInstallation instructions:")
            if platform.system() == 'Darwin':  # macOS
                print("Using Homebrew:")
                for tool in missing_tools:
                    print(f"brew install {tool}")
            elif platform.system() == 'Windows':
                print("Using Chocolatey:")
                for tool in missing_tools:
                    print(f"choco install {tool}")
            sys.exit(1)

    def _get_scaling_filter(self):
        """Get scaling filter that maintains aspect ratio"""
        if self.scale == "half":
            return "scale=iw/2:ih/2"
        return "scale=iw/4:ih/4"  # quarter

    def _is_mobile_footage(self, file_path):
        """Check if the footage is from a mobile or consumer device (phones, action cameras, etc.)"""
        try:
            result = subprocess.run(['exiftool', '-json', file_path],
                                    capture_output=True, text=True, check=True)
            metadata = json.loads(result.stdout)[0]
            
            # Check for various mobile/consumer device indicators
            mobile_indicators = [
                'AndroidVersion',        # Android phones
                'DeviceManufacturer',   # Generic device info
                'CameraModelName',      # Check for phone camera names
                'Make',                 # Camera manufacturer
                'Model'                 # Camera model
            ]
            
            # Common mobile device manufacturers and models
            mobile_keywords = [
                'android', 'samsung', 'pixel', 'oneplus', 'xiaomi', 'huawei',
                'meta', 'ray-ban', 'osmo', 'pocket', 'gopro', 'insta360', 'action',
                'phone', 'mobile', 'smartphone'
            ]
            
            # Check for direct Android indicator (original logic)
            if 'AndroidVersion' in metadata:
                return True
                
            # Check other metadata fields for mobile device indicators
            for field in mobile_indicators:
                if field in metadata:
                    value = str(metadata[field]).lower()
                    for keyword in mobile_keywords:
                        if keyword in value:
                            return True
            
            return False
        except Exception:
            return False

    def _is_mobile_folder(self, folder_path):
        """Check if the folder contains any file with 'is_mobile' in its name (case insensitive)"""
        try:
            folder = Path(folder_path)
            for item in folder.iterdir():
                if 'is_mobile' in item.name.lower():
                    return True
            return False
        except Exception:
            # If we can't read the directory for any reason, return False
            return False

    def _detect_sony_proxy_pair(self, video_path):
        """Detect if this video file has a corresponding Sony camera proxy in the same directory.
        
        Sony cameras create proxy files with the pattern:
        - Original: 20250630_ze12041.MP4
        - Proxy: 20250630_ze12041S03.MP4
        
        Returns:
            tuple: (is_original, proxy_path_if_found, original_path_if_found)
            - is_original: True if this file is the original, False if it's the proxy
            - proxy_path_if_found: Path to proxy file if found, None otherwise
            - original_path_if_found: Path to original file if found, None otherwise
        """
        video_path = Path(video_path)
        parent_dir = video_path.parent
        base_name = video_path.stem
        extension = video_path.suffix
        
        # Check if current file appears to be a Sony proxy (has suffix pattern like S03, S02, etc.)
        # Use regex to check for suffix pattern: ends with S followed by digits
        import re
        proxy_pattern = re.compile(r'S\d+$')
        
        if proxy_pattern.search(base_name):
            # This appears to be a proxy file
            # Extract the original base name by removing the S## suffix
            original_base_name = re.sub(r'S\d+$', '', base_name)
            original_path = parent_dir / f"{original_base_name}{extension}"
            
            if original_path.exists() and original_path != video_path:
                # Found the corresponding original file
                return False, video_path, original_path
            else:
                # No corresponding original found, treat as regular file
                return True, None, None
        else:
            # This appears to be an original file
            # Look for potential proxy files with S## suffix
            proxy_candidates = []
            
            # Look for files that start with the same base name and have S## suffix
            for file in parent_dir.iterdir():
                if (file.is_file() and 
                    file.suffix.lower() == extension.lower() and
                    file.stem.startswith(base_name) and
                    proxy_pattern.search(file.stem) and
                    file != video_path):
                    proxy_candidates.append(file)
            
            # Return the first valid proxy found (there might be multiple S01, S02, S03 etc.)
            for proxy_candidate in proxy_candidates:
                # Additional validation: proxy should be smaller than original
                try:
                    if proxy_candidate.stat().st_size < video_path.stat().st_size:
                        return True, proxy_candidate, video_path
                except OSError:
                    continue
            
            # No proxy found
            return True, None, None

    def _find_sony_proxy_in_proxies_folder(self, video_path, proxies_dir):
        """Check if a Sony proxy file for this video already exists in the proxies folder.

        This handles the case where a Sony proxy was manually copied to the proxies folder
        before running the script. The Sony proxy will have the S## suffix pattern.

        Args:
            video_path: Path to the original video file
            proxies_dir: Path to the proxies directory

        Returns:
            Path to Sony proxy file if found, None otherwise
        """
        if not proxies_dir.exists():
            return None

        video_path = Path(video_path)
        base_name = video_path.stem
        extension = video_path.suffix

        # Look for Sony proxy pattern: {base_name}S##.{extension}
        import re
        proxy_pattern = re.compile(r'S\d+$')

        sony_proxy_candidates = []

        for file in proxies_dir.iterdir():
            if (file.is_file() and
                file.suffix.lower() == extension.lower() and
                file.stem.startswith(base_name) and
                proxy_pattern.search(file.stem) and
                file != video_path):
                sony_proxy_candidates.append(file)

        # Validate and return the first valid Sony proxy
        for candidate in sony_proxy_candidates:
            try:
                # Verify it's smaller than the original (should be a proxy)
                if candidate.stat().st_size < video_path.stat().st_size:
                    return candidate
            except OSError:
                continue

        return None

    def _find_proxy_in_general_folder(self, video_path):
        """Check centralized proxies folder for existing proxy.

        Matches:
        - Standard format: <basename>_proxy.<ext>
        - Sony format: <basename>S##.<ext> (e.g., 20260115_ze12266S03.MP4)

        Returns:
            Path to proxy file if found and valid, None otherwise
        """
        if not self.GENERAL_PROXIES_DIR.exists():
            return None

        video_path = Path(video_path)
        base_name = video_path.stem.lower()

        for file in self.GENERAL_PROXIES_DIR.iterdir():
            if not file.is_file():
                continue

            file_stem_lower = file.stem.lower()

            # Check standard proxy naming: basename_proxy
            if file_stem_lower == f"{base_name}_proxy":
                if self._is_proxy_valid(file):
                    return file

            # Check Sony proxy naming: basenameS## (e.g., 20260115_ze12266S03)
            sony_pattern = re.compile(rf'^{re.escape(base_name)}s\d+$', re.IGNORECASE)
            if sony_pattern.match(file_stem_lower):
                if self._is_proxy_valid(file):
                    return file

        return None

    def _is_proxy_valid(self, proxy_path):
        """Check if existing proxy is valid"""
        self._log(f"Validating proxy file: {proxy_path}")
        try:
            # Use a more detailed ffprobe to verify the file is fully valid
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
                self._log(f"Proxy validation successful: {proxy_path}")
                return True
            else:
                self._log(f"Proxy has no valid video streams: {proxy_path}")
                return False

        except subprocess.CalledProcessError as e:
            self._log(f"Proxy validation failed: {proxy_path}\nError: {e.stderr}")
            return False
        except json.JSONDecodeError:
            self._log(f"Proxy validation failed: Invalid JSON from ffprobe for {proxy_path}")
            return False
        except Exception as e:
            self._log(f"Proxy validation failed with unexpected error: {proxy_path}\nError: {str(e)}")
            return False

    def _get_file_size(self, path):
        """Get file size in MB"""
        return os.path.getsize(path) / (1024 * 1024)

    def _get_proxies_dir(self, video_path):
        """Get the parent proxies directory"""
        # Determine the root directory
        if self.source_path.is_dir():
            root_dir = self.source_path
        else:
            # For single file processing, use the parent directory
            root_dir = video_path.parent

        # Create the parent proxies directory
        proxies_dir = root_dir.parent / 'proxies'
        proxies_dir.mkdir(exist_ok=True)
        return proxies_dir

    def _find_existing_proxy_with_different_extension(self, video_path, expected_proxy_path):
        """Find existing proxy files with different extensions in the parent proxies directory"""
        proxies_dir = expected_proxy_path.parent
        base_name = f"{video_path.stem}_proxy"
        
        # Look for any proxy with the same base name but different extension
        for file in proxies_dir.iterdir():
            if (file.is_file() and 
                file.stem.lower() == base_name.lower() and 
                file != expected_proxy_path and
                self._is_proxy_valid(file)):
                return file
        return None

    def _prompt_user_for_duplicate_proxy(self, video_path, existing_proxy_path, new_proxy_path):
        """Prompt user when a proxy with different extension exists"""
        # Auto-skip if flag is enabled
        if self.skip_existing:
            return 'skip'

        # Check if we have a pre-made decision (for parallel mode)
        if str(video_path) in self.conflict_decisions:
            return self.conflict_decisions[str(video_path)]

        # Check if user has already made a global choice
        if self.user_choice_for_duplicates == 'yes_to_all':
            return 'yes'
        elif self.user_choice_for_duplicates == 'skip_all':
            return 'skip'
        
        # Prompt user for this specific file
        existing_ext = existing_proxy_path.suffix
        new_ext = new_proxy_path.suffix
        
        print(f"\nProxy already exists for '{video_path.name}':")
        print(f"  Existing: {existing_proxy_path.name} ({existing_ext})")
        print(f"  New:      {new_proxy_path.name} ({new_ext})")
        print("\nOptions:")
        print("  y/yes     - Create duplicate proxy with new codec")
        print("  s/skip    - Skip this file")
        print("  ya/yes-all - Create duplicates for all remaining files")
        print("  sa/skip-all - Skip all remaining files with existing proxies")
        
        while True:
            choice = input("Choice [y/s/ya/sa]: ").lower().strip()
            
            if choice in ['y', 'yes']:
                return 'yes'
            elif choice in ['s', 'skip']:
                return 'skip'
            elif choice in ['ya', 'yes-all']:
                self.user_choice_for_duplicates = 'yes_to_all'
                return 'yes'
            elif choice in ['sa', 'skip-all']:
                self.user_choice_for_duplicates = 'skip_all'
                return 'skip'
            else:
                print("Invalid choice. Please enter 'y', 's', 'ya', or 'sa'.")

    def _scan_for_conflicts(self, video_files):
        """Scan for duplicate proxy conflicts before processing starts"""
        conflicts = []
        
        for video_path in video_files:
            video_path = Path(video_path)
            
            # Get the parent proxies directory
            proxies_dir = self._get_proxies_dir(video_path)
            
            # Determine if this is mobile/consumer footage - either by metadata or folder indicator
            is_mobile_metadata = self._is_mobile_footage(video_path)
            is_mobile_folder = self._is_mobile_folder(video_path.parent)
            is_mobile = is_mobile_metadata or is_mobile_folder
            
            selected_codec = "h264" if is_mobile else self.codec_config.selected_codec
            
            if selected_codec in ['prores', 'dnxhr']:
                output_extension = '.mov'
            else:
                output_extension = '.mp4'
            
            proxy_name = f"{video_path.stem}_proxy{output_extension}"
            proxy_path = proxies_dir / proxy_name
            
            # Skip if exact proxy already exists
            if proxy_path.exists() and self._is_proxy_valid(proxy_path):
                continue

            # Skip if proxy exists in centralized general proxies folder
            if self._find_proxy_in_general_folder(video_path):
                continue

            # Check for existing proxy with different extension
            existing_different_proxy = self._find_existing_proxy_with_different_extension(video_path, proxy_path)
            if existing_different_proxy:
                conflicts.append({
                    'video_path': video_path,
                    'existing_proxy': existing_different_proxy,
                    'new_proxy': proxy_path
                })
        
        return conflicts

    def _resolve_conflicts_upfront(self, conflicts):
        """Resolve all conflicts with user input before processing starts"""
        if not conflicts:
            return

        # Auto-skip all conflicts if flag is enabled
        if self.skip_existing:
            for conflict in conflicts:
                self.conflict_decisions[str(conflict['video_path'])] = 'skip'
            print(f"\n⏭️  Auto-skipping {len(conflicts)} videos with existing proxies (--skip-existing enabled)")
            return

        print(f"\n🎯 Found {len(conflicts)} proxy conflicts that need your decision:")
        print("=" * 60)
        
        for i, conflict in enumerate(conflicts, 1):
            video_path = conflict['video_path']
            existing_proxy = conflict['existing_proxy']
            new_proxy = conflict['new_proxy']
            
            print(f"\nConflict {i}/{len(conflicts)}:")
            choice = self._prompt_user_for_duplicate_proxy(video_path, existing_proxy, new_proxy)
            
            # Store the decision
            self.conflict_decisions[str(video_path)] = choice
            
            # If user chose "yes-all" or "skip-all", apply to remaining conflicts
            if self.user_choice_for_duplicates == 'yes_to_all':
                for remaining_conflict in conflicts[i:]:
                    self.conflict_decisions[str(remaining_conflict['video_path'])] = 'yes'
                break
            elif self.user_choice_for_duplicates == 'skip_all':
                for remaining_conflict in conflicts[i:]:
                    self.conflict_decisions[str(remaining_conflict['video_path'])] = 'skip'
                break
        
        print("\n✅ All conflicts resolved! Starting processing...")

    def _process_file(self, video_path):
        """Process a single video file"""
        video_path = Path(video_path)
        self._log(f"Processing file: {video_path}")

        # Check if file still exists (may have been moved by another thread in parallel processing)
        if not video_path.exists():
            self._log(f"File no longer exists (likely moved by another thread): {video_path.name}")
            return

        # Check for Sony camera proxy pairs first
        is_original, sony_proxy_path, sony_original_path = self._detect_sony_proxy_pair(video_path)
        
        # Create a details dictionary for this file
        file_details = {
            "filename": str(video_path),
            "size_mb": self._get_file_size(video_path),
            "processing_start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "pending",
            "codec_decision": {},
            "processing_time_seconds": 0,
            "sony_proxy_detected": sony_proxy_path is not None
        }

        # Handle Sony proxy optimization
        if sony_proxy_path and is_original:
            # This is an original file with a Sony proxy - use the existing proxy
            self._log(f"📷 SONY PROXY DETECTED: {video_path.name}")
            self._log(f"   Original: {sony_original_path.name} ({self._get_file_size(sony_original_path):.1f}MB)")
            self._log(f"   Sony proxy: {sony_proxy_path.name} ({self._get_file_size(sony_proxy_path):.1f}MB)")
            
            # Thread-safe Sony proxy processing
            with self.sony_proxy_lock:
                sony_proxy_str = str(sony_proxy_path)
                
                # Check if this Sony proxy was already processed by another thread
                if sony_proxy_str in self.processed_sony_proxies:
                    self._log(f"Sony proxy already processed by another thread: {sony_proxy_path.name}")
                    self.stats['skipped'] += 1
                    file_details["result"] = "skipped"
                    file_details["skip_reason"] = "Sony proxy already processed by another thread"
                    file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.processed_files_details.append(file_details)
                    return
                
                # Mark this Sony proxy as being processed
                self.processed_sony_proxies.add(sony_proxy_str)
            
            # Get the parent proxies directory
            proxies_dir = self._get_proxies_dir(video_path)
            
            # Generate the target proxy name (Premiere Pro compatible)
            proxy_name = f"{video_path.stem}_proxy{video_path.suffix}"
            target_proxy_path = proxies_dir / proxy_name
            
            try:
                # Check if target already exists
                if target_proxy_path.exists():
                    self._log(f"Target proxy already exists: {target_proxy_path}")
                    self.stats['skipped'] += 1
                    file_details["result"] = "skipped"
                    file_details["skip_reason"] = "Sony proxy already moved to target location"
                elif not sony_proxy_path.exists():
                    # Sony proxy was already moved by another thread
                    self._log(f"Sony proxy already moved by another thread: {sony_proxy_path.name}")
                    self.stats['skipped'] += 1
                    file_details["result"] = "skipped"
                    file_details["skip_reason"] = "Sony proxy already moved by another thread"
                else:
                    # Safer copy-then-delete approach for Sony proxies
                    shutil.copy2(sony_proxy_path, target_proxy_path)  # Copy with metadata
                    
                    # Verify the copy was successful before deleting
                    if target_proxy_path.exists() and target_proxy_path.stat().st_size > 0:
                        sony_proxy_path.unlink()  # Delete the original
                        self._log(f"✅ SONY PROXY MOVED: {sony_proxy_path.name} → {target_proxy_path.name}")
                        self._log(f"   Location: {proxies_dir}")
                        
                        self.stats['moved'] += 1
                        self.stats['sony_proxies_moved'] += 1
                        file_details["result"] = "sony_proxy_moved"
                        file_details["sony_proxy_source"] = str(sony_proxy_path)
                        file_details["sony_proxy_target"] = str(target_proxy_path)
                        file_details["sony_proxy_size_mb"] = self._get_file_size(target_proxy_path)
                    else:
                        # Copy failed, clean up and log error
                        if target_proxy_path.exists():
                            target_proxy_path.unlink()  # Remove incomplete copy
                        raise Exception(f"Copy verification failed - target file is missing or empty")
                    
            except Exception as e:
                self._log(f"❌ Error moving Sony proxy: {str(e)}")
                file_details["result"] = "error"
                file_details["error"] = f"Error moving Sony proxy: {str(e)}"
            
            file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.processed_files_details.append(file_details)
            return
            
        elif not is_original:
            # This is a Sony proxy file itself - skip it since we'll handle it via the original
            self._log(f"📷 SKIPPING SONY PROXY FILE: {video_path.name} (will be handled via original)")
            self.stats['skipped'] += 1
            file_details["result"] = "skipped"
            file_details["skip_reason"] = "Sony proxy file - handled via original file"
            file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.processed_files_details.append(file_details)
            return

        # Check centralized general proxies folder first
        general_proxy = self._find_proxy_in_general_folder(video_path)
        if general_proxy:
            proxies_dir = self._get_proxies_dir(video_path)

            # Determine target filename - rename Sony format to standard _proxy format
            if re.match(rf'^{re.escape(video_path.stem)}s\d+$', general_proxy.stem, re.IGNORECASE):
                # Sony format proxy - rename to standard format
                target_name = f"{video_path.stem}_proxy{general_proxy.suffix}"
            else:
                # Already standard format
                target_name = general_proxy.name

            target_path = proxies_dir / target_name

            # Check if target already exists
            if target_path.exists() and self._is_proxy_valid(target_path):
                self._log(f"Proxy already exists at target: {target_path}")
                self.stats['skipped'] += 1
                file_details["result"] = "skipped"
                file_details["skip_reason"] = "Proxy already exists at target location"
            else:
                try:
                    self._log(f"📦 GENERAL FOLDER PROXY FOUND: {general_proxy.name}")
                    self._log(f"   Moving: {general_proxy} -> {target_path}")
                    shutil.move(str(general_proxy), str(target_path))
                    self.stats['moved'] += 1
                    file_details["result"] = "moved"
                    file_details["moved_from"] = str(general_proxy)
                    file_details["moved_to"] = str(target_path)
                    self._log(f"✅ PROXY MOVED FROM GENERAL FOLDER: {target_path.name}")
                except Exception as e:
                    self._log(f"❌ Error moving proxy from general folder: {e}")
                    file_details["result"] = "error"
                    file_details["error"] = str(e)

            file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.processed_files_details.append(file_details)
            return

        # Get the parent proxies directory
        proxies_dir = self._get_proxies_dir(video_path)
        self._log(f"Using parent proxies directory: {proxies_dir}")

        # Determine if this is mobile/consumer footage - either by metadata or folder indicator
        is_mobile_metadata = self._is_mobile_footage(video_path)
        is_mobile_folder = self._is_mobile_folder(video_path.parent)
        is_mobile = is_mobile_metadata or is_mobile_folder
        
        file_details["is_mobile_footage"] = is_mobile
        file_details["mobile_detection_method"] = "metadata" if is_mobile_metadata else ("folder indicator" if is_mobile_folder else "none")

        # Get codec extension based on selection (or mobile footage override)
        selected_codec = "h264" if is_mobile else self.codec_config.selected_codec
        file_details["codec_decision"]["requested_codec"] = self.codec_config.selected_codec
        file_details["codec_decision"]["actual_codec"] = selected_codec
        
        if is_mobile:
            detection_method = "metadata" if is_mobile_metadata else "folder indicator"
            reason = f"Mobile footage detected via {detection_method}, using H.264"
            file_details["codec_decision"]["reason"] = reason
            
            # Log the codec override to console/log file
            if self.codec_config.selected_codec != "h264":
                self._log(f"📱 CODEC OVERRIDE: {video_path.name}")
                self._log(f"   Original codec: {self.codec_config.selected_codec.upper()}")
                self._log(f"   Override reason: Mobile footage detected via {detection_method}")
                self._log(f"   Using codec: H.264 (prevents VFR stuttering)")
            else:
                self._log(f"📱 Mobile footage detected via {detection_method} (H.264 already selected)")
        else:
            file_details["codec_decision"]["reason"] = "Using user-selected codec"

        if selected_codec in ['prores', 'dnxhr']:
            output_extension = '.mov'
        else:
            output_extension = '.mp4'

        file_details["output_extension"] = output_extension
        file_details["hw_acceleration"] = self.codec_config.hw_acceleration or "None"

        proxy_name = f"{video_path.stem}_proxy{output_extension}"
        proxy_path = proxies_dir / proxy_name

        # Check if a Sony proxy was manually copied to the proxies folder
        sony_proxy_in_proxies = self._find_sony_proxy_in_proxies_folder(video_path, proxies_dir)
        if sony_proxy_in_proxies:
            # Check if target path already exists
            if proxy_path.exists():
                self._log(f"📷 SONY PROXY FOUND: {sony_proxy_in_proxies.name}")
                self._log(f"   Target already exists: {proxy_path.name}")
                self._log(f"   Skipping rename, will use existing proxy")
                # Let the normal flow handle the existing proxy
            else:
                try:
                    self._log(f"📷 SONY PROXY FOUND IN PROXIES FOLDER: {sony_proxy_in_proxies.name}")
                    self._log(f"   Renaming to standard format: {proxy_path.name}")

                    # Rename the Sony proxy to the standard naming convention
                    sony_proxy_in_proxies.rename(proxy_path)

                    self._log(f"✅ SONY PROXY RENAMED: {sony_proxy_in_proxies.name} → {proxy_path.name}")
                    self._log(f"   Location: {proxies_dir}")

                    self.stats['moved'] += 1
                    self.stats['sony_proxies_moved'] += 1
                    file_details["result"] = "sony_proxy_renamed"
                    file_details["sony_proxy_source"] = str(sony_proxy_in_proxies)
                    file_details["sony_proxy_target"] = str(proxy_path)
                    file_details["sony_proxy_size_mb"] = self._get_file_size(proxy_path)
                    file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.processed_files_details.append(file_details)
                    return

                except Exception as e:
                    self._log(f"❌ Error renaming Sony proxy: {str(e)}")
                    self._log(f"   Will proceed with normal flow")
                    # Continue with normal flow if rename fails

        # First, check if the proxy already exists in the parent proxies directory
        if proxy_path.exists() and self._is_proxy_valid(proxy_path):
            self.stats['skipped'] += 1
            self._log(f"Skipped {video_path.name} - valid proxy already exists in parent proxies directory")
            file_details["result"] = "skipped"
            file_details["skip_reason"] = "Valid proxy already exists"
            self.processed_files_details.append(file_details)
            return

        # Check for existing proxy with different extension in parent proxies directory
        existing_different_proxy = self._find_existing_proxy_with_different_extension(video_path, proxy_path)
        if existing_different_proxy:
            user_choice = self._prompt_user_for_duplicate_proxy(video_path, existing_different_proxy, proxy_path)

            if user_choice == 'skip':
                self.stats['skipped'] += 1
                skip_reason = "Auto-skipped - existing proxy" if self.skip_existing else "User chose to skip due to existing proxy with different extension"
                self._log(f"Skipped {video_path.name} - {skip_reason}")
                file_details["result"] = "skipped"
                file_details["skip_reason"] = f"{skip_reason}: {existing_different_proxy.name}"
                self.processed_files_details.append(file_details)
                return
            else:
                # User chose to create duplicate, continue with processing
                self._log(f"Creating duplicate proxy for {video_path.name} - existing: {existing_different_proxy.name}, new: {proxy_path.name}")
                file_details["duplicate_created"] = True
                file_details["existing_proxy"] = str(existing_different_proxy)

        # Check for existing proxies in the old Proxies subdirectory
        old_proxies_dir = video_path.parent / 'Proxies'
        old_proxy_path = None

        if old_proxies_dir.exists():
            self._log(f"Checking for proxy in old Proxies folder: {video_path.stem}_Proxy (any extension)")
            for file in old_proxies_dir.iterdir():
                if file.is_file() and file.stem.lower() == f"{video_path.stem.lower()}_proxy":
                    self._log(f"Found potential proxy in old Proxies folder: {file}")
                    if self._is_proxy_valid(file):
                        old_proxy_path = file
                        break

        # Check for existing proxies in the same directory
        if not old_proxy_path:
            parent_dir = video_path.parent
            base_filename = video_path.stem.lower()
            self._log(f"Checking for proxies in same directory for: {base_filename}")

            for file in parent_dir.iterdir():
                if file.is_file():
                    file_stem_lower = file.stem.lower()
                    if "_proxy" in file_stem_lower and base_filename in file_stem_lower:
                        self._log(f"Found potential proxy in same directory: {file}")
                        if self._is_proxy_valid(file):
                            old_proxy_path = file
                            break

        # If proxy exists elsewhere, move it to the parent proxies directory
        if old_proxy_path:
            try:
                # Check for name collision
                if proxy_path.exists():
                    self._log(f"Error: Name collision detected. File already exists at: {proxy_path}")
                    file_details["result"] = "error"
                    file_details["error"] = f"Name collision detected at {proxy_path}"
                    self.processed_files_details.append(file_details)
                    return

                # Move the file
                shutil.move(old_proxy_path, proxy_path)
                self._log(f"Moved existing proxy: {old_proxy_path} -> {proxy_path}")
                self.stats['moved'] += 1
                file_details["result"] = "moved"
                file_details["moved_from"] = str(old_proxy_path)
                self.processed_files_details.append(file_details)
                return
            except Exception as e:
                self._log(f"Error moving proxy file: {str(e)}")
                file_details["result"] = "error"
                file_details["error"] = f"Error moving proxy file: {str(e)}"
                # Continue with transcoding if move fails

        # No valid proxies found, proceed with transcoding
        if file_details["result"] == "pending":  # Still need to transcode
            self._log(f"No valid proxies found, proceeding with transcoding for: {video_path}")

            # Get audio codec information for smart audio handling
            audio_info = self._get_audio_codec_info(video_path)
            should_copy_audio, audio_reason = self._should_copy_audio(audio_info)
            
            self._log(f"Audio analysis: {audio_reason}")
            file_details["audio_info"] = audio_info
            file_details["audio_decision"] = {
                "should_copy": should_copy_audio,
                "reason": audio_reason
            }

            scaling = self._get_scaling_filter()

            # Check if source is 10-bit HEVC to apply special handling
            source_info = self.codec_config._get_source_video_info(str(video_path))
            is_hevc_10bit = self.codec_config._is_hevc_10bit(source_info)
            
            # Get hardware acceleration and codec configuration
            if is_hevc_10bit and self.codec_config.hw_acceleration == 'cuda':
                # Use special configuration for 10-bit HEVC sources
                config = self.codec_config.get_hevc_10bit_codec_config(is_mobile)
                self._log(f"🎬 10-bit HEVC source detected: Using special H.264 encoding with CPU scaling")
                self._log(f"   - Source format: {source_info.get('codec_name', 'unknown')} {source_info.get('profile', 'unknown')} {source_info.get('pix_fmt', 'unknown')}")
                self._log(f"   - Target codec: H.264 (forced for compatibility)")
                
                # Override the output extension to .mp4 for H.264 encoding
                output_extension = '.mp4'
                proxy_name = f"{video_path.stem}_proxy{output_extension}"
                proxy_path = proxies_dir / proxy_name
                file_details["output_extension"] = output_extension
                file_details["codec_decision"]["hevc_10bit_override"] = True
                file_details["codec_decision"]["reason"] += " (10-bit HEVC → H.264 conversion)"
            else:
                # Use normal configuration
                config = self.codec_config.get_configuration(is_mobile)
            
            # Build video filter chain with GPU-accelerated scaling for CUDA
            video_filter, fallback_reason = self.codec_config.build_video_filter(
                scaling, 
                config.get('needs_format_conversion', False),
                video_path=str(video_path),
                target_codec=selected_codec
            )
            
            # Log CUDA optimizations or fallback reasons
            if fallback_reason:
                self._log(f"⚠️  CUDA Fallback Applied: {fallback_reason}")
                self._log(f"   - Video filter: {video_filter}")
                if is_hevc_10bit:
                    self._log(f"   - Using H.264 encoding and CPU scaling for 10-bit HEVC compatibility")
            elif self.codec_config.hw_acceleration == 'cuda' and selected_codec in ['h264', 'hevc']:
                self._log(f"🚀 GPU Acceleration Optimized: Using scale_cuda and hwaccel_output_format for maximum performance")
                self._log(f"   - GPU scaling: {video_filter}")
                self._log(f"   - Hardware acceleration: {config['hw_accel_args']}")
                self._log(f"   - Eliminates GPU↔CPU memory transfers")
            elif config.get('needs_format_conversion', False):
                self._log(f"Adding format=yuv420p filter for hardware acceleration")
            
            file_details["codec_config"] = {
                "hw_accel_args": config['hw_accel_args'],
                "codec_args": config['codec_args'],
                "video_filter": video_filter,
                "needs_format_conversion": config.get('needs_format_conversion', False)
            }

            # Build ffmpeg command
            cmd = ['ffmpeg', '-hide_banner', '-y']
            cmd.extend(config['hw_accel_args'])
            cmd.extend(['-i', str(video_path)])
            cmd.extend(['-vf', video_filter])
            cmd.extend(config['codec_args'])
            
            # Smart audio handling
            if should_copy_audio:
                cmd.extend(['-c:a', 'copy'])
            else:
                if audio_info.get('has_audio', False):
                    cmd.extend(['-c:a', 'aac', '-b:a', '128k'])
                else:
                    cmd.extend(['-an'])  # No audio
            
            cmd.append(str(proxy_path))

            # Execute transcoding
            start_time = time.time()
            try:
                self._log(f"Running command: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                duration = time.time() - start_time

                # Log success
                proxy_size = self._get_file_size(proxy_path)
                human_duration = format_time_human(duration)

                self._log(
                    f"Transcoded: {video_path.name}\n"
                    f"Time: {duration:.2f} seconds ({human_duration})\n"
                    f"Original size: {file_details['size_mb']:.2f}MB\n"
                    f"Proxy size: {proxy_size:.2f}MB\n"
                    f"Command: {' '.join(cmd)}\n"
                )
                self.stats['transcoded'] += 1

                file_details["result"] = "transcoded"
                file_details["processing_time_seconds"] = duration
                file_details["proxy_size_mb"] = proxy_size
                file_details["compression_ratio"] = file_details['size_mb'] / proxy_size if proxy_size > 0 else "N/A"

            except subprocess.CalledProcessError as e:
                self._log(f"Error transcoding {video_path.name}:\n{e.stderr}")
                file_details["result"] = "error"
                file_details["error"] = e.stderr

        file_details["processing_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.processed_files_details.append(file_details)

    def _log(self, message):
        """Write to log file and print to console"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] {message}\n"
        print(log_message, end='')
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(log_message)

    def process_directory(self):
        """Process all video files in the directory"""
        if not self.source_path.is_dir():
            self._log(f"Error: '{self.source_path}' is not a directory")
            return

        video_files = []
        for root, _, files in os.walk(self.source_path):
            # Skip Proxies directories
            if Path(root).name.lower() == "proxies":
                continue
            for file in files:
                file_path = Path(file)
                # Skip files if extension not in video_extensions or if filename contains 'proxy'
                if (file_path.suffix.lower() in self.video_extensions and
                        'proxy' not in file_path.stem.lower()):
                    video_files.append(Path(root) / file)

        self.stats['total_files'] = len(video_files)
        self._log(f"Found {len(video_files)} video files")

        # Scan for conflicts before processing
        conflicts = self._scan_for_conflicts(video_files)
        
        # Resolve conflicts upfront
        self._resolve_conflicts_upfront(conflicts)

        if self.parallel:
            # Use physical CPU cores count, max 8 concurrent processes
            default_workers = min(os.cpu_count() // 2 or 1, 8)
            max_workers = self.max_workers or default_workers
            self._log(f"Running with {max_workers} concurrent processes")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(self._process_file, video_files))
        else:
            for video_file in video_files:
                self._process_file(video_file)

        self._print_final_stats()

    def process_single_file(self):
        """Process a single video file"""
        if not self.source_path.is_file():
            self._log(f"Error: '{self.source_path}' is not a file")
            return

        if self.source_path.suffix.lower() not in self.video_extensions:
            self._log(f"Error: '{self.source_path}' is not a supported video file")
            return

        if 'proxy' in self.source_path.stem.lower():
            self._log(f"Error: '{self.source_path}' appears to be a proxy file")
            return

        self.stats['total_files'] = 1
        
        # Scan for conflicts before processing (even for single file)
        conflicts = self._scan_for_conflicts([self.source_path])
        
        # Resolve conflicts upfront
        self._resolve_conflicts_upfront(conflicts)
        
        self._process_file(self.source_path)
        self._print_final_stats()

    def _shutdown_system(self):
        """Initiate system shutdown with countdown"""
        print("\nInitiating shutdown in 10 seconds...")
        print("Press Ctrl+C to abort shutdown")

        try:
            for i in range(10, 0, -1):
                sys.stdout.write(f"\rShutting down in {i} seconds...")
                sys.stdout.flush()
                time.sleep(1)

            # Execute shutdown command based on OS
            if platform.system() == 'Windows':
                subprocess.run(['shutdown', '/s', '/t', '0'])
            elif platform.system() == 'Darwin':  # macOS
                subprocess.run(['sudo', 'shutdown', '-h', 'now'])
            else:  # Linux
                subprocess.run(['sudo', 'shutdown', '-h', 'now'])

        except KeyboardInterrupt:
            print("\nShutdown aborted!")

    def _generate_detailed_report(self):
        """Generate a detailed report of all processed files and system information"""
        # Generate descriptive filename
        total_time = time.time() - self.stats['start_time']
        cpu_name = self._get_filename_friendly_cpu()
        codec = self.run_params['codec_requested']
        processing_mode = "parallel" if self.run_params['parallel'] else "single"
        
        # Format time in a filename-friendly way
        time_str = f"{int(total_time//60)}m{int(total_time%60)}s"
        
        # Create descriptive filename
        filename_parts = [
            "proxy-report",
            cpu_name,
            codec,
            time_str,
            processing_mode,
            self.timestamp
        ]
        
        descriptive_filename = "_".join(filename_parts) + ".txt"
        self.report_file = self.proxy_logs_dir / descriptive_filename
        
        with open(self.report_file, 'w', encoding='utf-8') as f:
            # System Information Section
            f.write("=" * 80 + "\n")
            f.write("SYSTEM INFORMATION\n")
            f.write("=" * 80 + "\n\n")
            
            f.write(f"Operating System: {self.system_info['os']} {self.system_info['os_version']}\n")
            f.write(f"CPU: {self.system_info['cpu']}\n")
            f.write(f"FFmpeg Version: {self.system_info['ffmpeg_version']}\n\n")
            
            f.write("Hardware Acceleration:\n")
            f.write(f"  Selected: {self.system_info['hw_accel_available']}\n")
            f.write("  Tested Accelerators:\n")
            for accel in self.system_info['hw_accel_tested']:
                f.write(f"    - {accel['accelerator']}: {'Supported' if accel['supported'] else 'Not Supported'}\n")
            
            f.write("\nCodec Information:\n")
            f.write(f"  Selected Codec: {self.system_info['selected_codec']}\n")
            f.write(f"  Available Codecs: {', '.join(self.system_info['available_codecs'])}\n")
            
            # Run Parameters Section
            f.write("\n" + "=" * 80 + "\n")
            f.write("RUN PARAMETERS\n")
            f.write("=" * 80 + "\n\n")
            
            f.write(f"Scale: {self.run_params['scale']}\n")
            f.write(f"Codec Requested: {self.run_params['codec_requested']}\n")
            f.write(f"Parallel Processing: {'Yes' if self.run_params['parallel'] else 'No'}\n")
            
            if self.run_params['parallel']:
                # Get actual workers used
                actual_workers = self.max_workers or min(os.cpu_count() // 2 or 1, 8)
                f.write(f"Max Workers Requested: {self.run_params['max_workers_requested'] or 'Auto'}\n")
                f.write(f"Max Workers Actually Used: {actual_workers}\n")
                f.write(f"Available CPU Cores: {os.cpu_count()}\n")
            else:
                f.write("Max Workers: N/A (Single-threaded)\n")
                
            f.write(f"Shutdown After Completion: {'Yes' if self.run_params['shutdown'] else 'No'}\n")
            
            # Processing Statistics
            f.write("\n" + "=" * 80 + "\n")
            f.write("PROCESSING STATISTICS\n")
            f.write("=" * 80 + "\n\n")
            
            total_time = time.time() - self.stats['start_time']
            human_time = format_time_human(total_time)
            f.write(f"Total Processing Time: {total_time:.2f} seconds ({human_time})\n")
            f.write(f"Total Files Found: {self.stats['total_files']}\n")
            f.write(f"Files Transcoded: {self.stats['transcoded']}\n")
            f.write(f"Files Skipped: {self.stats['skipped']}\n")
            f.write(f"Proxies Moved: {self.stats['moved']}\n")
            if self.stats['sony_proxies_moved'] > 0:
                f.write(f"Sony Camera Proxies Moved: {self.stats['sony_proxies_moved']}\n")
            f.write("\n")
            
            # File Details Section
            f.write("\n" + "=" * 80 + "\n")
            f.write("PER-FILE DETAILS\n")
            f.write("=" * 80 + "\n\n")
            
            for idx, file_details in enumerate(self.processed_files_details, 1):
                f.write(f"File {idx}: {file_details['filename']}\n")
                f.write("-" * 80 + "\n")
                f.write(f"Size: {file_details['size_mb']:.2f} MB\n")
                f.write(f"Mobile Footage: {'Yes' if file_details.get('is_mobile_footage', False) else 'No'}\n")
                f.write(f"Result: {file_details['result']}\n")
                
                if file_details['result'] == 'skipped':
                    f.write(f"Skip Reason: {file_details.get('skip_reason', 'Unknown')}\n")
                
                if file_details['result'] == 'transcoded':
                    processing_time = file_details['processing_time_seconds']
                    human_processing_time = format_time_human(processing_time)
                    f.write(f"Processing Time: {processing_time:.2f} seconds ({human_processing_time})\n")
                    f.write(f"Proxy Size: {file_details.get('proxy_size_mb', 'N/A'):.2f} MB\n")
                    f.write(f"Compression Ratio: {file_details.get('compression_ratio', 'N/A'):.2f}x\n")
                
                # Duplicate proxy information
                if file_details.get('duplicate_created', False):
                    f.write(f"Duplicate Proxy: Yes (existing proxy: {file_details.get('existing_proxy', 'Unknown')})\n")
                
                # Codec Decision Details
                f.write("\nCodec Decision:\n")
                f.write(f"  Requested Codec: {file_details['codec_decision'].get('requested_codec', 'Unknown')}\n")
                f.write(f"  Actual Codec Used: {file_details['codec_decision'].get('actual_codec', 'Unknown')}\n")
                f.write(f"  Reason: {file_details['codec_decision'].get('reason', 'Unknown')}\n")
                f.write(f"  Output Extension: {file_details.get('output_extension', 'Unknown')}\n")
                f.write(f"  Hardware Acceleration: {file_details.get('hw_acceleration', 'None')}\n")
                
                # Audio Decision Details
                if 'audio_decision' in file_details:
                    f.write("\nAudio Processing:\n")
                    audio_info = file_details.get('audio_info', {})
                    f.write(f"  Has Audio: {'Yes' if audio_info.get('has_audio', False) else 'No'}\n")
                    if audio_info.get('has_audio', False):
                        f.write(f"  Source Codec: {audio_info.get('codec_name', 'Unknown')}\n")
                        f.write(f"  Source Bitrate: {audio_info.get('bit_rate', 'Unknown')}\n")
                        f.write(f"  Processing: {file_details['audio_decision'].get('reason', 'Unknown')}\n")
                
                if 'codec_config' in file_details:
                    f.write("\nCodec Configuration:\n")
                    f.write(f"  Hardware Acceleration Args: {' '.join(file_details['codec_config'].get('hw_accel_args', []))}\n")
                    f.write(f"  Codec Args: {' '.join(file_details['codec_config'].get('codec_args', []))}\n")
                    f.write(f"  Video Filter: {file_details['codec_config'].get('video_filter', 'Unknown')}\n")
                    f.write(f"  Format Conversion (10->8 bit): {'Yes' if file_details['codec_config'].get('needs_format_conversion', False) else 'No'}\n")
                
                if file_details['result'] == 'error':
                    f.write("\nError Information:\n")
                    f.write(f"{file_details.get('error', 'Unknown error')}\n")
                
                f.write("\n")  # Extra space between files
                
            f.write("\n" + "=" * 80 + "\n")
            f.write("REPORT GENERATED: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("=" * 80 + "\n")
            
        return self.report_file

    def _generate_benchmark_json(self):
        """Generate JSON output for benchmarking"""
        total_time = time.time() - self.stats['start_time']
        human_time = format_time_human(total_time)
        
        # Determine actual workers used
        if self.parallel:
            actual_workers = self.max_workers or min(os.cpu_count() // 2 or 1, 8)
        else:
            actual_workers = 1
        
        benchmark_data = {
            "completion_time_seconds": round(total_time, 2),
            "completion_time_human": human_time,
            "configuration": {
                "codec": self.codec_config.selected_codec,
                "parallel": self.parallel,
                "max_workers": actual_workers,
                "scale": self.scale,
                "input_path": str(self.source_path),
                "hardware_acceleration": self.codec_config.hw_acceleration or "none"
            },
            "system_info": {
                "cpu": self.system_info['cpu'],
                "os": self.system_info['os'],
                "available_cores": os.cpu_count()
            },
            "results": {
                "total_files": self.stats['total_files'],
                "transcoded": self.stats['transcoded'],
                "skipped": self.stats['skipped'],
                "moved": self.stats['moved'],
                "sony_proxies_moved": self.stats['sony_proxies_moved']
            },
            "timestamp": self.timestamp
        }
        
        # Generate JSON filename
        json_filename = f"benchmark-{self.codec_config.selected_codec}-{actual_workers}workers-{self.timestamp}.json"
        json_path = self.proxy_logs_dir / json_filename
        
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(benchmark_data, f, indent=2)
        
        return json_path, benchmark_data

    def _print_final_stats(self):
        """Print final statistics and generate detailed report"""
        total_time = time.time() - self.stats['start_time']
        human_time = format_time_human(total_time)
        sony_summary = f"\nSony proxies moved: {self.stats['sony_proxies_moved']}\n" if self.stats['sony_proxies_moved'] > 0 else ""
        
        self._log(
            f"\nFinal Report:\n"
            f"Total time: {total_time:.2f} seconds ({human_time})\n"
            f"Total files found: {self.stats['total_files']}\n"
            f"Files transcoded: {self.stats['transcoded']}\n"
            f"Files skipped: {self.stats['skipped']}\n"
            f"Proxies moved: {self.stats['moved']}{sony_summary}"
        )
        
        # Generate detailed report
        report_path = self._generate_detailed_report()
        self._log(f"\nDetailed report generated at: {report_path}\n")
        
        # Generate JSON output if requested
        if self.json_output:
            json_path, json_data = self._generate_benchmark_json()
            self._log(f"Benchmark JSON generated at: {json_path}\n")

        if self.shutdown:
            self._shutdown_system()

    def process(self):
        """Process either a single file or directory based on input"""
        if self.source_path.is_dir():
            self.process_directory()
        else:
            self.process_single_file()

    def _get_audio_codec_info(self, video_path):
        """Get audio codec information from video file"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name,codec_long_name,bit_rate,sample_rate',
                '-of', 'json',
                str(video_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]
                return {
                    'codec_name': stream.get('codec_name', 'unknown'),
                    'codec_long_name': stream.get('codec_long_name', 'unknown'),
                    'bit_rate': stream.get('bit_rate', 'unknown'),
                    'sample_rate': stream.get('sample_rate', 'unknown'),
                    'has_audio': True
                }
            else:
                return {'has_audio': False}
                
        except Exception as e:
            self._log(f"Warning: Could not detect audio codec for {video_path}: {str(e)}")
            return {'has_audio': False}

    def _should_copy_audio(self, audio_info):
        """Determine if audio should be copied or re-encoded based on codec"""
        if not audio_info.get('has_audio', False):
            return False, "No audio stream"
            
        codec_name = audio_info.get('codec_name', '').lower()
        
        # List of compressed audio codecs that should be copied
        compressed_codecs = {
            'aac', 'mp3', 'ac3', 'eac3', 'dts', 'truehd', 'flac', 'vorbis', 'opus'
        }
        
        # List of uncompressed/large audio codecs that should be re-encoded
        uncompressed_codecs = {
            'pcm_s16be', 'pcm_s16le', 'pcm_s24be', 'pcm_s24le', 'pcm_s32be', 'pcm_s32le',
            'pcm_f32be', 'pcm_f32le', 'pcm_f64be', 'pcm_f64le'
        }
        
        if codec_name in compressed_codecs:
            return True, f"Compressed codec ({codec_name}) - copying"
        elif codec_name in uncompressed_codecs:
            return False, f"Uncompressed codec ({codec_name}) - re-encoding to AAC"
        else:
            # For unknown codecs, check bit rate if available
            try:
                bit_rate = int(audio_info.get('bit_rate', 0))
                if bit_rate > 1000000:  # > 1 Mbps, likely uncompressed
                    return False, f"High bitrate ({bit_rate} bps) - re-encoding to AAC"
                else:
                    return True, f"Unknown codec ({codec_name}) with reasonable bitrate - copying"
            except (ValueError, TypeError):
                return True, f"Unknown codec ({codec_name}) - copying (default)"

def _clean_path_input(path_input):
    """Clean path input to handle copy-paste scenarios with quotes and escaping"""
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

# The main function remains unchanged
def main():
    # Display application info and current settings upfront
    print("=" * 80)
    print("🎬 VIDEO PROXY GENERATOR")
    print("=" * 80)
    
    if len(sys.argv) > 1:
        # Process quoted paths while preserving other arguments
        args = sys.argv[1:]
        new_args = []
        in_quotes = False
        path_parts = []

        for arg in args:
            if (arg.startswith('"') or arg.startswith("'")) and not in_quotes:
                in_quotes = True
                path_parts.append(arg)
            elif (arg.endswith('"') or arg.endswith("'")) and in_quotes:
                in_quotes = False
                path_parts.append(arg)
                full_path = ' '.join(path_parts)
                full_path = full_path.strip('"').strip("'")
                new_args.append(full_path)
                path_parts = []
            elif in_quotes:
                path_parts.append(arg)
            else:
                new_args.append(arg)

        if path_parts:
            full_path = ' '.join(path_parts)
            full_path = full_path.strip('"').strip("'")
            new_args.append(full_path)

        sys.argv[1:] = new_args

    parser = argparse.ArgumentParser(description='Generate video proxies')
    parser.add_argument('path', nargs='?',
                        help='Source path (directory or video file)')
    parser.add_argument('--scale', choices=['half', 'quarter'], default='quarter',
                        help='Scaling factor (default: quarter)')
    parser.add_argument('--codec', choices=['prores', 'h264', 'dnxhr'],
                        default='prores', help='Output codec (default: prores)')
    parser.add_argument('--no-parallel', action='store_true',
                        help='Disable parallel processing (parallel is enabled by default)')
    parser.add_argument('--max-workers', type=int,
                        help='Maximum number of concurrent processes for parallel processing')
    parser.add_argument('--shutdown', action='store_true',
                        help='Shutdown the computer when processing is complete')
    parser.add_argument('--json-output', action='store_true',
                        help='Generate JSON output for benchmarking')
    parser.add_argument('--skip-existing', action='store_true',
                        help='Automatically skip videos that already have proxies (different extensions), no prompts')

    args = parser.parse_args()

    # Display current settings
    _display_current_settings(args)

    # Handle path input with improved quote stripping
    if not args.path:
        args.path = _prompt_for_path()

    # Additional path cleaning for copy-paste scenarios
    args.path = _clean_path_input(args.path)

    # Ensure the path exists and is accessible
    source_path = Path(args.path).expanduser().resolve()
    if not source_path.exists():
        print(f"❌ Error: Path '{source_path}' does not exist")
        print("Please check the path and try again.")
        sys.exit(1)

    print(f"✅ Source path validated: {source_path}")
    print("=" * 80)

    # Create and run proxy generator
    generator = ProxyGenerator(
        source_path,
        scale=args.scale,
        codec=args.codec,
        parallel=not args.no_parallel,  # Invert the no_parallel flag
        max_workers=args.max_workers,
        shutdown=args.shutdown,
        json_output=args.json_output,
        skip_existing=args.skip_existing
    )
    generator.process()

def _display_current_settings(args):
    """Display current settings to the user"""
    print("📋 CURRENT SETTINGS:")
    print("-" * 40)
    print(f"Scale: {args.scale}")
    print(f"Codec: {args.codec}")
    print(f"Parallel Processing: {'No' if args.no_parallel else 'Yes'}")
    if not args.no_parallel and args.max_workers:
        print(f"Max Workers: {args.max_workers}")
    elif not args.no_parallel:
        default_workers = min(os.cpu_count() // 2 or 1, 8)
        print(f"Max Workers: {default_workers} (auto-detected)")
    print(f"Skip Existing: {'Yes' if args.skip_existing else 'No'}")
    print(f"Auto-shutdown: {'Yes' if args.shutdown else 'No'}")
    print(f"JSON Output: {'Yes' if args.json_output else 'No'}")
    print()

def _prompt_for_path():
    """Prompt user for source path with helpful instructions"""
    print("📁 SOURCE PATH REQUIRED:")
    print("-" * 40)
    print("Please enter the source path (directory or video file).")
    print("💡 Tips:")
    print("  • You can copy-paste paths directly from File Explorer/Finder")
    print("  • Quotes around paths will be automatically handled")
    print("  • Use ~ for home directory (e.g., ~/Videos)")
    print()
    
    while True:
        user_input = input("Enter path: ").strip()
        if user_input:
            return user_input
        print("⚠️  Path cannot be empty. Please enter a valid path.")

if __name__ == '__main__':
    main()
