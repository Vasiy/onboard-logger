# Third-party notices

## 5am_util

`app/kline/fw_codec.py` (and the firmware write/read sequence in `docs/PROTOCOL.md`) follow
`5am_util` by DoI, <https://codeberg.org/DoI/5am_util> (originally github.com/denandz/5am_util).
The image encoding, the checksum and the security-access arithmetic are ported from its `main.c`,
and `tests/test_fw_codec.py` pins them to vectors produced by that code.

`app/kline/hexdump.py` reproduces the *layout* of the util's `dump_hex` (16 bytes per line, a gap
after eight, `|  <ascii> `). The util's version is taken from
<https://gist.github.com/ccbrown/9722406>, which states no licence; the Python function was
written independently from that output format and pinned to lines of the board's logs, no code
was copied.

```
BSD 3-Clause License

Copyright (c) 2018, DoI
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
