"""Build a printable travel booklet directly from Notion via API.

No more CSV export / unzip. Images uploaded to Notion are downloaded
through signed URLs at runtime, so newly added photos just work.

Setup (one time):
  1. Create an internal integration: https://www.notion.so/my-integrations
     -> "New integration", copy the "Internal Integration Secret".
  2. In Notion, open your travel page -> Share -> add the integration
     (grant it access to the page; databases inside are included).
  3. pip install "notion-client>=2.2" pillow
  4. Set the env var NOTION_TOKEN to the integration secret.
     Windows (cmd):  setx NOTION_TOKEN "ntn_xxx..."
     macOS/Linux:    export NOTION_TOKEN="ntn_xxx..."
  5. python main.py

Microsoft Edge is used in headless print mode to create the PDF.
"""

from __future__ import annotations

import base64
import html
import os
import shutil
import subprocess
import sys
import urllib.request
from io import BytesIO
from pathlib import Path

try:
    from notion_client import Client
except ImportError:  # pragma: no cover
    Client = None

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


ROOT = Path(__file__).resolve().parent
PDF_PATH = ROOT / "Dolomites_Travel_Booklet_2026.pdf"
HTML_PATH = ROOT / "Dolomites_Travel_Booklet_2026.html"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

# Data source IDs of the TravelBooklet Planner template.
# Find yours: open the "DB of TravelBooklet Planner" page in Notion,
# each database link's URL ends with its page ID; the data source ID
# is resolved from it (or just keep these if you use the same template).
DATA_SOURCES = {
    "trip": "63654aaf-45f0-8203-b514-0794c442c1ac",
    "day": "4c454aaf-45f0-8335-a74d-077d627ab872",
    "itinerary": "b3c54aaf-45f0-83b4-869b-07528ecce1d1",
    "food": "83954aaf-45f0-82ab-89e5-075b38a68de8",
    "stay": "1f654aaf-45f0-8220-ba56-878cf0576da9",
    "traffic": "25254aaf-45f0-823e-b2ba-072de0fd17e3",
}


# --------------------------------------------------------------------------
# Notion property helpers
# --------------------------------------------------------------------------

def _prop(page: dict, name: str) -> dict | None:
    props = page.get("properties") or {}
    return props.get(name)


def text_of(page: dict, name: str) -> str:
    """Read a title/rich_text/select/number/date/formula property as text."""
    p = _prop(page, name)
    if not p:
        return ""
    kind = p.get("type")
    if kind == "title":
        return "".join(x.get("plain_text", "") for x in p.get("title", [])).strip()
    if kind == "rich_text":
        return "".join(x.get("plain_text", "") for x in p.get("rich_text", [])).strip()
    if kind == "select":
        sel = p.get("select") or {}
        return sel.get("name", "") or ""
    if kind == "multi_select":
        return "、".join(x.get("name", "") for x in p.get("multi_select", []))
    if kind == "number":
        v = p.get("number")
        if v is None:
            return ""
        return str(int(v) if float(v).is_integer() else v)
    if kind == "date":
        d = p.get("date") or {}
        return d.get("start", "") or ""
    if kind == "formula":
        f = p.get("formula") or {}
        v = f.get(f.get("type"))
        return "" if v is None else str(v)
    if kind == "rollup":
        r = p.get("rollup") or {}
        v = r.get(r.get("type"))
        if isinstance(v, list):
            return "、".join(str(x) for x in v)
        return "" if v is None else str(v)
    if kind in ("url", "email", "phone_number"):
        return p.get(kind) or ""
    if kind == "checkbox":
        return "是" if p.get("checkbox") else ""
    return ""


def file_urls(page: dict, name: str) -> list[tuple[str, str]]:
    """Return [(filename, download_url)] for a files property.

    Notion signs these URLs and they expire quickly (minutes), so the
    caller must download immediately.
    """
    p = _prop(page, name)
    if not p or p.get("type") != "files":
        return []
    out: list[tuple[str, str]] = []
    for f in p.get("files", []):
        fname = f.get("name", "image")
        if f.get("type") == "file":
            out.append((fname, f["file"]["url"]))
        elif f.get("type") == "external":
            out.append((fname, f["external"]["url"]))
    return out


_title_cache: dict[str, str] = {}


def page_title(client: "Client", page_id: str) -> str:
    """Resolve any page's title (used for relation properties like meals)."""
    if page_id in _title_cache:
        return _title_cache[page_id]
    title = ""
    try:
        pg = client.pages.retrieve(page_id=page_id)
        for prop in (pg.get("properties") or {}).values():
            if prop.get("type") == "title":
                title = "".join(
                    x.get("plain_text", "") for x in prop.get("title", [])
                ).strip()
                break
    except Exception:
        title = ""
    _title_cache[page_id] = title
    return title


def relation_titles(client: "Client", page: dict, name: str) -> list[str]:
    p = _prop(page, name)
    if not p or p.get("type") != "relation":
        return []
    return [
        t for t in (page_title(client, r["id"]) for r in p.get("relation", [])) if t
    ]


