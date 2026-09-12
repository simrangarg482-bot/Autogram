"""
Phase 3 — `extract_text` reads through `StorageBackend.local_path()` rather
than assuming `stored_path` already IS a real filesystem path
(`app/services/storage/base.py`'s module docstring). pdfplumber/python-docx
both need an actual file on disk, so this confirms extraction still works
against a plain local path (unchanged behavior) AND against an `s3://`
locator (the previously-unrouted gap this phase closes).
"""

from __future__ import annotations

import io

import docx
from moto import mock_aws

from app.services.text_extraction import extract_text

_BODY = "Experienced backend engineer with ten years building APIs and services."


def _docx_bytes(paragraph: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(paragraph)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_extract_text_from_a_local_docx_path(tmp_path):
    path = tmp_path / "resume.docx"
    path.write_bytes(_docx_bytes(_BODY))

    text = extract_text(str(path))

    assert "backend engineer" in text.lower()


def test_extract_text_reads_an_s3_stored_docx_through_local_path(monkeypatch):
    with mock_aws():
        import boto3

        from app.services.storage.s3_backend import S3StorageBackend

        bucket = "autogram-test-bucket"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        backend = S3StorageBackend(bucket=bucket, region="us-east-1")
        monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: backend)

        locator = backend.save("documents/resume/test.docx", _docx_bytes(_BODY))

        text = extract_text(locator)

    assert "backend engineer" in text.lower()
