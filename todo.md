- post benchmarks to https://www.notion.so/jsyntax/Transcoding-benchmarks-M4-vs-M4-Pro-vs-Intel-10850K-RTX3070-20670218025c806fafaaeb733393b56b
- host files for benchmarking (so others can run)*

## Compare against Media Encoder
- Time for exact same footage
- Validate codec output equivalency for all codecs

## Optimization
- Validate most efficient ffmpeg usage, especially for

## File management
- Some video files may already have proxies, safely move them to `../proxies` folder as a pre-setup (e.g. Sony camera files) 
- Recursively look into all folders, see if media file exist confirm with user what folders can be converted (e.g. we don't want to convert exported files or ignored files)

## Misc
- Record ffmpeg version and video card in future results
