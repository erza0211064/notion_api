"""Build a printable travel booklet from a Notion CSV export.

Export the Notion page as Markdown & CSV, unzip it beside this script, then run
``python main.py``. Microsoft Edge is used in headless print mode to create PDF.
"""

from __future__ import annotations

import csv
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXPORT_PREFIX = "d7634390-a666-42c1-b490-91ee6f059f52_ExportBlock-"
PDF_PATH = ROOT / "Dolomites_Travel_Booklet_2026.pdf"
HTML_PATH = ROOT / "Dolomites_Travel_Booklet_2026.html"


def find_csv(pattern: str) -> Path:
    export_dirs = [path for path in ROOT.iterdir() if path.is_dir() and path.name.startswith(EXPORT_PREFIX)]
    if not export_dirs:
        raise FileNotFoundError("找不到解壓縮後的 Notion 匯出資料夾")
    export_dir = export_dirs[0]
    # Windows' filesystem glob can be surprisingly strict about mixed case;
    # compare the ASCII stem without case sensitivity instead.
    token = next(part for part in pattern.split("*") if part).lower()
    matches = [path for path in export_dir.glob("*.csv") if token in path.name.lower()]
    # The export contains both database source CSVs (with every property) and
    # view CSVs (which omit fields such as day_no and itin_day_no). Prefer the
    # complete database export whenever more than one file has the same name.
    matches.sort(key=lambda path: not path.name.lower().endswith("_all.csv"))
    if not matches:
        raise FileNotFoundError(f"找不到匯出 CSV：{pattern}（請先解壓縮 Notion 匯出檔）")
    return matches[0]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def esc(value: object) -> str:
    return html.escape(str(value or "").strip(), quote=True)


def linked_text(value: str) -> str:
    """Turn Notion CSV relation values into readable names, not pasted URLs."""
    names = re.findall(r"([^,]+?)\s*\(https?://[^)]+\)", value or "")
    if names:
        return "、".join(esc(name.strip()) for name in names)
    return esc(value)


def time_key(row: dict[str, str]) -> tuple[int, str]:
    try:
        day = int(float(row.get("itin_day_no", "0") or 0))
    except ValueError:
        day = 999
    return day, row.get("itin_time", "")


def make_booklet() -> str:
    trip_rows = read_csv(find_csv("*Trip overview*all.csv"))
    day_rows = read_csv(find_csv("*Day overview*all.csv"))
    itinerary = read_csv(find_csv("*Itinerary*all.csv"))
    if not trip_rows:
        raise ValueError("行程總覽 CSV 沒有資料")
    trip = trip_rows[0]
    activities: dict[int, list[dict[str, str]]] = {}
    for item in itinerary:
        try:
            day_no = int(float(item.get("itin_day_no", "0") or 0))
        except ValueError:
            continue
        if day_no > 0 and item.get("itin_item", "").strip():
            activities.setdefault(day_no, []).append(item)
    for items in activities.values():
        items.sort(key=lambda x: x.get("itin_time", ""))

    days = []
    for day in sorted(day_rows, key=lambda row: int(row.get("day_no") or 999)):
        try:
            day_no = int(day.get("day_no") or 0)
        except ValueError:
            continue
        if day_no <= 0:
            continue
        # The template pre-creates 30 Day Overview rows. Empty placeholders
        # should not become blank pages in the finished booklet.
        has_day_content = day_no in activities or any(
            day.get(key, "").strip()
            for key in (
                "day_description", "day_date", "day_dis_date", "day_breakfast",
                "day_lunch", "day_dinner", "day_hotel", "day_image",
            )
        )
        if not has_day_content:
            continue
        plans = []
        for item in activities.get(day_no, []):
            time = esc(item.get("itin_time"))
            name = esc(item.get("itin_item"))
            details = esc(item.get("spot_description"))
            location = esc(item.get("spot_location"))
            meta = " · ".join(part for part in (location,) if part)
            plans.append(
                '<li class="event"><div class="event-time">'
                f'{time or "行程"}</div><div><h3>{name}</h3>'
                f'{f"<p>{details}</p>" if details else ""}'
                f'{f"<small>{meta}</small>" if meta else ""}</div></li>'
            )
        meals = []
        for label, key in (("早餐", "day_breakfast"), ("午餐", "day_lunch"), ("晚餐", "day_dinner"), ("住宿", "day_hotel")):
            value = linked_text(day.get(key, ""))
            if value:
                meals.append(f'<div><b>{label}</b><span>{value}</span></div>')
        title = esc(day.get("day_title") or f"Day {day_no:02d}")
        date = esc(day.get("day_dis_date") or day.get("day_date"))
        description = esc(day.get("day_description"))
        days.append(
            f'<section class="day"><header class="day-header"><div class="eyebrow">DAY {day_no:02d}'
            f'{f"　·　{date}" if date else ""}</div><h2>{title}</h2>'
            f'{f"<p>{description}</p>" if description else ""}</header>'
            f'<ol class="timeline">{"".join(plans) or "<li>尚未安排活動</li>"}</ol>'
            f'{f"<footer class=\"day-footer\">{"".join(meals)}</footer>" if meals else ""}</section>'
        )

    title = esc(trip.get("trip_title"))
    subtitle = esc(trip.get("trip_subtitle"))
    duration = esc(trip.get("trip_duration"))
    destination = " · ".join(filter(None, (esc(trip.get("trip_country")), esc(trip.get("trip_region")))))
    return f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
