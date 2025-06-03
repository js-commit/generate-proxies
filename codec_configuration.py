import platform
import subprocess
import json
from typing import Dict, List, Optional

class CodecConfiguration:
    CODEC_PROFILES = {
        'h264': {
            'videotoolbox': {
                'codec': 'h264_videotoolbox',
                'preset': None,
                'extra_args': [
                    '-quality', 'medium'  # VideoToolbox quality-based encoding
                ]
            },
            'cuda': {
                'codec': 'h264_nvenc',
                'preset': 'p4',
                'extra_args': [
                    '-rc', 'constqp',
                    '-qp', '23',  # Quality-based encoding for NVENC
                    '-g', '15',   # More keyframes for smoother timeline scrubbing
                    '-bf', '0',   # No B-frames for better seeking
                    '-forced-idr', '1',  # More accurate seeking points
                    '-movflags', '+faststart'  # Faster playback startup
                ]
            },
            'qsv': {
                'codec': 'h264_qsv',
                'preset': 'veryfast',
                'extra_args': [
                    '-global_quality', '23'  # Quality-based encoding for QSV
                ]
            },
            'software': {
                'codec': 'libx264',
                'preset': 'veryfast',
                'extra_args': [
                    '-crf', '23',  # Quality-based encoding for x264
                    '-profile:v', 'high',
                    '-level:v', '4.1',
                    '-g', '30',
                    '-bf', '2',
                    '-refs', '3'
                ]
            }
        },
        'hevc': {
            'videotoolbox': {
                'codec': 'hevc_videotoolbox',
                'extra_args': [
                    '-tag:v', 'hvc1',
                    '-quality', 'medium'
                ]
            },
            'cuda': {
                'codec': 'hevc_nvenc',
                'preset': 'p4',
                'extra_args': [
                    '-rc', 'constqp',
                    '-qp', '23'
                ]
            },
            'qsv': {
                'codec': 'hevc_qsv',
                'preset': 'veryfast',
                'extra_args': [
                    '-global_quality', '23'
                ]
            },
            'software': {
                'codec': 'libx265',
                'preset': 'veryfast',
                'extra_args': [
                    '-crf', '23'
                ]
            }
        },
        'prores': {
            'videotoolbox': {
                'codec': 'prores_videotoolbox',
                'profile': '0'
            },
            'software': {
                'codec': 'prores_ks',
                'profile': '0'
            }
        },
        'dnxhr': {
            'software': {
                'codec': 'dnxhd',
                'profile': 'dnxhr_lb'
            }
        }
    }

    HW_ACCEL_MAP = {
        'Darwin': ['videotoolbox'],
        'Windows': ['cuda', 'qsv']
    }

    def __init__(self, selected_codec: str = "prores"):
        self.system = platform.system()
        self.selected_codec = selected_codec.lower()
        self.hw_acceleration = self._detect_hw_acceleration()
        self._validate_codec()

    def _validate_codec(self) -> None:
        """Validate that the selected codec is supported"""
        if self.selected_codec not in self.CODEC_PROFILES:
            raise ValueError(f"Unsupported codec: {self.selected_codec}")

    def _detect_hw_acceleration(self) -> Optional[str]:
        """Detect available hardware acceleration for the current system"""
        available_accelerators = self.HW_ACCEL_MAP.get(self.system, [])

        for accel in available_accelerators:
            if accel == 'videotoolbox':
                return accel
            elif self._check_ffmpeg_hw_support(accel):
                return accel

        return None

    def _check_ffmpeg_hw_support(self, hwaccel: str) -> bool:
        """Test if specific hardware acceleration is supported"""
        try:
            subprocess.run(
                ['ffmpeg', '-hwaccel', hwaccel, '-version'],
                capture_output=True,
                check=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def _get_source_video_info(self, video_path: str) -> Dict[str, str]:
        """Get source video format information to detect problematic combinations"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,profile,pix_fmt',
                '-of', 'json',
                str(video_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]
                return {
                    'codec_name': stream.get('codec_name', 'unknown').lower(),
                    'profile': stream.get('profile', 'unknown').lower(),
                    'pix_fmt': stream.get('pix_fmt', 'unknown').lower()
                }
        except Exception:
            # If detection fails, return safe defaults
            pass
        
        return {
            'codec_name': 'unknown',
            'profile': 'unknown', 
            'pix_fmt': 'unknown'
        }

    def _is_hevc_10bit(self, video_info: Dict[str, str]) -> bool:
        """Check if source video is HEVC 10-bit format"""
        codec_name = video_info.get('codec_name', '').lower()
        profile = video_info.get('profile', '').lower()
        pix_fmt = video_info.get('pix_fmt', '').lower()
        
        # Check for HEVC codec
        is_hevc = codec_name in ['hevc', 'h265']
        
        # Check for 10-bit indicators
        is_10bit = (
            'main 10' in profile or
            '10' in pix_fmt or
            'p010' in pix_fmt or
            'yuv420p10' in pix_fmt
        )
        
        return is_hevc and is_10bit

    def _get_codec_config(self, is_mobile: bool = False) -> Dict[str, List[str]]:
        """Get codec configuration based on system capabilities and requirements"""
        # For mobile/consumer devices, always use H.264 to avoid VFR stuttering issues
        codec = 'h264' if is_mobile else self.selected_codec

        # Determine acceleration profile to use
        accel_profile = self.hw_acceleration if self.hw_acceleration in self.CODEC_PROFILES[codec] else 'software'
        profile = self.CODEC_PROFILES[codec][accel_profile]

        # Build codec arguments
        codec_args = ['-c:v', profile['codec']]

        if profile.get('preset'):
            codec_args.extend(['-preset', profile['preset']])

        if profile.get('profile'):
            codec_args.extend(['-profile:v', profile['profile']])

        if profile.get('extra_args'):
            codec_args.extend(profile['extra_args'])

        # Build hardware acceleration arguments with improved CUDA support
        hw_accel_args = []
        if self.hw_acceleration:
            hw_accel_args.extend(['-hwaccel', self.hw_acceleration])
            # Add hwaccel_output_format for CUDA to keep video on GPU
            if self.hw_acceleration == 'cuda' and codec in ['h264', 'hevc']:
                hw_accel_args.extend(['-hwaccel_output_format', 'cuda'])

        # Determine if we need format conversion for hardware acceleration
        # With hwaccel_output_format cuda, we no longer need format conversion
        needs_format_conversion = False

        return {
            'hw_accel_args': hw_accel_args,
            'codec_args': codec_args,
            'needs_format_conversion': needs_format_conversion,
            'hw_acceleration': self.hw_acceleration
        }

    def get_configuration(self, is_mobile: bool = False) -> Dict[str, List[str]]:
        """Public method to get the full configuration"""
        return self._get_codec_config(is_mobile)

    def build_video_filter(self, base_filter: str, needs_format_conversion: bool = False, 
                          video_path: str = None, target_codec: str = None) -> tuple[str, str]:
        """Build the complete video filter chain with GPU-accelerated scaling for CUDA
        
        Returns:
            tuple: (video_filter, fallback_reason)
        """
        # Detect source video format if path is provided
        source_info = {}
        if video_path:
            source_info = self._get_source_video_info(video_path)
        
        # Check for problematic HEVC 10-bit + ProRes combination
        is_hevc_10bit = self._is_hevc_10bit(source_info)
        is_prores_target = target_codec in ['prores', 'dnxhr']
        
        # Apply CUDA optimization fallback logic
        use_cpu_fallback = False
        fallback_reason = ""
        
        if (self.hw_acceleration == 'cuda' and is_hevc_10bit and is_prores_target):
            use_cpu_fallback = True
            fallback_reason = f"HEVC 10-bit source → {target_codec.upper()} target: Using CPU scaling to avoid format compatibility issues"
        
        # Apply the appropriate filter chain
        if use_cpu_fallback:
            # Use CPU scaling with format conversion for problematic combinations
            filters = [base_filter, 'format=yuv420p']
        else:
            # Use GPU scaling for CUDA when compatible
            if self.hw_acceleration == 'cuda':
                # Replace CPU scale with GPU scale_cuda
                if base_filter.startswith('scale='):
                    base_filter = base_filter.replace('scale=', 'scale_cuda=')
            
            filters = [base_filter]
            
            # Add format conversion if needed (legacy cases)
            if needs_format_conversion:
                filters.append('format=yuv420p')
        
        return ','.join(filters), fallback_reason

    def get_system_info(self) -> dict:
        """Get detailed information about system and codec support"""
        system_info = {
            "system": self.system,
            "selected_codec": self.selected_codec,
            "hw_acceleration": self.hw_acceleration,
            "available_accelerators": self.HW_ACCEL_MAP.get(self.system, []),
            "codec_profiles": {}
        }
        
        # Add details about each codec profile
        for codec, profiles in self.CODEC_PROFILES.items():
            system_info["codec_profiles"][codec] = {
                "available_accelerators": list(profiles.keys()),
                "selected_accelerator": self.hw_acceleration if self.hw_acceleration in profiles else "software"
            }
        
        return system_info
