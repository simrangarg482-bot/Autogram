"""
Adversarial regression test: a résumé-upload widget (Filestack/Uploadcare-
style embed — a common real-world pattern for "Upload your résumé" on career
sites that don't build their own file picker) rendered in its OWN `<iframe>`,
separate from whichever frame holds the rest of the application form.

`resolve_form_root` (automation/browser/selectors.py) picks the frame with
the MOST form controls to scan the rest of the form against — correct, since
a normal page's incidental iframes (chat widgets, video embeds) must not win.
But an upload-widget iframe genuinely has only ~1 control (the file input
itself), so it can never win that comparison, and `form_root` never points at
it. Before the fix in `automation/ats/base.py::_find_resume_input`, this meant
the résumé was NEVER found or uploaded on any form shaped this way — 100%
failure regardless of a trigger button, since the trigger-and-retry path also
only ever looked back at `form_root`.

Reproduces with `ATSAdapter.upload_resume()` (not a lower-level selector call)
so this pins the actual code path a real run takes.
"""

from __future__ import annotations

import pytest

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.generic.generic_adapter import GenericAdapter

# The main form lives in one iframe (several ordinary fields — enough to win
# resolve_form_root's control-count comparison); the résumé upload widget is
# a SEPARATE iframe containing nothing but the file input.
MAIN_FORM_IFRAME_HTML = (
    "<label for=first>First name</label><input id=first>"
    "<label for=last>Last name</label><input id=last>"
    "<label for=email>Email</label><input id=email type=email>"
    "<label for=phone>Phone</label><input id=phone type=tel>"
    "<input id=consent type=checkbox>"
)
UPLOAD_WIDGET_IFRAME_HTML = "<input type=file id=resume-widget-input>"

PAGE_HTML = f"""
<html><body>
<iframe srcdoc="{MAIN_FORM_IFRAME_HTML}"></iframe>
<iframe srcdoc="{UPLOAD_WIDGET_IFRAME_HTML}"></iframe>
</body></html>
"""


def _profile() -> CandidateProfile:
    profile = CandidateProfile(
        profile_id="profile-1", user_id="user-1",
        first_name="Ada", last_name="Lovelace", email="ada@example.com",
    )
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _resume_document(path) -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=str(path),
        file_hash="abc123", is_default=True,
    )


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake resume bytes")
    return path


def test_uploads_into_a_resume_widget_that_lives_in_a_separate_iframe(page, resume_file):
    page.set_content(PAGE_HTML)
    adapter = GenericAdapter(page=page, profile=_profile(), resume_document=_resume_document(resume_file))

    # form_root resolves to the main-form iframe (5 controls) — NOT the
    # upload-widget iframe (1 control) — confirming this isn't just a
    # coincidental case where form_root already covers the widget.
    assert adapter.form_root is not page
    assert adapter.form_root.locator("input, select").count() == 5

    assert adapter.upload_resume() is True

    uploaded_count = None
    for frame in page.frames:
        try:
            uploaded_count = frame.locator("input[type=file]").evaluate(
                "el => el.files ? el.files.length : 0"
            )
        except Exception:
            continue
        if uploaded_count:
            break
    assert uploaded_count == 1
