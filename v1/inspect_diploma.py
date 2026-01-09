#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
inspect_diploma.py (SVG-free)

Сканирует HTML-шаблон диплома и делает отчёт:
1) Поля-шаблоны вида {{ ... }}
2) Жёстко прописанный текст (обычные текстовые узлы)
3) Картинки:
   - <img src="..."> (включая data:image/...;base64,...)
   - background-image: url(...) (включая data:image/...;base64,...)

Запуск:
  python inspect_diploma.py diploma.html

Результат:
  - diploma_fields_report.html
  - папка extracted_images/ с извлечёнными картинками
"""

import re
import base64
import mimetypes
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString, Comment

# -------- настройки --------
INPUT_HTML = "diploma.html"
OUT_DIR = "extracted_images"
REPORT_HTML = "diploma_fields_report.html"

# если строка очень длинная (CSS/base64), считаем её служебной и отбрасываем
MAX_TEXT_LEN = 400

TEMPLATE_FIELD_RE = re.compile(r"{{\s*([^}]+?)\s*}}")
DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)
URL_IN_CSS_RE = re.compile(r'url\(\s*["\']?(.*?)["\']?\s*\)', re.IGNORECASE)


# -------- утилиты --------
def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def looks_like_service_text(s: str) -> bool:
    if not s:
        return True
    if len(s) > MAX_TEXT_LEN:
        return True
    if "{% " in s or "%}" in s:
        return True
    # явный CSS/служебка
    if s.startswith(":root") or "font-family:" in s or "box-sizing:" in s:
        return True
    # base64 мусор как текст
    if "data:image" in s and "base64" in s:
        return True
    return False

def guess_mime_from_path(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"

def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )

def rel_to_report(path: Path, report_path: Path) -> str:
    try:
        return str(path.resolve().relative_to(report_path.parent.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


# -------- data-url -> файл (возвращаем реальный путь с расширением) --------
def save_data_url(data_url: str, out_path_no_ext: Path):
    """
    Сохраняет data:*;base64,... в файл.
    Возвращает (mime, saved_path: Path) или ("", None)
    """
    m = DATA_URL_RE.match(data_url.strip())
    if not m:
        return "", None

    mime = m.group(1).strip()
    b64 = m.group(2).strip()

    ext = mimetypes.guess_extension(mime) or ""
    if ext == ".jpe":
        ext = ".jpg"

    saved_path = out_path_no_ext
    if saved_path.suffix == "":
        saved_path = saved_path.with_suffix(ext or ".bin")

    raw = base64.b64decode(b64)
    saved_path.write_bytes(raw)

    return mime, saved_path


# -------- “жёсткий” текст --------
def extract_hard_texts(soup: BeautifulSoup):
    hard_texts = []
    seen = set()

    body = soup.body if soup.body else soup

    SKIP_PARENTS = {"style", "script", "head", "title", "meta", "link"}

    for node in body.descendants:
        if isinstance(node, Comment):
            continue
        if not isinstance(node, NavigableString):
            continue

        parent = node.parent.name.lower() if node.parent and node.parent.name else ""
        if parent in SKIP_PARENTS:
            continue

        text = clean_text(str(node))
        if looks_like_service_text(text):
            continue
        if len(text) < 2:
            continue

        if text not in seen:
            seen.add(text)
            hard_texts.append(text)

    return hard_texts


def collect_urls_from_css(css_text: str):
    urls = []
    for m in URL_IN_CSS_RE.finditer(css_text or ""):
        u = (m.group(1) or "").strip()
        if u:
            urls.append(u)
    return urls


# -------- основная логика --------
def main():
    src = Path(INPUT_HTML)
    if not src.exists():
        raise SystemExit(f"Не найден файл: {src.resolve()}")

    html = src.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    out_dir = Path(OUT_DIR)
    ensure_dir(out_dir)

    report_path = Path(REPORT_HTML)

    # 1) поля-шаблоны {{ ... }}
    template_fields = []
    for m in TEMPLATE_FIELD_RE.finditer(html):
        field = m.group(1).strip()
        if field and field not in template_fields:
            template_fields.append(field)

    # 2) жесткий текст
    hard_texts = extract_hard_texts(soup)

    # 3) картинки
    extracted = []  # entries: {kind, mime, path, note, preview_type}

    img_counter = 0

    # 3.1 IMG tags
    for img in soup.find_all("img"):
        src_attr = (img.get("src") or "").strip()
        if not src_attr:
            continue

        img_counter += 1

        if src_attr.startswith("data:"):
            out_no_ext = out_dir / f"img_{img_counter:03d}"
            mime, saved_path = save_data_url(src_attr, out_no_ext)
            if saved_path:
                extracted.append({
                    "kind": "<img src=…>",
                    "mime": mime or "unknown",
                    "path": saved_path,
                    "note": "data-url",
                    "preview_type": "img",
                })
        else:
            extracted.append({
                "kind": "<img src=…>",
                "mime": guess_mime_from_path(src_attr),
                "path": src_attr,
                "note": "external/path",
                "preview_type": "none",
            })

    # 3.2 background-image urls (inline styles)
    for tag in soup.find_all(style=True):
        css = tag.get("style") or ""
        for u in collect_urls_from_css(css):
            if u.startswith("data:"):
                img_counter += 1
                out_no_ext = out_dir / f"img_{img_counter:03d}"
                mime, saved_path = save_data_url(u, out_no_ext)
                if saved_path:
                    extracted.append({
                        "kind": "background-image: url(…)",
                        "mime": mime or "unknown",
                        "path": saved_path,
                        "note": "data-url",
                        "preview_type": "img",
                    })
            else:
                extracted.append({
                    "kind": "background-image: url(…)",
                    "mime": guess_mime_from_path(u),
                    "path": u,
                    "note": "external/path",
                    "preview_type": "none",
                })

    # 3.3 background-image urls (<style> blocks)
    for style_tag in soup.find_all("style"):
        css = style_tag.get_text() or ""
        for u in collect_urls_from_css(css):
            if u.startswith("data:"):
                img_counter += 1
                out_no_ext = out_dir / f"img_{img_counter:03d}"
                mime, saved_path = save_data_url(u, out_no_ext)
                if saved_path:
                    extracted.append({
                        "kind": "background-image: url(…)",
                        "mime": mime or "unknown",
                        "path": saved_path,
                        "note": "data-url",
                        "preview_type": "img",
                    })
            else:
                extracted.append({
                    "kind": "background-image: url(…)",
                    "mime": guess_mime_from_path(u),
                    "path": u,
                    "note": "external/path",
                    "preview_type": "none",
                })

    # -------- генерация отчёта --------
    report = []
    report.append("<!doctype html>")
    report.append("<html><head><meta charset='utf-8'>")
    report.append("<title>Diploma fields report</title>")
    report.append("""
