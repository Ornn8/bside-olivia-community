# Third-party notices

The bundled `BAAI/bge-small-zh-v1.5` embedding model is released by BAAI under the MIT License. Its pinned files are verified before installation into the user's data directory. Project: https://huggingface.co/BAAI/bge-small-zh-v1.5

Copyright (c) the model's contributors.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The app also bundles the following pinned runtimes and dependencies. Their license files are retained in the package's dependency metadata:

- Python 3.12.15 standalone runtime: PSF License.
- Electron 44.9.0: MIT License.
- electron-builder 26.15.3: MIT License.
- aiohttp 3.14.5, Pillow 12.3.0, imageio-ffmpeg 0.6.0, jsonschema 4.26.0, keyring 25.7.0.
- mem0ai 2.0.18 and the architecture-specific NumPy, PyTorch, Transformers and sentence-transformers packages.

## FFmpeg

The app runs the FFmpeg executable shipped inside the `imageio-ffmpeg` 0.6.0 macOS wheel (`Resources/python/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-*`). It is used unmodified, as a separate program, to remux downloaded media and to trim or crop clips.

- arm64 build: FFmpeg 7.1, configured with `--enable-gpl` (includes libx264, libx265, libmp3lame, libvpx, libaom and others). `ffmpeg -L` reports the GNU General Public License, version 2 or (at your option) any later version.
- x64 build: comes from the same wheel release for Intel Macs. Check its version and license with `ffmpeg -version` and `ffmpeg -L` before distributing.
- Source: FFmpeg 7.1 from https://ffmpeg.org/releases/ (https://git.ffmpeg.org/ffmpeg.git, tag `n7.1`). The macOS build scripts for the wheel binaries are at https://github.com/imageio/imageio-binaries and https://github.com/imageio/imageio-ffmpeg.
- Anyone distributing the DMG must provide the corresponding FFmpeg source (and that of its GPL libraries) as GPL requires, or ship a different FFmpeg build. As noted in `client/THIRD_PARTY_NOTICES.md`, the actual source, license and NOTICE of the binary in use must be verified for each release.

The project license is shipped separately as `LICENSE`. The original Olivia client and all original assets are excluded from the application package and remain the user's separately obtained files.
