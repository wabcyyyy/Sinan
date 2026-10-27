"""生成 P3 自制匿名输入及人工编写参考字段；不运行视觉模型，不生成评价成绩。"""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/screenshots"


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else ImageFont.load_default(size=28)
    # 同一对匿名图片包含原文与可检查的矩形。quote 不包括订单号或个人信息。
    cases = {
        "guide": [
            ("name", "Musée du Louvre", "推荐 Musée du Louvre / 卢浮宫", "recommended"),
            ("city", "Paris", "Paris · 法国 FR", None),
            ("countryCode", "FR", "Paris · 法国 FR", None),
            ("note", "不去 Louvre-Lens", "不去 Louvre-Lens（不同城市与馆区）", None),
        ],
        "booking": [
            ("name", "Musée du Louvre", "预约 Musée du Louvre / 卢浮宫", "recommended"),
            ("date", "2026-10-25", "开始 2026-10-25 14:00", None),
            ("startTime", "14:00", "开始 2026-10-25 14:00", None),
            ("endDate", "2026-10-25", "结束 2026-10-25 16:00", None),
            ("endTime", "16:00", "结束 2026-10-25 16:00", None),
            ("timeZone", "Europe/Paris", "当地时区 Europe/Paris", None),
            ("amount", "22.00", "票面 EUR 22.00", None),
            ("currency", "EUR", "票面 EUR 22.00", None),
        ],
        "guide_kyoto": [
            ("name", "Kiyomizu-dera", "おすすめ 清水寺 / Kiyomizu-dera", "recommended"),
            ("city", "京都", "京都 · 日本 JP", None),
            ("countryCode", "JP", "京都 · 日本 JP", None),
        ],
        "transport": [
            ("name", "京都駅", "出発駅 京都駅 / Kyoto Station", "recommended"),
            ("date", "2026-10-25", "出発 2026-10-25 23:00", None),
            ("startTime", "23:00", "出発 2026-10-25 23:00", None),
            ("endDate", "2026-10-26", "到着 2026-10-26 06:00", None),
            ("endTime", "06:00", "到着 2026-10-26 06:00", None),
            ("timeZone", "Asia/Tokyo", "現地時間 Asia/Tokyo", None),
            ("amount", "1200", "券面 JPY 1200 · 非实际班次", None),
            ("currency", "JPY", "券面 JPY 1200 · 非实际班次", None),
        ],
    }
    for kind, rows in cases.items():
        image = Image.new("RGB", (1000, 760), "white")
        draw = ImageDraw.Draw(image)
        draw.text((35, 15), "SINAN · 自制匿名开发样例 / NOT A REAL ORDER", font=font, fill="black")
        quotes = list(dict.fromkeys(row[2] for row in rows))
        fields = []
        for key, value, quote, _ in rows:
            y = 100 + quotes.index(quote) * 75
            draw.text((40, y), quote, font=font, fill="black")
            fields.append(
                {
                    "key": key,
                    "value": value,
                    "quote": quote,
                    "region": {"x": 0.035, "y": y / 760, "width": 0.91, "height": 55 / 760},
                    "language": "fr/zh",
                }
            )
        payload = {
            "entries": [
                {
                    "kind": "place"
                    if kind.startswith("guide")
                    else "transport"
                    if kind == "transport"
                    else "reservation",
                    "polarity": "recommended",
                    "fields": fields,
                    "questions": ["推荐仅作候选，是否本次必去需用户确认"] if kind == "guide" else [],
                }
            ],
            "questions": [],
        }
        if kind == "guide":
            region = next(f["region"] for f in fields if f["key"] == "note")
            payload["entries"].append(
                {
                    "kind": "place",
                    "polarity": "excluded",
                    "fields": [
                        {
                            "key": "name",
                            "value": "Louvre-Lens",
                            "quote": "不去 Louvre-Lens（不同城市与馆区）",
                            "region": region,
                            "language": "zh/fr",
                        }
                    ],
                    "questions": ["此馆所在城市和国家待确认，不应当作巴黎卢浮宫"],
                }
            )
        image.save(ROOT / f"{kind}.png")
        (ROOT / f"{kind}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