@page {{ size:A5; margin:15mm 14mm 17mm; @bottom-center {{ content: counter(page); color:#82908c; font:9pt sans-serif; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; color:#253b36; font-family:"Microsoft JhengHei","Noto Sans CJK TC",sans-serif; font-size:10pt; line-height:1.65 }}
.cover {{ min-height:178mm; display:flex; flex-direction:column; justify-content:center; border-top:5px solid #7f9a82; padding:12mm 2mm }}
.cover .eyebrow,.eyebrow {{ color:#748b77; font-size:9pt; letter-spacing:.12em; font-weight:700 }}
h1 {{ font-family:serif; font-size:29pt; line-height:1.3; margin:9mm 0 3mm; color:#263e36 }}
.subtitle {{ font-size:14pt; color:#61756a }} .destination {{ margin-top:16mm; color:#75857e }}
.intro {{ margin-top:9mm; max-width:95%; color:#60736b }} .section-title {{ font:20pt serif; margin:0 0 8mm }}
.day {{ page-break-before:always; break-before:page }} .day-header {{ border-bottom:1px solid #cdd7cf; padding-bottom:5mm; margin-bottom:5mm }}
.day-header h2 {{ font:22pt serif; margin:2mm 0 }} .day-header p {{ color:#61756a; margin:2mm 0 0 }}
.timeline {{ list-style:none; padding:0; margin:0 }} .event {{ display:grid; grid-template-columns:16mm 1fr; gap:3mm; padding:3.5mm 0; border-bottom:1px solid #e7ece8; break-inside:avoid }}
.event-time {{ color:#718a76; font-weight:700; font-size:9pt; padding-top:1mm }} .event h3 {{ font-size:11pt; margin:0; color:#2e443b }}
.event p {{ margin:1mm 0; color:#586c64 }} .event small {{ color:#89958f }}
.day-footer {{ margin-top:7mm; padding:4mm; background:#f1f4ef; border-radius:3mm; break-inside:avoid }}
.day-footer div {{ display:grid; grid-template-columns:14mm 1fr; gap:2mm; margin:1.3mm 0 }} .day-footer b {{ color:#718a76 }}
</style></head><body>
<section class="cover"><div class="eyebrow">TRAVEL JOURNAL　/　2026</div><h1>{title}</h1>
<div class="subtitle">{subtitle}</div><div class="destination">{destination}{f"　·　{duration}" if duration else ""}</div>
<p class="intro">旅程小冊子<br>每日行程、景點資訊與餐宿安排</p></section>
<section class="day"><div class="eyebrow">ITINERARY</div><h2 class="section-title">每日行程</h2>
<p>{destination}{f"　·　{duration}" if duration else ""}</p><p>共 {len(days)} 天行程</p></section>
{"".join(days)}</body></html>'''


def main() -> None:
    html_doc = make_booklet()
    HTML_PATH.write_text(html_doc, encoding="utf-8")
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(edge).exists():
        print(f"已產生 HTML：{HTML_PATH}\n找不到 Microsoft Edge，請用瀏覽器開啟後列印成 PDF。")
        return
    result = subprocess.run(
        [edge, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={PDF_PATH}", HTML_PATH.as_uri()],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode or not PDF_PATH.exists() or PDF_PATH.stat().st_size < 1000:
        detail = result.stderr.strip() or result.stdout.strip() or "Edge 沒有輸出 PDF"
        raise RuntimeError(detail)
    print(f"完成：{PDF_PATH}\nHTML 預覽：{HTML_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"產生失敗：{error}", file=sys.stderr)
        raise SystemExit(1)
