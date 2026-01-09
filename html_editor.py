import re
import base64
import mimetypes
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from io import BytesIO

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from bs4 import BeautifulSoup, NavigableString, Tag

# Pillow (для превью и размеров)
try:
    from PIL import Image, ImageTk
    PIL_OK = True
except Exception:
    PIL_OK = False


# ----------------------------
# Общие утилиты
# ----------------------------
WS_RE = re.compile(r"\s+")
JINJA_RE = re.compile(r"({{.*?}}|{%.+?%})", re.DOTALL)

def norm(s: str) -> str:
    return WS_RE.sub(" ", s or "").strip()

def has_jinja(s: str) -> bool:
    return bool(JINJA_RE.search(s or ""))

def strip_jinja(s: str) -> str:
    return norm(JINJA_RE.sub("", s or ""))


# ----------------------------
# Тексты
# ----------------------------
SKIP_TAGS = {"style", "script", "svg", "head"}
CANDIDATE_TAGS = ["h1", "h2", "h3", "p", "span", "div"]

STATUSES = ["Platinum", "Gold", "Silver", "Bronze"]


@dataclass(frozen=True)
class TextNodeRef:
    key: str
    display: str
    kind: str                # "text" | "status"
    tag_name: str
    class_str: str
    occurrence: int
    original_text: str


