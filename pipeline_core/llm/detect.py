"""Structural early-stop over a content stream.

The model is done as soon as the structure it was asked for is closed:
  * a ``mod gen_* { ... }`` block returns to brace-depth 0, or
  * every ``<FILE ...>`` has a matching ``</FILE>``.

Stopping there saves the entire trailing-token budget (the model otherwise keeps
emitting echoes / alternatives). This is a pure incremental detector — feed it the
NON-reasoning content pieces in order; it returns True the moment a structure closes.
Verbatim transcription of the per-backend brace/FILE loops it replaces.
"""

from __future__ import annotations


class StopDetector:
    __slots__ = ("_cbuf", "_blk_tracking", "_blk_depth", "_file_opens", "_file_closes", "reason")

    def __init__(self) -> None:
        self._cbuf = ""              # accumulated content seen so far
        self._blk_tracking = False   # have we entered a `mod gen_* {` block yet
        self._blk_depth = 0
        self._file_opens = 0
        self._file_closes = 0
        self.reason: str | None = None  # set to "block"/"file" when a stop fires

    def feed(self, text: str) -> bool:
        """Return True if a structure just closed and streaming should stop."""
        if not text:
            return False
        self._cbuf += text
        # mod gen_* block: brace-balance from the opening `{` after `mod gen_`.
        if not self._blk_tracking:
            _mg = self._cbuf.find("mod gen_")
            if _mg != -1:
                _ob = self._cbuf.find("{", _mg)
                if _ob != -1:
                    self._blk_tracking = True
                    for _ch in self._cbuf[_ob:]:
                        if _ch == "{":
                            self._blk_depth += 1
                        elif _ch == "}":
                            self._blk_depth -= 1
                    if self._blk_depth <= 0:
                        self.reason = "block"
                        return True
        else:
            for _ch in text:
                if _ch == "{":
                    self._blk_depth += 1
                elif _ch == "}":
                    self._blk_depth -= 1
            if self._blk_depth <= 0:
                self.reason = "block"
                return True
        # All <FILE ...>...</FILE> blocks closed.
        self._file_opens += text.count("<FILE ")
        self._file_closes += text.count("</FILE>")
        if self._file_opens > 0 and self._file_opens == self._file_closes:
            self.reason = "file"
            return True
        return False

    @property
    def chars(self) -> int:
        return len(self._cbuf)
