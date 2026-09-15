import io

from PIL import Image

from app.routers import survey_georeference


def test_digitize_preview_is_screen_sized_and_reports_source_dimensions(monkeypatch):
    source = Image.new("RGB", (4000, 2000), "white")
    source_bytes = io.BytesIO()
    source.save(source_bytes, format="PNG")
    row = {
        "source_object_key": "source.png",
        "source_content_type": "image/png",
    }

    monkeypatch.setattr(survey_georeference, "_load_session_row", lambda db, session_id: row)
    monkeypatch.setattr(survey_georeference, "_build_r2", lambda: {})
    monkeypatch.setattr(survey_georeference, "_download_r2_bytes", lambda settings, key: source_bytes.getvalue())
    monkeypatch.setattr(survey_georeference, "_touch_session", lambda db, session_id: None)

    response = survey_georeference.get_georeference_digitize_preview("session-id", db=object())

    preview = Image.open(io.BytesIO(response.body))
    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert preview.size == (3200, 1600)
    assert response.headers["x-source-width"] == "4000"
    assert response.headers["x-source-height"] == "2000"
