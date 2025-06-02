#!/usr/bin/env python3
import os
import sys
import json
import shutil
import subprocess
import argparse
import time
from pathlib import Path
from datetime import datetime
import platform

def format_time_human(seconds):
    """Convert seconds to human-readable MM:SS format"""
    if seconds is None:
        return "N/A"
    minutes = int(seconds // 60)
    seconds_remainder = int(seconds % 60)
    return f"{minutes}:{seconds_remainder:02d}"

class ProxyBenchmark:
    def __init__(self, source_path):
        self.source_path = Path(source_path)
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Benchmark configurations
        self.codecs = ['h264', 'prores']
        self.worker_configs = [
            {'parallel': False, 'workers': 1, 'name': 'single'},
            {'parallel': True, 'workers': 2, 'name': '2x'},
            {'parallel': True, 'workers': 6, 'name': '6x'},
            {'parallel': True, 'workers': 8, 'name': '8x'}
        ]
        
        # Results storage
        self.results = []
        self.benchmark_logs_dir = self.source_path.parent / "benchmark_logs"
        self.benchmark_logs_dir.mkdir(exist_ok=True)
        
        # System info
        self.system_info = self._collect_system_info()
        
        print("=" * 80)
        print("🚀 PROXY GENERATOR BENCHMARK SUITE")
        print("=" * 80)
        print(f"Source Path: {self.source_path}")
        print(f"System: {self.system_info['cpu']}")
        print(f"Available Cores: {self.system_info['available_cores']}")
        print(f"Total Configurations: {len(self.codecs) * len(self.worker_configs)}")
        print("=" * 80)

    def _collect_system_info(self):
        """Collect basic system information"""
        try:
            if platform.system() == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
                cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0]
            elif platform.system() == "Darwin":  # macOS
                cmd = ["sysctl", "-n", "machdep.cpu.brand_string"]
                cpu = subprocess.check_output(cmd).decode().strip()
            else:  # Linux
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("model name"):
                            cpu = line.split(":", 1)[1].strip()
                            break
                    else:
                        cpu = "Unknown CPU"
        except Exception:
            cpu = "Unknown CPU"
            
        return {
            'cpu': cpu,
            'os': platform.system(),
            'os_version': platform.version(),
            'available_cores': os.cpu_count()
        }

    def _clean_proxies(self):
        """Remove all proxy files and directories to start fresh"""
        print("🧹 Cleaning existing proxies...")
        
        # Remove parent proxies directory
        proxies_dir = self.source_path.parent / 'proxies'
        if proxies_dir.exists():
            shutil.rmtree(proxies_dir)
            print(f"   Removed: {proxies_dir}")
        
        print("✅ Cleanup complete (logs preserved)\n")

    def _run_proxy_generator(self, codec, worker_config):
        """Run proxy generator with specific configuration"""
        config_name = f"{codec}-{worker_config['name']}"
        print(f"🎬 Running: {config_name}")
        
        # Build command
        cmd = [
            sys.executable, 'proxy_generator.py',
            str(self.source_path),
            '--codec', codec,
            '--json-output'
        ]
        
        if worker_config['parallel']:
            cmd.extend(['--max-workers', str(worker_config['workers'])])
        else:
            cmd.append('--no-parallel')
        
        try:
            # Run the command
            start_time = time.time()
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            end_time = time.time()
            
            # Find the JSON output file
            json_file = self._find_latest_json()
            if json_file:
                with open(json_file, 'r') as f:
                    json_data = json.load(f)
                
                # Add our benchmark metadata
                json_data['benchmark_metadata'] = {
                    'config_name': config_name,
                    'subprocess_time': round(end_time - start_time, 2)
                }
                
                self.results.append(json_data)
                completion_time = json_data['completion_time_seconds']
                human_time = format_time_human(completion_time)
                print(f"   ✅ Completed in {completion_time}s ({human_time})")
                return True
            else:
                print(f"   ❌ No JSON output found")
                return False
                
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Failed: {e}")
            print(f"   Error output: {e.stderr}")
            return False
        except Exception as e:
            print(f"   ❌ Unexpected error: {e}")
            return False

    def _find_latest_json(self):
        """Find the most recently created benchmark JSON file"""
        proxy_logs_dir = self.source_path.parent / 'proxy_logs'
        if not proxy_logs_dir.exists():
            return None
        
        json_files = list(proxy_logs_dir.glob('benchmark-*.json'))
        if not json_files:
            return None
        
        # Return the most recent file
        return max(json_files, key=lambda f: f.stat().st_mtime)

    def _analyze_results(self):
        """Analyze benchmark results and determine optimal configurations"""
        if not self.results:
            print("❌ No results to analyze")
            return
        
        print("\n" + "=" * 80)
        print("📊 BENCHMARK ANALYSIS")
        print("=" * 80)
        
        # Group results by codec
        codec_results = {}
        for result in self.results:
            codec = result['configuration']['codec']
            if codec not in codec_results:
                codec_results[codec] = []
            codec_results[codec].append(result)
        
        optimal_configs = {}
        
        for codec, results in codec_results.items():
            print(f"\n🎯 {codec.upper()} CODEC PERFORMANCE:")
            print("-" * 50)
            
            # Sort by completion time
            results.sort(key=lambda x: x['completion_time_seconds'])
            
            fastest = results[0]
            optimal_configs[codec] = fastest
            
            print(f"{'Configuration':<15} {'Time (s)':<10} {'Time (MM:SS)':<12} {'Workers':<8} {'Files':<8} {'Speedup':<8}")
            print("-" * 65)
            
            baseline_time = None
            for result in results:
                config = result['benchmark_metadata']['config_name']
                time_taken = result['completion_time_seconds']
                human_time = format_time_human(time_taken)
                workers = result['configuration']['max_workers']
                files = result['results']['transcoded']
                
                # Calculate speedup relative to single-threaded
                if workers == 1:
                    baseline_time = time_taken
                    speedup = "1.00x"
                elif baseline_time:
                    speedup = f"{baseline_time / time_taken:.2f}x"
                else:
                    speedup = "N/A"
                
                # Mark the fastest configuration
                marker = " ⭐" if result == fastest else ""
                
                print(f"{config:<15} {time_taken:<10.1f} {human_time:<12} {workers:<8} {files:<8} {speedup:<8}{marker}")
            
            optimal_time_human = format_time_human(fastest['completion_time_seconds'])
            print(f"\n🏆 Optimal for {codec.upper()}: {fastest['benchmark_metadata']['config_name']} "
                  f"({fastest['completion_time_seconds']:.1f}s / {optimal_time_human} with {fastest['configuration']['max_workers']} workers)")
        
        return optimal_configs

    def _generate_final_report(self, optimal_configs):
        """Generate comprehensive benchmark report"""
        # Create detailed filename
        cpu_clean = self.system_info['cpu'].replace(' ', '-').replace('(R)', '').replace('(TM)', '')
        cpu_clean = cpu_clean[:30] if len(cpu_clean) > 30 else cpu_clean
        
        report_filename = f"benchmark-report-{cpu_clean}-{self.timestamp}.json"
        report_path = self.benchmark_logs_dir / report_filename
        
        # Calculate efficiency metrics
        efficiency_analysis = {}
        codec_results = {}
        
        for result in self.results:
            codec = result['configuration']['codec']
            if codec not in codec_results:
                codec_results[codec] = []
            codec_results[codec].append(result)
        
        for codec, results in codec_results.items():
            single_thread_time = None
            parallel_times = []
            
            for result in results:
                workers = result['configuration']['max_workers']
                time_taken = result['completion_time_seconds']
                
                if workers == 1:
                    single_thread_time = time_taken
                else:
                    parallel_times.append({
                        'workers': workers,
                        'time': time_taken,
                        'config': result['benchmark_metadata']['config_name']
                    })
            
            if single_thread_time:
                efficiency_data = []
                for pt in parallel_times:
                    speedup = single_thread_time / pt['time']
                    efficiency = speedup / pt['workers']  # Perfect efficiency = 1.0
                    efficiency_data.append({
                        'workers': pt['workers'],
                        'speedup': round(speedup, 2),
                        'efficiency': round(efficiency, 3),
                        'config': pt['config']
                    })
                
                efficiency_analysis[codec] = {
                    'baseline_time': single_thread_time,
                    'parallel_results': efficiency_data
                }
        
        # Create comprehensive report
        final_report = {
            'benchmark_metadata': {
                'timestamp': self.timestamp,
                'source_path': str(self.source_path),
                'total_configurations_tested': len(self.results),
                'benchmark_duration_minutes': round(sum(r['completion_time_seconds'] for r in self.results) / 60, 1),
                'total_benchmark_time_seconds': round(sum(r['completion_time_seconds'] for r in self.results), 1),
                'total_benchmark_time_human': format_time_human(sum(r['completion_time_seconds'] for r in self.results))
            },
            'system_info': self.system_info,
            'optimal_configurations': {
                codec: {
                    'config_name': config['benchmark_metadata']['config_name'],
                    'workers': config['configuration']['max_workers'],
                    'time_seconds': config['completion_time_seconds'],
                    'time_human': format_time_human(config['completion_time_seconds']),
                    'hardware_acceleration': config['configuration']['hardware_acceleration']
                } for codec, config in optimal_configs.items()
            },
            'efficiency_analysis': efficiency_analysis,
            'all_results': self.results,
            'recommendations': self._generate_recommendations(optimal_configs, efficiency_analysis)
        }
        
        # Save report
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(final_report, f, indent=2)
        
        print(f"\n📋 Comprehensive benchmark report saved to: {report_path}")
        return report_path

    def _generate_recommendations(self, optimal_configs, efficiency_analysis):
        """Generate performance recommendations"""
        recommendations = {}
        
        for codec, analysis in efficiency_analysis.items():
            if not analysis['parallel_results']:
                continue
                
            # Find most efficient configuration (best efficiency score)
            most_efficient = max(analysis['parallel_results'], key=lambda x: x['efficiency'])
            
            # Find fastest configuration
            fastest = min(analysis['parallel_results'], key=lambda x: analysis['baseline_time'] / x['speedup'])
            
            # Determine sweet spot (good efficiency + reasonable speed)
            sweet_spot = None
            for result in analysis['parallel_results']:
                if result['efficiency'] >= 0.7 and result['speedup'] >= 2.0:  # At least 70% efficient and 2x speedup
                    if not sweet_spot or result['speedup'] > sweet_spot['speedup']:
                        sweet_spot = result
            
            recommendations[codec] = {
                'most_efficient': most_efficient,
                'fastest': fastest,
                'sweet_spot': sweet_spot or most_efficient,
                'advice': self._get_advice(most_efficient, fastest, sweet_spot)
            }
        
        return recommendations

    def _get_advice(self, most_efficient, fastest, sweet_spot):
        """Generate human-readable advice"""
        if sweet_spot and sweet_spot != most_efficient:
            return f"Recommended: {sweet_spot['workers']} workers for optimal balance of speed ({sweet_spot['speedup']:.1f}x) and efficiency ({sweet_spot['efficiency']:.1%})"
        elif most_efficient == fastest:
            return f"Clear winner: {most_efficient['workers']} workers provides both best speed and efficiency"
        else:
            return f"Choose {most_efficient['workers']} workers for efficiency ({most_efficient['efficiency']:.1%}) or {fastest['workers']} for maximum speed ({fastest['speedup']:.1f}x)"

    def run_benchmark(self):
        """Run the complete benchmark suite"""
        total_configs = len(self.codecs) * len(self.worker_configs)
        current_config = 0
        
        for codec in self.codecs:
            for worker_config in self.worker_configs:
                current_config += 1
                
                print(f"\n[{current_config}/{total_configs}] Testing {codec} with {worker_config['name']} processing")
                print("-" * 60)
                
                # Clean between runs
                self._clean_proxies()
                
                # Run the test
                success = self._run_proxy_generator(codec, worker_config)
                
                if not success:
                    print(f"❌ Skipping remaining tests due to failure")
                    break
                
                # Brief pause between runs
                time.sleep(2)
        
        # Analyze results
        if self.results:
            optimal_configs = self._analyze_results()
            self._generate_final_report(optimal_configs)
            
            print(f"\n🎉 Benchmark complete! Tested {len(self.results)} configurations.")
            print("Check the benchmark_logs directory for detailed results.")
        else:
            print("❌ No successful benchmark runs completed.")

def main():
    parser = argparse.ArgumentParser(description='Benchmark video proxy generation performance')
    parser.add_argument('path', help='Source path (directory or video file)')
    
    args = parser.parse_args()
    
    # Validate path
    source_path = Path(args.path).expanduser().resolve()
    if not source_path.exists():
        print(f"❌ Error: Path '{source_path}' does not exist")
        sys.exit(1)
    
    # Run benchmark
    benchmark = ProxyBenchmark(source_path)
    benchmark.run_benchmark()

if __name__ == '__main__':
    main() 