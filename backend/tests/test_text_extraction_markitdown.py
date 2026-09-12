"""
MarkItDown is the primary extraction engine (app/services/text_extraction.py);
the pre-existing pdfplumber/python-docx extractors are the fallback when it's
absent or comes back too short. MarkItDown may or may not be installed in any
given environment, so these tests fake the import rather than depending on
the real package.
"""

import sys
import types
from unittest.mock import patch

from app.services import text_extraction as te


def _fake_markitdown_module(text: str | None = "some text", raise_on_convert: bool = False):
    module = types.ModuleType("markitdown")

    class FakeResult:
        def __init__(self, text_content):
            self.text_content = text_content

    class FakeMarkItDown:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, path):
            if raise_on_convert:
                raise RuntimeError("boom")
            return FakeResult(text)

    module.MarkItDown = FakeMarkItDown
    return module


def test_pdf_uses_markitdown_output_when_long_enough(tmp_path):
    long_text = "Experienced engineer. " * 10
    fake_path = str(tmp_path / "resume.pdf")
    with patch.dict(sys.modules, {"markitdown": _fake_markitdown_module(long_text)}):
        with patch.object(te, "extract_text_from_pdf") as legacy:
            result = te._extract_pdf(fake_path)
    legacy.assert_not_called()
    assert result == long_text


def test_pdf_falls_back_to_legacy_when_markitdown_not_installed(tmp_path):
    fake_path = str(tmp_path / "resume.pdf")
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "markitdown":
            raise ImportError("no module named markitdown")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with patch.object(te, "extract_text_from_pdf", return_value="legacy text") as legacy:
            result = te._extract_pdf(fake_path)
    legacy.assert_called_once_with(fake_path)
    assert result == "legacy text"


def test_pdf_falls_back_to_legacy_when_markitdown_output_too_short(tmp_path):
    fake_path = str(tmp_path / "resume.pdf")
    with patch.dict(sys.modules, {"markitdown": _fake_markitdown_module("hi")}):
        with patch.object(te, "extract_text_from_pdf", return_value="legacy text long enough") as legacy:
            result = te._extract_pdf(fake_path)
    legacy.assert_called_once_with(fake_path)
    assert result == "legacy text long enough"


def test_pdf_falls_back_to_legacy_when_markitdown_raises(tmp_path):
    fake_path = str(tmp_path / "resume.pdf")
    with patch.dict(sys.modules, {"markitdown": _fake_markitdown_module(raise_on_convert=True)}):
        with patch.object(te, "extract_text_from_pdf", return_value="legacy text") as legacy:
            result = te._extract_pdf(fake_path)
    legacy.assert_called_once_with(fake_path)
    assert result == "legacy text"


def test_docx_uses_markitdown_output_when_long_enough(tmp_path):
    long_text = "Experienced engineer. " * 10
    fake_path = str(tmp_path / "resume.docx")
    with patch.dict(sys.modules, {"markitdown": _fake_markitdown_module(long_text)}):
        with patch.object(te, "extract_text_from_docx") as legacy:
            result = te._extract_docx(fake_path)
    legacy.assert_not_called()
    assert result == long_text
