"""The bundled PDF font has explicit provenance, glyphs and embedding support."""

import hashlib
import json
from pathlib import Path

from reportlab.pdfbase.ttfonts import TTFont

from app.common.config import settings
from app.services.export_pdf import FONT_NAME, register_font


def test_bundled_font_identity_and_chinese_glyphs():
    font_path = Path(settings.export_font_file)
    provenance = json.loads(font_path.with_name("provenance.json").read_text())
    assert hashlib.sha256(font_path.read_bytes()).hexdigest() == provenance["output_sha256"]
    assert provenance["license"] == "OFL-1.1"
    assert provenance["weight"] == 400 and provenance["subset"] is False
    assert "SIL OPEN FONT LICENSE" in font_path.with_name("OFL.txt").read_text()
    font = TTFont("PreviewGlyphCheck", str(font_path))
    assert all(ord(char) in font.face.charToGlyph for char in "成都旅行预算住宿￥元")
    register_font(font_path)
    assert FONT_NAME == "SinanSansSC"
