<p align="center">
  <img src="assets/brand/png/chamo-icon-1024.png" width="128" alt="Chamo icon">
</p>

<h1 align="center">Chamo</h1>

<p align="center">
  Changes your files' format the way a chameleon changes colour.<br>
  A local file converter for PDF, Office/WPS and images, plus document preparation for RAG knowledge bases.
</p>

<p align="center">
  <a href="https://github.com/WangMingCF/chamo/releases/latest">Download</a> ·
  <a href="docs/USER_GUIDE.txt">User guide (Chinese)</a> ·
  <a href="docs/RELEASE_NOTES.txt">Release notes (Chinese)</a> ·
  <a href="README.md">中文</a>
</p>

<p align="center">
  <img src="assets/brand/png/chamo-mascot-1024.png" width="220" alt="Chamo mascot: a chameleon sitting on a document">
</p>

## Why Chamo

- **Your files never leave your computer.** Every conversion runs locally: no uploads, no network, no account.
- **Originals are always kept.** Results go to a separate place; name clashes get a new name, nothing is overwritten.
- **Drop anything, get suggestions.** Drag files onto the start page and Chamo lists the conversions that apply.
- **Fast, resumable batches.** Thousands of small files finish in seconds; large PDFs show per-page progress and reuse finished pages after an interruption.
- **Honest about changes.** Lossy operations are opt-in, and when a lossless result is not possible Chamo says so instead of quietly degrading.

## Download

Get the package for your platform from [Releases](https://github.com/WangMingCF/chamo/releases/latest):

| Platform | Package | Notes |
| --- | --- | --- |
| macOS (Apple Silicon, macOS 15+) | `Chamo-<version>-AppleSilicon.dmg` | Drag Chamo into Applications. Python, the PDF/image engines and LibreOffice are bundled |
| Windows 10/11 x64 | `chamo-cli_<version>_windows-x64.zip` | Unzip and run `chamo.cmd ui` to open the local interface |
| Debian/Ubuntu (x86_64, arm64) | `chamo-cli_<version>_amd64.deb` / `_arm64.deb` | Install, then run `chamo ui` |

On Windows and Linux the interface is a browser page served only on 127.0.0.1 with a random token. It drives the same engine and rules as the macOS app.

The macOS build is ad-hoc signed and not yet Developer ID signed or notarized; if macOS blocks the first launch, allow it under System Settings → Privacy & Security. The Windows package is unsigned.

## What it does

| Conversion | Details |
| --- | --- |
| PDF to images | PNG/JPEG with page ranges and DPI; a static export without the text layer or interactivity |
| Split / merge PDF | Splitting limits both page count and real file size; merging follows list order and refuses structures it cannot keep intact |
| Compress PDF | Lossless clean-up, or opt-in lossy resampling; encrypted and digitally signed PDFs are refused |
| Office to PDF / images | Word, PowerPoint, Excel and WPS documents rendered locally; check complex fonts and layouts |
| Images to PDF | Combined in order, original size or A4/A3/Letter with margins, never cropped |
| Convert / compress images | PNG/JPEG/WebP/TIFF/BMP output, HEIC and AVIF input; lossless PNG optimisation |
| RAG preparation | Keeps folder structure and original formats, skips audio/video, verifies every file before writing an upload manifest |

The interface is currently in Chinese. OCR, PDF to editable Office, long-image stitching, archive extraction and audio/video are on the [roadmap](docs/ROADMAP.txt).

## Privacy

Chamo collects no data. Update checks are off by default; Chamo only asks GitHub for the latest version number when you check manually or turn on automatic checks. "Feedback" opens a pre-filled GitHub issue in your browser for you to review; files, paths and logs are never attached automatically.

## Feedback

Questions and ideas are welcome in [Issues](https://github.com/WangMingCF/chamo/issues). Issues are public, so please leave out private material.

## Development

Building, testing and the project layout are described in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (Chinese).

## License

Chamo is released under the [MIT License](LICENSE). Bundled third-party components (LibreOffice, Python and its packages, and others) keep their own licenses; see [docs/LICENSES.txt](docs/LICENSES.txt) and [engine/THIRD_PARTY_NOTICES.md](engine/THIRD_PARTY_NOTICES.md).
