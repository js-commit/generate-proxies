import platform
import subprocess
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
                    '-qp', '23'  # Quality-based encoding for NVENC
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

        # Build hardware acceleration arguments
        hw_accel_args = ['-hwaccel', self.hw_acceleration] if self.hw_acceleration else []

        # Determine if we need format conversion for hardware acceleration
        needs_format_conversion = (self.hw_acceleration == 'cuda' and 
                                 codec in ['h264', 'hevc'])

        return {
            'hw_accel_args': hw_accel_args,
            'codec_args': codec_args,
            'needs_format_conversion': needs_format_conversion,
            'hw_acceleration': self.hw_acceleration
        }

    def get_configuration(self, is_mobile: bool = False) -> Dict[str, List[str]]:
        """Public method to get the full configuration"""
        return self._get_codec_config(is_mobile)

    def build_video_filter(self, base_filter: str, needs_format_conversion: bool = False) -> str:
        """Build the complete video filter chain including format conversion if needed"""
        filters = [base_filter]
        
        # Add format conversion for CUDA to handle 10-bit to 8-bit conversion
        if needs_format_conversion:
            filters.append('format=yuv420p')
            
        return ','.join(filters)

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