def _extract_status_texts_from_p_status(html: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    p = soup.find("p", class_="status")
    if not p:
        return {}

    inner = p.decode_contents()
    out: Dict[str, str] = {}
    for status in STATUSES:
        pattern = re.compile(
            r'{%\s*(if|elif)\s+status\s*==\s*"' + re.escape(status) + r'"\s*%}\s*([\s\S]*?)(?={%\s*(elif|endif)\b)',
            re.IGNORECASE
        )
        m = pattern.search(inner)
        if not m:
            continue
        block = m.group(2)
        block_soup = BeautifulSoup(block, "html.parser")
        text = norm(block_soup.get_text(" ", strip=True))
        if text:
            out[status] = text
    return out


def _apply_status_edits(html: str, status_edits: Dict[str, str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    p = soup.find("p", class_="status")
    if not p:
        return str(soup)

    inner = p.decode_contents()

    def replace_block(status: str, new_text: str, inner_html: str) -> str:
        pat = re.compile(
            r'({%\s*(if|elif)\s+status\s*==\s*"' + re.escape(status) + r'"\s*%}\s*)([\s\S]*?)(?=({%\s*(elif|endif)\b))',
            re.IGNORECASE
        )
        def _repl(m):
            prefix = m.group(1)
            return prefix + "\n                " + new_text + "\n                "
        return pat.sub(_repl, inner_html, count=1)

    for status, new_text in status_edits.items():
        t = norm(new_text)
        if not t:
            continue
        inner = replace_block(status, t, inner)

    p.clear()
    p.append(BeautifulSoup(inner, "html.parser"))
    return str(soup)


def extract_text_nodes(html: str) -> List[TextNodeRef]:
    status_texts = _extract_status_texts_from_p_status(html)
    status_nodes: List[TextNodeRef] = []
    for st in STATUSES:
        if st in status_texts:
            text = status_texts[st]
            status_nodes.append(TextNodeRef(
                key=f"STATUS|{st}",
                display=text,
                kind="status",
                tag_name="p",
                class_str="status",
                occurrence=1,
                original_text=text
            ))

    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.find_all(list(SKIP_TAGS)):
        bad.decompose()

    candidates: List[Tuple[str, str, str]] = []  # (tag, class_str, extracted_text)

    for tag in soup.find_all(CANDIDATE_TAGS):
        if any(isinstance(ch, Tag) for ch in tag.contents):
            continue
        if not tag.string or not isinstance(tag.string, NavigableString):
            continue

        raw = str(tag.string)
        text = norm(raw)
        if not text:
            continue

        if has_jinja(text):
            hard = strip_jinja(text)
            if not hard:
                continue
            text = hard

        class_str = " ".join(tag.get("class", []))
        candidates.append((tag.name, class_str, text))

    counters: Dict[Tuple[str, str], int] = {}
    nodes: List[TextNodeRef] = []
    for tag_name, class_str, extracted_text in candidates:
        key2 = (tag_name, class_str)
        counters[key2] = counters.get(key2, 0) + 1
        occ = counters[key2]
        nodes.append(TextNodeRef(
            key=f"TEXT|{tag_name}|{class_str}|{occ}",
            display=extracted_text,
            kind="text",
            tag_name=tag_name,
            class_str=class_str,
            occurrence=occ,
            original_text=extracted_text
        ))

    return nodes + status_nodes


def apply_text_edits(html: str, edits: Dict[str, str]) -> str:
    soup = BeautifulSoup(html, "html.parser")

    index: Dict[Tuple[str, str], List[Tag]] = {}
    for tag in soup.find_all(CANDIDATE_TAGS):
        if any(isinstance(ch, Tag) for ch in tag.contents):
            continue
        if not tag.string or not isinstance(tag.string, NavigableString):
            continue

        raw = str(tag.string)
        text = norm(raw)
        if not text:
            continue

        class_str = " ".join(tag.get("class", []))
        index.setdefault((tag.name, class_str), []).append(tag)

    for k, new_text in edits.items():
        if not k.startswith("TEXT|"):
            continue
        parts = k.split("|", 3)
        if len(parts) != 4:
            continue
        _, tag_name, class_str, occ_s = parts
        try:
            occ = int(occ_s)
        except Exception:
            continue

        tags = index.get((tag_name, class_str), [])
        if not (1 <= occ <= len(tags)):
            continue

        t = tags[occ - 1]
        old_raw = str(t.string) if t.string else ""

        if has_jinja(old_raw):
            old_label = strip_jinja(old_raw)
            if old_label:
                tail = old_raw.replace(old_label, "", 1)
                t.string = norm(new_text) + tail
        else:
            t.string = norm(new_text)

    status_edits: Dict[str, str] = {}
    for k, new_text in edits.items():
        if k.startswith("STATUS|"):
            st = k.split("|", 1)[1]
            status_edits[st] = norm(new_text)

    out_html = str(soup)
    if status_edits:
        out_html = _apply_status_edits(out_html, status_edits)
    return out_html


# ----------------------------
# Изображения
# ----------------------------
DATA_URL_RE = re.compile(r"^data:(image/(png|jpeg|jpg));base64,(.+)$", re.IGNORECASE | re.DOTALL)
BG_URL_RE = re.compile(
    r'(?:background-image|background)\s*:\s*[^;]*url\(\s*["\']?([^"\')]+)["\']?\s*\)',
    re.IGNORECASE
)

@dataclass
class ImageLocator:
    kind: str  # "img" | "inline_style" | "style_tag"
    a: int     # index (img index OR tag index)
    b: int     # match index inside style (for inline/style tag), else 0

@dataclass
class ImageEntry:
    img_id: str
    source: str        # img-tag | background-image
    mime: str
    fmt: str
    width: Optional[int]
    height: Optional[int]
    bytes_data: Optional[bytes]
    hint: str
    locator: ImageLocator


def _detect_size_from_bytes(b: bytes) -> Tuple[Optional[int], Optional[int]]:
    if not PIL_OK:
        return None, None
    try:
        im = Image.open(BytesIO(b))
        return im.size[0], im.size[1]
    except Exception:
        return None, None


def extract_images(html: str) -> List[ImageEntry]:
    soup = BeautifulSoup(html, "html.parser")
    out: List[ImageEntry] = []
    counter = 0

    # 1) img tags
    imgs = soup.find_all("img")
    for idx, img in enumerate(imgs):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        counter += 1
        img_id = f"IMG{counter:03d}"

        m = DATA_URL_RE.match(src)
        if m:
            mime = m.group(1).lower()
            fmt = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
            b64 = re.sub(r"\s+", "", m.group(3))
            try:
                data = base64.b64decode(b64)
            except Exception:
                data = b""
            w, h = _detect_size_from_bytes(data) if data else (None, None)
            out.append(ImageEntry(
                img_id=img_id,
                source="img-tag",
                mime=mime,
                fmt=fmt,
                width=w,
                height=h,
                bytes_data=data,
                hint="(data-url)",
                locator=ImageLocator(kind="img", a=idx, b=0),
            ))
        else:
            out.append(ImageEntry(
                img_id=img_id,
                source="img-tag",
                mime="image/unknown",
                fmt="?",
                width=None,
                height=None,
                bytes_data=None,
                hint=src,
                locator=ImageLocator(kind="img", a=idx, b=0),
            ))

    # 2) background-image in inline style=""
    style_tags = soup.find_all(style=True)
    for tag_idx, tag in enumerate(style_tags):
        style = tag.get("style") or ""
        urls = BG_URL_RE.findall(style)
        for match_idx, u in enumerate(urls):
            u = (u or "").strip()
            if not u:
                continue
            counter += 1
            img_id = f"IMG{counter:03d}"

            m = DATA_URL_RE.match(u)
            if m:
                mime = m.group(1).lower()
                fmt = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
                b64 = re.sub(r"\s+", "", m.group(3))
                try:
                    data = base64.b64decode(b64)
                except Exception:
                    data = b""
                w, h = _detect_size_from_bytes(data) if data else (None, None)
                out.append(ImageEntry(
                    img_id=img_id,
                    source="background-image",
                    mime=mime,
                    fmt=fmt,
                    width=w,
                    height=h,
                    bytes_data=data,
                    hint="(data-url)",
                    locator=ImageLocator(kind="inline_style", a=tag_idx, b=match_idx),
                ))
            else:
                out.append(ImageEntry(
                    img_id=img_id,
                    source="background-image",
                    mime="image/unknown",
                    fmt="?",
                    width=None,
                    height=None,
                    bytes_data=None,
                    hint=u,
                    locator=ImageLocator(kind="inline_style", a=tag_idx, b=match_idx),
                ))

    # 3) background-image in <style> blocks
    style_blocks = soup.find_all("style")
    for st_idx, st in enumerate(style_blocks):
        css = st.get_text() or ""
        urls = BG_URL_RE.findall(css)
        for match_idx, u in enumerate(urls):
            u = (u or "").strip()
            if not u:
                continue
            counter += 1
            img_id = f"IMG{counter:03d}"

            m = DATA_URL_RE.match(u)
            if m:
                mime = m.group(1).lower()
                fmt = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
                b64 = re.sub(r"\s+", "", m.group(3))
                try:
                    data = base64.b64decode(b64)
                except Exception:
                    data = b""
                w, h = _detect_size_from_bytes(data) if data else (None, None)
                out.append(ImageEntry(
                    img_id=img_id,
                    source="background-image",
                    mime=mime,
                    fmt=fmt,
                    width=w,
                    height=h,
                    bytes_data=data,
                    hint="(data-url)",
                    locator=ImageLocator(kind="style_tag", a=st_idx, b=match_idx),
                ))
            else:
                out.append(ImageEntry(
                    img_id=img_id,
                    source="background-image",
                    mime="image/unknown",
                    fmt="?",
                    width=None,
                    height=None,
                    bytes_data=None,
                    hint=u,
                    locator=ImageLocator(kind="style_tag", a=st_idx, b=match_idx),
                ))

    return out


def make_preview_fixed(b: bytes, target_w: int = 264, target_h: int = 200):
    if not PIL_OK:
        return None
    try:
        im = Image.open(BytesIO(b)).convert("RGBA")
        w, h = im.size
        scale = min(target_w / max(w, 1), target_h / max(h, 1))
        nw, nh = int(w * scale), int(h * scale)
        if nw < 1: nw = 1
        if nh < 1: nh = 1
        resized = im.resize((nw, nh))

        canvas = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 0))
        x = (target_w - nw) // 2
        y = (target_h - nh) // 2
        canvas.paste(resized, (x, y), resized)
        return ImageTk.PhotoImage(canvas)
    except Exception:
        return None


def _guess_mime_for_file(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    mime = (mime or "").lower()
    if mime in ("image/png", "image/jpeg"):
        return mime
    # fallback по расширению
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    return ""


def make_data_url_from_file(path: Path) -> Tuple[str, bytes]:
    """
    Возвращает (data_url, raw_bytes)
    """
    mime = _guess_mime_for_file(path)
    if mime not in ("image/png", "image/jpeg"):
        raise ValueError("Поддерживаются только PNG и JPG/JPEG.")

    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return data_url, raw


def replace_image_in_html(html: str, locator: ImageLocator, new_data_url: str) -> str:
    """
    Точечная замена картинки по locator:
    - img: N-й <img> → src=
    - inline_style: N-й tag with style → N-й background-image url(...)
    - style_tag: N-й <style> → N-й background-image url(...)
    """
    soup = BeautifulSoup(html, "html.parser")

    if locator.kind == "img":
        imgs = soup.find_all("img")
        if locator.a < 0 or locator.a >= len(imgs):
            raise ValueError("Не найден <img> для замены (индекс вне диапазона).")
        imgs[locator.a]["src"] = new_data_url
        return str(soup)

    if locator.kind == "inline_style":
        tags = soup.find_all(style=True)
        if locator.a < 0 or locator.a >= len(tags):
            raise ValueError("Не найден tag со style=... (индекс вне диапазона).")
        tag = tags[locator.a]
        style = tag.get("style") or ""
        urls = BG_URL_RE.findall(style)
        if locator.b < 0 or locator.b >= len(urls):
            raise ValueError("Не найден background-image url(...) (индекс вне диапазона).")
        old_url = urls[locator.b]

        # меняем только первое совпадение конкретного url (безопаснее, чем глобально)
        new_style = style.replace(old_url, new_data_url, 1)
        tag["style"] = new_style
        return str(soup)

    if locator.kind == "style_tag":
        style_blocks = soup.find_all("style")
        if locator.a < 0 or locator.a >= len(style_blocks):
            raise ValueError("Не найден <style> (индекс вне диапазона).")
        st = style_blocks[locator.a]
        css = st.get_text() or ""
        urls = BG_URL_RE.findall(css)
        if locator.b < 0 or locator.b >= len(urls):
            raise ValueError("Не найден background-image url(...) в CSS (индекс вне диапазона).")
        old_url = urls[locator.b]

        new_css = css.replace(old_url, new_data_url, 1)
        # важно: перезаписываем содержимое style
        st.string = new_css
        return str(soup)

    raise ValueError("Неизвестный locator.kind")


# ----------------------------
# GUI
# ----------------------------
class HTMLEditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HTML-edit Content Editor")
        self.geometry("1250x760")

        # Working files (JSON, etc.) are stored in a subfolder to keep the repo clean
        # and avoid committing temporary data into GitHub.
        self.work_dir = Path(__file__).resolve().parent / "_work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.work_dir / "session.json"
        self.last_dir: Optional[str] = None
        self._load_session()

        self.html_path: Optional[Path] = None
        self.original_html: str = ""

        self.nodes: List[TextNodeRef] = []
        self.edits: Dict[str, str] = {}

        self.selected_key: Optional[str] = None

        self.images: List[ImageEntry] = []
        self._preview_photo = None
        self._selected_image_index: Optional[int] = None

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)

        self.file_label = ttk.Label(top, text="Файл: не выбран")
        self.file_label.pack(side="left")

        ttk.Button(top, text="Открыть HTML", command=self.open_file).pack(side="left", padx=10)

        if not PIL_OK:
            ttk.Label(top, text="(Pillow не установлен — превью/размеры могут не работать)", foreground="#a00").pack(side="left", padx=10)

        self.saved_label = ttk.Label(top, text="Изменений сохранено: 0")
        self.saved_label.pack(side="right")

        style = ttk.Style()
        style.configure("TNotebook", background="#f4f4f4")
        style.configure("TNotebook.Tab", padding=[14, 6], background="#eaeaea")
        style.map("TNotebook.Tab", background=[("selected", "#dcdcdc")])

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Status bar (bottom)
        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        ttk.Label(status, text="© LICANT Company").pack(side="right")


        self.tab_texts = ttk.Frame(self.nb)
        self.tab_images = ttk.Frame(self.nb)

        self.nb.add(self.tab_texts, text="Тексты")
        self.nb.add(self.tab_images, text="Изображения")

        self._build_texts_tab()
        self._build_images_tab()

    # -------- Session (JSON in _work/) --------
    def _load_session(self) -> None:
        """Load lightweight session info (e.g., last opened directory)."""
        try:
            if self.session_path.exists():
                data = json.loads(self.session_path.read_text(encoding="utf-8"))
                last_dir = data.get("last_dir")
                if isinstance(last_dir, str) and last_dir:
                    self.last_dir = last_dir
        except Exception:
            # Session is optional; ignore any errors.
            self.last_dir = None

    def _save_session(self) -> None:
        """Persist lightweight session info into _work/session.json."""
        try:
            payload = {"last_dir": self.last_dir or ""}
            self.session_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -------- Тексты --------
    def _build_texts_tab(self):
        body = ttk.Frame(self.tab_texts)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="Тексты (по порядку):").pack(anchor="w")

        self.listbox = tk.Listbox(left, width=45, height=30, exportselection=False)
        self.listbox.pack(fill="y", expand=False)
        self.listbox.bind("<<ListboxSelect>>", self.on_select_text)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        ttk.Label(right, text="Текущий текст / Новый текст:").pack(anchor="w")

        self.text = tk.Text(right, height=18)
        self.text.pack(fill="both", expand=True)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=8)

        ttk.Button(btns, text="Сохранить поле", command=self.save_field).pack(side="left")
        ttk.Button(btns, text="Сохранить HTML", command=self.save_html).pack(side="left", padx=10)

        self.where_label = ttk.Label(right, text="")
        self.where_label.pack(anchor="w", pady=(6, 0))

        self.count_label = ttk.Label(right, text="")
        self.count_label.pack(anchor="w")

    def on_select_text(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        node = self.nodes[idx]
        self.selected_key = node.key

        current_text = self.edits.get(self.selected_key, node.original_text)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", current_text)

        if node.kind == "status":
            where = "<p class=status> (ветка if/elif)"
        else:
            where = f"<{node.tag_name}{(' class=' + node.class_str) if node.class_str else ''}> (#{node.occurrence})"
        self.where_label.config(text=f"Где: {where}")

    def save_field(self):
        if not self.selected_key:
            messagebox.showwarning("Нет выбора", "Сначала выбери поле слева.")
            return

        new_text = norm(self.text.get("1.0", "end").strip())
        if not new_text:
            messagebox.showwarning("Пустой текст", "Новый текст пустой — так нельзя.")
            return

        self.edits[self.selected_key] = new_text
        self.saved_label.config(text=f"Изменений сохранено: {len(self.edits)}")

    def save_html(self):
        if not self.html_path:
            messagebox.showwarning("Нет файла", "Сначала открой HTML.")
            return

        # автосохранение текущего поля
        if self.selected_key:
            new_text = norm(self.text.get("1.0", "end").strip())
            if new_text:
                self.edits[self.selected_key] = new_text

        edited = apply_text_edits(self.original_html, self.edits)

        self.html_path.write_text(edited, encoding="utf-8")
        self.original_html = edited

        self.last_dir = str(self.html_path.parent)
        self._save_session()

        messagebox.showinfo("Готово", f"Сохранено в: {self.html_path.name}")

        self.rescan_all()

    # -------- Изображения --------
    def _build_images_tab(self):
        wrap = ttk.Frame(self.tab_images)
        wrap.pack(fill="both", expand=True)

        left = ttk.Frame(wrap)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Картинки из HTML:").pack(anchor="w")

        cols = ("id", "source", "type", "size")
        self.img_tree = ttk.Treeview(left, columns=cols, show="headings", height=24)
        self.img_tree.heading("id", text="ID")
        self.img_tree.heading("source", text="Источник")
        self.img_tree.heading("type", text="Тип")
        self.img_tree.heading("size", text="Размер (px)")

        self.img_tree.column("id", width=90)
        self.img_tree.column("source", width=160)
        self.img_tree.column("type", width=140)
        self.img_tree.column("size", width=140)

        self.img_tree.pack(fill="both", expand=True)
        self.img_tree.bind("<<TreeviewSelect>>", self.on_image_select)

        right = ttk.Frame(wrap, width=320)
        right.pack(side="left", fill="y", expand=False, padx=(12, 0))
        right.pack_propagate(False)

        ttk.Label(right, text="Preview:").pack(anchor="w")

        self.preview_label = ttk.Label(right)
        self.preview_label.pack(anchor="n", pady=6)

        self.preview_info = ttk.Label(right, text="", justify="left")
        self.preview_info.pack(anchor="w", pady=6)

        ttk.Button(right, text="Заменить изображение…", command=self.replace_selected_image).pack(anchor="w", pady=10)

        self.replace_note = ttk.Label(
            right,
            text="Поддерживаются PNG/JPG.\nЗаменяется прямо в HTML как data-url.",
            justify="left"
        )
        self.replace_note.pack(anchor="w")

    def on_image_select(self, event=None):
        sel = self.img_tree.selection()
        if not sel:
            self._selected_image_index = None
            return
        iid = sel[0]
        idx = int(self.img_tree.item(iid, "tags")[0])
        self._selected_image_index = idx

        e = self.images[idx]

        if e.bytes_data and PIL_OK:
            photo = make_preview_fixed(e.bytes_data, target_w=264, target_h=200)
            self._preview_photo = photo
            self.preview_label.configure(image=photo if photo else "")
        else:
            self._preview_photo = None
            self.preview_label.configure(image="")

        size_txt = f"{e.width}×{e.height}" if e.width and e.height else "?"
        self.preview_info.config(
            text=f"ID: {e.img_id}\nИсточник: {e.source}\nТип: {e.mime}\nРазмер: {size_txt}\nHint: {e.hint}"
        )

    def replace_selected_image(self):
        if not self.html_path:
            messagebox.showwarning("Нет файла", "Сначала открой HTML.")
            return
        if self._selected_image_index is None:
            messagebox.showwarning("Нет выбора", "Сначала выбери картинку в таблице слева.")
            return

        entry = self.images[self._selected_image_index]

        path_str = filedialog.askopenfilename(
            title="Выберите PNG/JPG",
            filetypes=[("Images", "*.png;*.jpg;*.jpeg"), ("All files", "*.*")]
        )
        if not path_str:
            return

        try:
            data_url, raw = make_data_url_from_file(Path(path_str))
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            return

        try:
            new_html = replace_image_in_html(self.original_html, entry.locator, data_url)
        except Exception as e:
            messagebox.showerror("Ошибка замены", str(e))
            return

        # сохранить в тот же файл
        self.html_path.write_text(new_html, encoding="utf-8")
        self.original_html = new_html

        self.last_dir = str(self.html_path.parent)
        self._save_session()

        messagebox.showinfo("Готово", f"Изображение {entry.img_id} заменено и сохранено в {self.html_path.name}")

        # обновить интерфейс
        self.rescan_all()

        # попытка выделить примерно ту же строку по ID
        for iid in self.img_tree.get_children():
            vals = self.img_tree.item(iid, "values")
            if vals and vals[0] == entry.img_id:
                self.img_tree.selection_set(iid)
                self.img_tree.see(iid)
                self.on_image_select()
                break

    # -------- Работа с файлом --------
    def open_file(self):
        path = filedialog.askopenfilename(
            title="Выберите HTML",
            initialdir=self.last_dir if self.last_dir else None,
            filetypes=[("HTML files", "*.html"), ("All files", "*.*")]
        )
        if not path:
            return

        self.html_path = Path(path)
        self.file_label.config(text=f"Файл: {self.html_path.name}")
        self.original_html = self.html_path.read_text(encoding="utf-8", errors="replace")

        # remember last directory for convenience
        self.last_dir = str(self.html_path.parent)
        self._save_session()

        self.rescan_all()

    def rescan_all(self):
        # Тексты
        self.nodes = extract_text_nodes(self.original_html)
        self.listbox.delete(0, tk.END)
        for n in self.nodes:
            self.listbox.insert(tk.END, n.display)

        self.selected_key = None
        self.text.delete("1.0", tk.END)
        self.where_label.config(text="")
        self.count_label.config(text=f"Найдено текстовых полей: {len(self.nodes)}")

        if self.nodes:
            self.listbox.selection_set(0)
            self.listbox.activate(0)
            self.on_select_text()

        # Картинки
        self.images = extract_images(self.original_html)
        for iid in self.img_tree.get_children():
            self.img_tree.delete(iid)

        for i, e in enumerate(self.images):
            size_txt = f"{e.width}×{e.height}" if e.width and e.height else "?"
            self.img_tree.insert("", "end", values=(e.img_id, e.source, e.mime, size_txt), tags=(str(i),))

        self.preview_label.configure(image="")
        self.preview_info.config(text=f"Найдено картинок: {len(self.images)}")
        self._preview_photo = None
        self._selected_image_index = None


if __name__ == "__main__":
    app = HTMLEditorApp()
    app.mainloop()
