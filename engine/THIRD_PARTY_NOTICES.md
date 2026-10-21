# Third-party attribution

`src/cloud_kb_pack/office.py` adapts LibreOffice discovery and isolated-profile
conversion from Tencent/WeKnora `docreader/parser/excel_convert.py`, commit
`3e22822c1d2397be730c458c7539d0eb8c113a24`. The path-oriented API, validation,
configuration, and CLI are modifications for this tool.

Copyright (C) 2026 Tencent. All rights reserved.

MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Runtime packages (not vendored source):

- pypdfium2: Apache-2.0 / BSD-3-Clause; includes PDFium and its third-party notices.
- Pillow 12.3.0: MIT-CMU and accompanying third-party notices.
- openpyxl: MIT.
- LibreOffice and FFmpeg: external executables; retain their own licenses.

See installed dependency distributions for complete attribution and licenses.
