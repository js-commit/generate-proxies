#!/usr/bin/env python3
import os
import sys
import subprocess
import json
import time
import platform
import argparse
import re
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import shutil

from codec_configuration import CodecConfiguration

class ProxyGenerator:
    def __init__(self, source_path, scale="quarter", codec="prores", parallel=True, max_workers=None, shutdown=False):
        self.source_path = Path(source_path)
        self.scale = scale
        self.parallel = parallel
        self.max_workers = max_workers
        self.shutdown = shutdown
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.source_path.parent / f"proxy-gen-logs-and-report-{self.timestamp}.txt"
        self.report_file = None  
        self.video_extensions = {'.mp4', '.mov', '.mxf', '.avi', '.mkv'}
        self.stats = {
            'total_files': 0,
            'transcoded': 0,
            'skipped': 0,
            'moved': 0,
            'start_time': time.time()
        }
        self.processed_files_details = []
        
        # User choice tracking for duplicate proxy handling
        self.user_choice_for_duplicates = None  # None, 'yes_to_all', 'skip_all'

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

    def _is_android_footage(self, file_path):
        """Check if the footage is from an Android device"""
        try:
            result = subprocess.run(['exiftool', '-json', file_path],
                                    capture_output=True, text=True, check=True)
            metadata = json.loads(result.stdout)[0]
            return 'AndroidVersion' in metadata
        except Exception:
            return False

    def _is_android_folder(self, folder_path):
        """Check if the folder contains a .is_android indicator file"""
        indicator_file = Path(folder_path) / '.is_android'
        return indicator_file.exists()

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
        if self.parallel:
            # In parallel mode, automatically create duplicate to avoid user input issues
            return 'yes'
            
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

    def _process_file(self, video_path):
        """Process a single video file"""
        video_path = Path(video_path)
        self._log(f"Processing file: {video_path}")

        # Create a details dictionary for this file
        file_details = {
            "filename": str(video_path),
            "size_mb": self._get_file_size(video_path),
            "processing_start": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "pending",
            "codec_decision": {},
            "processing_time_seconds": 0
        }

        # Get the parent proxies directory
        proxies_dir = self._get_proxies_dir(video_path)
        self._log(f"Using parent proxies directory: {proxies_dir}")

        # Determine if this is Android footage - either by metadata or folder indicator
        is_android_metadata = self._is_android_footage(video_path)
        is_android_folder = self._is_android_folder(video_path.parent)
        is_android = is_android_metadata or is_android_folder
        
        file_details["is_android_footage"] = is_android
        file_details["android_detection_method"] = "metadata" if is_android_metadata else ("folder indicator" if is_android_folder else "none")

        # Get codec extension based on selection (or Android override)
        selected_codec = "h264" if is_android else self.codec_config.selected_codec
        file_details["codec_decision"]["requested_codec"] = self.codec_config.selected_codec
        file_details["codec_decision"]["actual_codec"] = selected_codec
        
        if is_android:
            reason = "Android footage detected via "
            reason += "metadata" if is_android_metadata else "folder indicator"
            reason += ", using H.264"
            file_details["codec_decision"]["reason"] = reason
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
                self._log(f"Skipped {video_path.name} - user chose to skip due to existing proxy with different extension")
                file_details["result"] = "skipped"
                file_details["skip_reason"] = f"User chose to skip - existing proxy: {existing_different_proxy.name}"
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

            # Get hardware acceleration and codec configuration
            config = self.codec_config.get_configuration(is_android)
            
            # Build video filter chain with format conversion if needed for CUDA
            video_filter = self.codec_config.build_video_filter(
                scaling, 
                config.get('needs_format_conversion', False)
            )
            
            if config.get('needs_format_conversion', False):
                self._log(f"Adding format=yuv420p filter for CUDA hardware acceleration")
            
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

                self._log(
                    f"Transcoded: {video_path.name}\n"
                    f"Time: {duration:.2f} seconds\n"
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
        with open(self.log_file, 'a') as f:
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
        self.report_file = self.source_path.parent / descriptive_filename
        
        with open(self.report_file, 'w') as f:
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
            f.write(f"Total Processing Time: {total_time:.2f} seconds\n")
            f.write(f"Total Files Found: {self.stats['total_files']}\n")
            f.write(f"Files Transcoded: {self.stats['transcoded']}\n")
            f.write(f"Files Skipped: {self.stats['skipped']}\n")
            f.write(f"Proxies Moved: {self.stats['moved']}\n\n")
            
            # File Details Section
            f.write("\n" + "=" * 80 + "\n")
            f.write("PER-FILE DETAILS\n")
            f.write("=" * 80 + "\n\n")
            
            for idx, file_details in enumerate(self.processed_files_details, 1):
                f.write(f"File {idx}: {file_details['filename']}\n")
                f.write("-" * 80 + "\n")
                f.write(f"Size: {file_details['size_mb']:.2f} MB\n")
                f.write(f"Android Footage: {'Yes' if file_details.get('is_android_footage', False) else 'No'}\n")
                f.write(f"Result: {file_details['result']}\n")
                
                if file_details['result'] == 'skipped':
                    f.write(f"Skip Reason: {file_details.get('skip_reason', 'Unknown')}\n")
                
                if file_details['result'] == 'transcoded':
                    f.write(f"Processing Time: {file_details['processing_time_seconds']:.2f} seconds\n")
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

    def _print_final_stats(self):
        """Print final statistics and generate detailed report"""
        total_time = time.time() - self.stats['start_time']
        self._log(
            f"\nFinal Report:\n"
            f"Total time: {total_time:.2f} seconds\n"
            f"Total files found: {self.stats['total_files']}\n"
            f"Files transcoded: {self.stats['transcoded']}\n"
            f"Files skipped: {self.stats['skipped']}\n"
            f"Proxies moved: {self.stats['moved']}\n"
        )
        
        # Generate detailed report
        report_path = self._generate_detailed_report()
        self._log(f"\nDetailed report generated at: {report_path}\n")

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

# The main function remains unchanged
def main():
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

    args = parser.parse_args()

    if not args.path:
        print("Please enter the source path (directory or video file, you can use quotes if path contains spaces):")
        user_input = input().strip()
        args.path = user_input.strip("'")

    # Ensure the path exists and is accessible
    source_path = Path(args.path).expanduser().resolve()
    if not source_path.exists():
        print(f"Error: Path '{source_path}' does not exist")
        sys.exit(1)

    # Create and run proxy generator
    generator = ProxyGenerator(
        source_path,
        scale=args.scale,
        codec=args.codec,
        parallel=not args.no_parallel,  # Invert the no_parallel flag
        max_workers=args.max_workers,
        shutdown=args.shutdown
    )
    generator.process()

if __name__ == '__main__':
    main()
