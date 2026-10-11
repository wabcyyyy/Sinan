"""Build the committed static PDF font from a hash-pinned official Noto source."""

import argparse
import hashlib
import json
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

REVISION = "f8d157532fbfaeda587e826d4cd5b21a49186f7c"
SOURCE_SHA256 = "d68bafcb48a2707749396aa12bbbd833cb70401f3a9a689fd2902c7e0d295964"
SOURCE_PATH = "Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf"


def build(source: Path, output: Path) -> None:
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("Noto source SHA256 mismatch; refusing unpinned font")
    font = TTFont(source, recalcTimestamp=False)
    instance = instantiateVariableFont(font, {axis.axisTag: 400 for axis in font["fvar"].axes}, inplace=False)
    names = {
        1: "Sinan Sans SC",
        2: "Regular",
        3: "SinanSansSC-Regular",
        4: "Sinan Sans SC Regular",
        6: "SinanSansSC-Regular",
        16: "Sinan Sans SC",
        17: "Regular",
    }
    for record in instance["name"].names:
        if record.nameID in names:
            record.string = names[record.nameID].encode(record.getEncoding())
    instance.save(output)
    manifest = {
        "source": f"https://github.com/notofonts/noto-cjk/blob/{REVISION}/{SOURCE_PATH}",
        "revision": REVISION,
        "source_sha256": SOURCE_SHA256,
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "license": "OFL-1.1",
        "weight": 400,
        "subset": False,
        "fonttools_version": __import__("fontTools").__version__,
        "command": "uv run python scripts/build_preview_font.py SOURCE.ttf",
    }
    output.with_name("provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    build(args.source, Path(__file__).resolve().parents[1] / "app/resources/fonts/SinanSansSC-Regular.ttf")