def query_all(client: "Client", data_source_id: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        resp = client.data_sources.query(
            data_source_id=data_source_id, start_cursor=cursor, page_size=100
        )
        rows.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return rows


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def download_image(url: str, max_width: int = 1200) -> str:
    """Download an image URL now (signed URLs expire fast) and return a
    base64 data URI, compressed with PIL when available."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        if HAS_PIL:
            img = Image.open(BytesIO(raw))
            if img.width > max_width:
                img = img.resize(
                    (max_width, int(img.height * max_width / img.width))
                )
            buf = BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=82)
            raw, mime = buf.getvalue(), "image/jpeg"
        else:
            mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode()
    except Exception:
        return ""


def hero_for_day(client_pages_unused, day: dict, items: list[dict]) -> str:
    """Pick one hero image for a day: day_image first, then the first
    itinerary item with a downloadable image."""
    for _, url in file_urls(day, "day_image"):
        uri = download_image(url)
        if uri:
            return uri
    for item in items:
        for _, url in file_urls(item, "spot_image"):
            uri = download_image(url)
            if uri:
                return uri
    return ""


# --------------------------------------------------------------------------
# Booklet
# --------------------------------------------------------------------------

def esc(value: object) -> str:
    return html.escape(str(value or "").strip(), quote=True)


def num(page: dict, name: str, default: int = 0) -> int:
    try:
        return int(float(text_of(page, name) or default))
    except (ValueError, TypeError):
        return default


def make_booklet(client: "Client") -> str:
    trip_rows = query_all(client, DATA_SOURCES["trip"])
    day_rows = query_all(client, DATA_SOURCES["day"])
    itinerary = query_all(client, DATA_SOURCES["itinerary"])
    if not trip_rows:
        raise ValueError("行程總覽資料庫沒有資料（請確認 integration 有權限）")
    trip = trip_rows[0]

    activities: dict[int, list[dict]] = {}
    for item in itinerary:
        day_no = num(item, "itin_day_no", 999)
        if day_no > 0 and text_of(item, "itin_item"):
            activities.setdefault(day_no, []).append(item)
    for items in activities.values():
        items.sort(key=lambda x: text_of(x, "itin_time"))

    days = []
    for day in sorted(day_rows, key=lambda d: num(d, "day_no", 999)):
        day_no = num(day, "day_no")
        if day_no <= 0:
            continue
        # The template pre-creates placeholder Day rows; skip the empties.
        has_day_content = day_no in activities or any(
            text_of(day, key)
            for key in (
                "day_description", "day_date", "day_dis_date", "day_breakfast",
                "day_lunch", "day_dinner", "day_hotel", "day_image",
            )
        )
        if not has_day_content:
            continue
        items = activities.get(day_no, [])
        hero = hero_for_day(None, day, items)
        hero_html = (
            f'<div class="day-hero"><img src="{hero}" alt=""></div>' if hero else ""
        )
        plans = []
        for item in items:
            time = esc(text_of(item, "itin_time"))
            name = esc(text_of(item, "itin_item"))
            details = esc(text_of(item, "spot_description"))
            location = esc(text_of(item, "spot_location"))
            plans.append(
                '<li class="event"><div class="event-time">'
                f'{time or "行程"}</div><div><h3>{name}</h3>'
                f'{f"<p>{details}</p>" if details else ""}'
                f'{f"<small>{location}</small>" if location else ""}</div></li>'
            )
        meals = []
        for label, key in (
            ("早餐", "day_breakfast"),
            ("午餐", "day_lunch"),
            ("晚餐", "day_dinner"),
            ("住宿", "day_hotel"),
        ):
            value = "、".join(relation_titles(client, day, key))
            if value:
                meals.append(f"<div><b>{label}</b><span>{esc(value)}</span></div>")
        title = esc(text_of(day, "day_title") or f"Day {day_no:02d}")
        date = esc(text_of(day, "day_dis_date") or text_of(day, "day_date"))
        description = esc(text_of(day, "day_description"))
        days.append(
            f'<section class="day"><header class="day-header">'
            f'<div class="eyebrow">DAY {day_no:02d}'
            f'{f"　·　{date}" if date else ""}</div><h2>{title}</h2>'
            f'{f"<p>{description}</p>" if description else ""}</header>'
            f"{hero_html}"
            f'<ol class="timeline">{"".join(plans) or "<li>尚未安排活動</li>"}</ol>'
            f'{f"<footer class=\"day-footer\">{"".join(meals)}</footer>" if meals else ""}'
            f"</section>"
        )

    title = esc(text_of(trip, "trip_title"))
    subtitle = esc(text_of(trip, "trip_subtitle"))
    duration = esc(text_of(trip, "trip_duration"))
    destination = " · ".join(
        filter(None, (esc(text_of(trip, "trip_country")),
                      esc(text_of(trip, "trip_region"))))
    )
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
.day-hero {{ margin:0 0 5mm }} .day-hero img {{ width:100%; border-radius:3mm; display:block }}
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
    if Client is None:
        print(
            "找不到 notion-client，請先執行：pip install \"notion-client>=2.2\" pillow",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not NOTION_TOKEN:
        print(
            "找不到 NOTION_TOKEN 環境變數。\n"
            "請到 https://www.notion.so/my-integrations 建立 internal integration，\n"
            "把 secret 設成環境變數 NOTION_TOKEN 後再跑。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    client = Client(auth=NOTION_TOKEN)
    html_doc = make_booklet(client)
    HTML_PATH.write_text(html_doc, encoding="utf-8")
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(edge).exists():
        print(f"已產生 HTML：{HTML_PATH}\n找不到 Microsoft Edge，請用瀏覽器開啟後列印成 PDF。")
        return
    result = subprocess.run(
        [edge, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={PDF_PATH}", HTML_PATH.as_uri()],
        capture_output=True, text=True, timeout=180,
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
