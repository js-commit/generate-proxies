# Video Proxy Generator Benchmark Suite

This benchmark suite automatically tests different configurations of the video proxy generator to determine optimal performance settings for your system.

## Quick Start

```bash
# Run benchmark on a directory of video files
python benchmark.py /path/to/video/directory

# Run benchmark on a single video file
python benchmark.py /path/to/video/file.mp4
```

## What It Tests

The benchmark runs **8 different configurations** by testing all combinations of:

### Codecs (2)
- **H.264**: Fast, widely compatible, smaller files
- **ProRes**: Higher quality, larger files, optimized for editing

### Parallelism (4)
- **Single**: 1 worker (baseline)
- **2x**: 2 parallel workers
- **6x**: 6 parallel workers  
- **8x**: 8 parallel workers

## What You Get

### Console Output
- Real-time progress for each configuration
- Performance comparison tables
- Optimal configuration recommendations
- Efficiency analysis with speedup calculations

### Generated Files

#### Benchmark Logs Directory
All results are saved in `benchmark_logs/` next to your source directory:

1. **Comprehensive Report** (`benchmark-report-[CPU]-[timestamp].json`)
   - Complete performance analysis
   - Efficiency metrics and recommendations
   - System information
   - All raw results

2. **Individual Run Data** (automatic from proxy_generator.py)
   - Detailed logs in `proxy_logs/`
   - JSON results for each configuration

## How It Works

1. **Clean Start**: Before each test, all existing proxy files are automatically deleted
2. **Run Configuration**: Executes proxy_generator.py with specific settings
3. **Collect Results**: Gathers timing and performance data
4. **Repeat**: Tests all 8 configurations systematically
5. **Analyze**: Calculates speedups, efficiency, and optimal settings
6. **Report**: Generates comprehensive performance report

## Understanding the Results

### Performance Metrics

- **Time (s)**: Total processing time in seconds
- **Workers**: Number of parallel workers used
- **Speedup**: How much faster than single-threaded (e.g., 2.5x = 2.5 times faster)
- **Efficiency**: How well workers are utilized (1.0 = perfect, 0.5 = 50% efficiency)

### Recommendations

The system provides three types of recommendations:

1. **Most Efficient**: Best worker utilization (highest efficiency score)
2. **Fastest**: Lowest total processing time
3. **Sweet Spot**: Best balance of speed and efficiency (≥70% efficiency + ≥2x speedup)

### Example Output

```
🎯 H264 CODEC PERFORMANCE:
--------------------------------------------------
Configuration   Time (s)   Workers  Files    Speedup
--------------------------------------------------
h264-6x         45.2       6        12       3.21x ⭐
h264-8x         47.1       8        12       3.08x
h264-2x         68.5       2        12       2.12x
h264-single     145.0      1        12       1.00x

🏆 Optimal for H264: h264-6x (45.2s with 6 workers)
```

## System Requirements

- Python 3.6+
- FFmpeg installed and in PATH
- ExifTool installed and in PATH
- Sufficient disk space (proxies are created and deleted multiple times)

## Tips for Accurate Benchmarks

1. **Use Representative Data**: Test with video files similar to your typical workflow
2. **Sufficient File Count**: Use at least 5-10 video files for meaningful results
3. **Close Other Apps**: Minimize system load during benchmarking
4. **Multiple Runs**: Consider running multiple benchmarks and averaging results
5. **Check Disk Space**: Ensure enough space for temporary proxy files

## Understanding Your Results

### When Single-Threading Wins
- Very small files or very few files
- I/O bottleneck (slow storage)
- CPU already heavily loaded

### When Parallel Processing Shines
- Multiple large video files
- Fast storage (SSD)
- Available CPU cores
- CPU-intensive codecs (ProRes, DNxHR)

### Hardware Acceleration Impact
The benchmark will automatically use available hardware acceleration:
- **NVIDIA**: NVENC encoding
- **Intel**: Quick Sync
- **AMD**: AMF encoding
- **Apple Silicon**: VideoToolbox

## Troubleshooting

### Common Issues

1. **No video files found**: Ensure your path contains supported video formats (.mp4, .mov, .mxf, .avi, .mkv)
2. **Benchmark fails**: Check that proxy_generator.py works manually first
3. **Missing dependencies**: Install FFmpeg and ExifTool
4. **Permission errors**: Ensure write access to source directory parent

### Debug Steps

1. Test proxy_generator.py manually:
   ```bash
   python proxy_generator.py /path/to/test/file.mp4 --json-output
   ```

2. Check system requirements:
   ```bash
   ffmpeg -version
   exiftool -ver
   ```

3. Verify Python imports:
   ```bash
   python -c "import json, subprocess, pathlib; print('All imports OK')"
   ```

## Interpreting Performance Data

### Efficiency Guidelines
- **0.8-1.0**: Excellent efficiency, well-suited for parallel processing
- **0.6-0.8**: Good efficiency, parallel processing beneficial
- **0.4-0.6**: Moderate efficiency, consider reducing workers
- **0.0-0.4**: Poor efficiency, may be better with fewer workers or single-threading

### When to Use Each Codec
- **H.264**: Daily editing, quick turnarounds, storage-conscious workflows
- **ProRes**: Color grading, final delivery, maximum quality preservation

## Performance Optimization Tips

Based on benchmark results, you can optimize your workflow:

1. **Use Recommended Settings**: Apply the "sweet spot" configuration for regular use
2. **Codec Selection**: Choose based on your primary use case
3. **Worker Count**: Don't always use maximum cores - efficiency matters
4. **Hardware Upgrades**: Identify bottlenecks (CPU vs. storage vs. memory)

---

**Note**: The benchmark automatically cleans up proxy files between runs, so you won't be prompted about existing proxies during the test process. 