<style>
  body { font-family: Arial, sans-serif; margin: 24px; }
  h2 { margin-top: 28px; }
  ul { line-height: 1.6; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
  .card { border: 1px solid #ddd; border-radius: 12px; padding: 12px; }
  .meta { color: #111; }
  .path { margin-top: 6px; color: #555; }
  img { max-width: 680px; max-height: 320px; display: block; margin-top: 10px; border: 1px solid #eee; }
  code { background: #f6f6f6; padding: 2px 5px; border-radius: 6px; }
</style>
</head><body>
""")
    report.append("<h1>Отчёт по дипломному шаблону</h1>")

    report.append("<h2>Поля-шаблоны ({{ ... }})</h2><ul>")
    for f in template_fields:
        report.append(f"<li><code>{html_escape(f)}</code></li>")
    report.append("</ul>")

    report.append("<h2>Жёстко прописанный текст (контент)</h2><ul>")
    for t in hard_texts:
        report.append(f"<li>{html_escape(t)}</li>")
    report.append("</ul>")

    report.append("<h2>Картинки (PNG/JPG/…)</h2>")
    report.append("<div class='grid'>")

    for item in extracted:
        kind = html_escape(item["kind"])
        mime = html_escape(item["mime"])

        report.append("<div class='card'>")
        report.append(f"<div class='meta'><b>{kind}</b> · <code>{mime}</code></div>")

        rel_path = None
        if isinstance(item["path"], Path):
            rel_path = rel_to_report(item["path"], report_path)
            report.append(f"<div class='path'><code>{html_escape(rel_path)}</code></div>")
        else:
            report.append(f"<div class='path'><code>{html_escape(str(item['path']))}</code></div>")

        if item.get("preview_type") == "img" and rel_path:
            report.append(f"<img src='{html_escape(rel_path)}' alt='preview'/>")

        report.append("</div>")

    report.append("</div>")
    report.append("</body></html>")

    report_path.write_text("\n".join(report), encoding="utf-8")

    print("OK")
    print(f"- report: {report_path.resolve()}")
    print(f"- images: {out_dir.resolve()}")
    print(f"- template fields: {len(template_fields)}")
    print(f"- hard texts: {len(hard_texts)}")
    print(f"- extracted image entries: {len(extracted)}")


if __name__ == "__main__":
    main()
