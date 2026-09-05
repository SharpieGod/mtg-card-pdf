"""
MTG Print Sheet Generator
- Reads Scryfall IDs and quantities from a CSV file
- Fetches full card images from Scryfall
- Lays them out on Letter-size pages at configurable card size (print & cut)

CLI usage:
    python mtg.py cards.csv
    python mtg.py cards.csv --output my_print.pdf --column "Scryfall ID" --qty-column "Quantity"

GUI usage:
    python mtg.py          (no arguments — opens the UI)
"""

import argparse
import csv
import queue
import sys
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

MM_TO_PT = 2.8346
PAGE_W, PAGE_H = letter  # 612 x 792 pt

HEADERS = {
    "User-Agent": "MTGPrintImporter/1.0",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


# --- Config ---


@dataclass
class Config:
    cards_per_row: int = 3
    cards_per_col: int = 3
    card_w_mm: float = 63.0
    card_h_mm: float = 88.0
    gap_mm: float = 1.0
    margin_in: float = 0.24
    jpeg_quality: int = 95
    batch_size: int = 75

    @property
    def cards_per_page(self):
        return self.cards_per_row * self.cards_per_col

    @property
    def card_w_pt(self):
        return self.card_w_mm * MM_TO_PT

    @property
    def card_h_pt(self):
        return self.card_h_mm * MM_TO_PT

    @property
    def gap_pt(self):
        return self.gap_mm * MM_TO_PT

    @property
    def margin_pt(self):
        return self.margin_in * 72

    @property
    def grid_w(self):
        return (
            self.cards_per_row * self.card_w_pt + (self.cards_per_row - 1) * self.gap_pt
        )

    @property
    def grid_h(self):
        return (
            self.cards_per_col * self.card_h_pt + (self.cards_per_col - 1) * self.gap_pt
        )

    @property
    def margin_x(self):
        return max(self.margin_pt, (PAGE_W - self.grid_w) / 2)

    @property
    def margin_y(self):
        return max(self.margin_pt, (PAGE_H - self.grid_h) / 2)


# --- CSV Loading ---


def load_ids_from_csv(path: str, id_column: str, qty_column: str) -> list[str]:
    """Returns a flat list of IDs, each repeated according to its quantity."""
    entries = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if id_column not in reader.fieldnames:
            available = ", ".join(reader.fieldnames)
            raise ValueError(
                f"ID column '{id_column}' not found. Available: {available}"
            )

        has_qty = qty_column in reader.fieldnames
        if not has_qty:
            print(
                f"WARNING: Quantity column '{qty_column}' not found — defaulting all quantities to 1."
            )

        for row in reader:
            scryfall_id = row[id_column].strip()
            if not scryfall_id:
                continue
            qty = 1
            if has_qty:
                try:
                    qty = max(1, int(row[qty_column].strip()))
                except (ValueError, KeyError):
                    qty = 1
            entries.extend([scryfall_id] * qty)

    unique = len(set(entries))
    print(f"Loaded {len(entries)} card slot(s) ({unique} unique) from {path}")
    return entries


# --- Scryfall ---


def fetch_batch(ids: list[str]) -> tuple[list[dict], list[dict]]:
    unique_ids = list(dict.fromkeys(ids))
    identifiers = [{"id": sid} for sid in unique_ids]
    resp = requests.post(
        "https://api.scryfall.com/cards/collection",
        headers=HEADERS,
        json={"identifiers": identifiers},
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []), data.get("not_found", [])


def fetch_all_cards(ids: list[str], cfg: Config) -> dict[str, dict]:
    """Returns a dict of scryfall_id -> card object for all unique IDs."""
    unique_ids = list(dict.fromkeys(ids))
    batches = [
        unique_ids[i : i + cfg.batch_size]
        for i in range(0, len(unique_ids), cfg.batch_size)
    ]
    card_map = {}
    all_not_found = []

    print(f"Fetching {len(unique_ids)} unique card(s) in {len(batches)} batch(es)...")
    for i, batch in enumerate(batches):
        cards, not_found = fetch_batch(batch)
        for card in cards:
            card_map[card["id"]] = card
        all_not_found.extend(not_found)
        print(
            f"  Batch {i+1}/{len(batches)}: {len(cards)} found, {len(not_found)} not found"
        )
        if i < len(batches) - 1:
            time.sleep(0.1)

    if all_not_found:
        print(f"\nWARNING: {len(all_not_found)} ID(s) not found on Scryfall:")
        for nf in all_not_found:
            print(f"  {nf}")

    return card_map


def get_image_urls(card: dict) -> list[tuple[str, str]]:
    """Return (url, face_label) for every face of a card."""
    name = card.get("name", "Unknown")
    if "image_uris" in card:
        url = card["image_uris"].get("normal") or card["image_uris"].get("large")
        return [(url, name)] if url else []
    faces = card.get("card_faces", [])
    results = []
    for face in faces:
        if "image_uris" in face:
            url = face["image_uris"].get("normal") or face["image_uris"].get("large")
            if url:
                results.append((url, face.get("name", name)))
    return results


def download_image(url: str) -> Image.Image | None:
    """Download a card image. No rate limit needed for *.scryfall.io."""
    try:
        resp = requests.get(
            url, headers={"User-Agent": "MTGPrintImporter/1.0"}, timeout=15
        )
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"  WARNING: Failed to download {url}: {e}")
        return None


# --- PDF Layout ---


def draw_card(c: canvas.Canvas, img: Image.Image, col: int, row: int, cfg: Config):
    x = cfg.margin_x + col * (cfg.card_w_pt + cfg.gap_pt)
    y = PAGE_H - cfg.margin_y - row * (cfg.card_h_pt + cfg.gap_pt) - cfg.card_h_pt

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=cfg.jpeg_quality)
    buf.seek(0)
    c.drawImage(
        ImageReader(buf),
        x,
        y,
        width=cfg.card_w_pt,
        height=cfg.card_h_pt,
        preserveAspectRatio=True,
        anchor="c",
    )


def draw_margin_zone(c: canvas.Canvas, cfg: Config):
    """Shade the non-printable margin on all four edges, drawn last so it overlays
    everything and makes the dead zone unmistakably visible in the PDF viewer."""
    m = cfg.margin_pt
    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(1, 1, 1)
    # Left/Right span full height so corners are fully covered
    c.rect(0, 0, m, PAGE_H, fill=1, stroke=0)
    c.rect(PAGE_W - m, 0, m, PAGE_H, fill=1, stroke=0)
    c.rect(0, 0, PAGE_W, m, fill=1, stroke=0)
    c.rect(0, PAGE_H - m, PAGE_W, m, fill=1, stroke=0)


def draw_cut_lines(c: canvas.Canvas, cfg: Config):
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.setLineWidth(0.25)
    safe_left = cfg.margin_pt
    safe_right = PAGE_W - cfg.margin_pt
    safe_bottom = cfg.margin_pt
    safe_top = PAGE_H - cfg.margin_pt
    for col in range(cfg.cards_per_row):
        x0 = cfg.margin_x + col * (cfg.card_w_pt + cfg.gap_pt)
        x1 = x0 + cfg.card_w_pt
        c.line(x0, safe_bottom, x0, safe_top)
        c.line(x1, safe_bottom, x1, safe_top)
    for row in range(cfg.cards_per_col):
        y0 = PAGE_H - cfg.margin_y - row * (cfg.card_h_pt + cfg.gap_pt)
        y1 = y0 - cfg.card_h_pt
        c.line(safe_left, y0, safe_right, y0)
        c.line(safe_left, y1, safe_right, y1)


def build_pdf(
    id_list: list[str],
    card_map: dict[str, dict],
    output_path: str,
    cfg: Config,
    on_progress=None,
):
    c = canvas.Canvas(output_path, pagesize=letter)
    image_cache: dict[str, Image.Image] = {}
    slot = 0
    page_num = 0
    total = sum(
        len(get_image_urls(card_map[sid])) for sid in id_list if sid in card_map
    )

    for scryfall_id in id_list:
        card = card_map.get(scryfall_id)
        if not card:
            print(f"  SKIP (not found): {scryfall_id}")
            continue

        face_urls = get_image_urls(card)
        if not face_urls:
            print(f"  SKIP (no image): {card.get('name', scryfall_id)}")
            continue

        for url, face_label in face_urls:
            pos_on_page = slot % cfg.cards_per_page
            col = pos_on_page % cfg.cards_per_row
            row = pos_on_page // cfg.cards_per_row

            if pos_on_page == 0:
                if slot > 0:
                    draw_cut_lines(c, cfg)
                    draw_margin_zone(c, cfg)
                    c.showPage()
                page_num += 1
                print(f"\nPage {page_num}:")

            if url not in image_cache:
                print(f"  Downloading: {face_label}")
                img = download_image(url)
                if img:
                    image_cache[url] = img
                else:
                    print(f"  SKIP (download failed): {face_label}")
                    continue
            else:
                print(f"  Using cached: {face_label}")

            draw_card(c, image_cache[url], col, row, cfg)
            slot += 1
            if on_progress:
                on_progress(slot, total)

    if slot > 0:
        draw_cut_lines(c, cfg)

    c.save()
    total_pages = -(-slot // cfg.cards_per_page)
    print(f"\nSaved: {output_path} ({slot} cards, {total_pages} page(s))")


# --- Shared core logic ---


def check_fit(cfg: Config):
    safe_w = PAGE_W - 2 * cfg.margin_pt
    safe_h = PAGE_H - 2 * cfg.margin_pt
    if cfg.grid_w > safe_w or cfg.grid_h > safe_h:
        print(
            f"WARNING: Grid ({cfg.grid_w:.1f} x {cfg.grid_h:.1f} pt) exceeds the safe print area "
            f"({safe_w:.1f} x {safe_h:.1f} pt). Cards near the edges may be clipped."
        )
    else:
        print(
            f"Grid fits safely: {cfg.grid_w:.1f} x {cfg.grid_h:.1f} pt "
            f"within {safe_w:.1f} x {safe_h:.1f} pt safe area."
        )


def run_generation(
    csv_file: str,
    output: str,
    column: str,
    qty_column: str,
    cfg: Config,
    on_progress=None,
):
    """Run the full pipeline. Raises on error so both CLI and GUI can handle it."""
    check_fit(cfg)

    if not Path(csv_file).exists():
        raise FileNotFoundError(f"File not found: {csv_file}")

    id_list = load_ids_from_csv(csv_file, column, qty_column)
    if not id_list:
        raise ValueError("No IDs found in CSV.")

    card_map = fetch_all_cards(id_list, cfg)
    if not card_map:
        raise ValueError("No cards returned from Scryfall.")

    total_slots = len(id_list)
    print(
        f"\nBuilding PDF: {total_slots} card slot(s), ~{-(-total_slots // cfg.cards_per_page)} page(s)..."
    )
    build_pdf(id_list, card_map, output, cfg, on_progress=on_progress)


# --- GUI ---


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext

    log_queue: queue.Queue[str] = queue.Queue()

    class _QueueStream:
        def write(self, text):
            log_queue.put(text)

        def flush(self):
            pass

    sys.stdout = _QueueStream()

    # Tell Windows this process handles its own DPI scaling so the window
    # isn't blurry-stretched on high-DPI / scaled displays.
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    root.title("MTG Print Sheet Generator")
    root.resizable(True, True)
    root.minsize(580, 560)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
    style.configure(
        "Section.TLabel", font=("Segoe UI", 9, "bold"), foreground="#475569"
    )
    style.configure("TLabel", font=("Segoe UI", 10))
    style.configure("TEntry", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10))
    style.configure("TSpinbox", font=("Segoe UI", 10))
    style.configure(
        "Generate.TButton",
        font=("Segoe UI", 11, "bold"),
        foreground="white",
        background="#2563eb",
        padding=(12, 6),
    )
    style.map(
        "Generate.TButton", background=[("disabled", "#94a3b8"), ("active", "#1d4ed8")]
    )

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    notebook = ttk.Notebook(root)
    notebook.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    # ── Generate tab ────────────────────────────────────────────────────────
    gen_tab = ttk.Frame(notebook, padding=16)
    gen_tab.columnconfigure(1, weight=1)
    notebook.add(gen_tab, text="  Generate  ")

    ttk.Label(gen_tab, text="MTG Print Sheet Generator", style="Header.TLabel").grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 14)
    )

    csv_var = tk.StringVar()
    ttk.Label(gen_tab, text="CSV File:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(gen_tab, textvariable=csv_var).grid(
        row=1, column=1, sticky="ew", padx=(8, 4)
    )
    ttk.Button(
        gen_tab,
        text="Browse…",
        command=lambda: csv_var.set(
            filedialog.askopenfilename(
                title="Select CSV file",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
        ),
    ).grid(row=1, column=2)

    out_var = tk.StringVar(value="print_sheet.pdf")
    ttk.Label(gen_tab, text="Output PDF:").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(gen_tab, textvariable=out_var).grid(
        row=2, column=1, sticky="ew", padx=(8, 4)
    )
    ttk.Button(
        gen_tab,
        text="Save as…",
        command=lambda: out_var.set(
            filedialog.asksaveasfilename(
                title="Save PDF as",
                defaultextension=".pdf",
                filetypes=[("PDF files", "*.pdf")],
                initialfile="print_sheet.pdf",
            )
        ),
    ).grid(row=2, column=2)

    ttk.Separator(gen_tab, orient="horizontal").grid(
        row=3, column=0, columnspan=3, sticky="ew", pady=12
    )

    col_var = tk.StringVar(value="Scryfall ID")
    qty_var = tk.StringVar(value="Quantity")
    ttk.Label(gen_tab, text="Scryfall ID column:").grid(
        row=4, column=0, sticky="w", pady=4
    )
    ttk.Entry(gen_tab, textvariable=col_var, width=24).grid(
        row=4, column=1, sticky="w", padx=(8, 0)
    )
    ttk.Label(gen_tab, text="Quantity column:").grid(
        row=5, column=0, sticky="w", pady=4
    )
    ttk.Entry(gen_tab, textvariable=qty_var, width=24).grid(
        row=5, column=1, sticky="w", padx=(8, 0)
    )

    ttk.Separator(gen_tab, orient="horizontal").grid(
        row=6, column=0, columnspan=3, sticky="ew", pady=12
    )

    btn = ttk.Button(gen_tab, text="Generate PDF", style="Generate.TButton")
    btn.grid(row=7, column=0, columnspan=3, pady=(0, 10))

    # Progress bar + status label (Generate tab, below button)
    progress_var = tk.DoubleVar(value=0)
    progress_bar = ttk.Progressbar(
        gen_tab, variable=progress_var, maximum=100, mode="determinate"
    )
    progress_bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 4))

    status_var = tk.StringVar(value="")
    ttk.Label(gen_tab, textvariable=status_var, foreground="#64748b").grid(
        row=9, column=0, columnspan=3, sticky="w"
    )

    # ── Log tab ──────────────────────────────────────────────────────────────
    log_tab = ttk.Frame(notebook, padding=8)
    log_tab.columnconfigure(0, weight=1)
    log_tab.rowconfigure(0, weight=1)
    notebook.add(log_tab, text="  Log  ")

    log_box = scrolledtext.ScrolledText(
        log_tab,
        state="disabled",
        font=("Consolas", 9),
        bg="#1e293b",
        fg="#e2e8f0",
        insertbackground="white",
        relief="flat",
        wrap="word",
    )
    log_box.grid(row=0, column=0, sticky="nsew")

    # ── Settings tab ────────────────────────────────────────────────────────
    def section(parent, text, row):
        ttk.Label(parent, text=text, style="Section.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 2)
        )

    def spin_row(parent, label, var, row, mn, mx, inc=1):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Spinbox(
            parent, textvariable=var, from_=mn, to=mx, increment=inc, width=8
        ).grid(row=row, column=1, sticky="w", padx=(8, 0))

    def entry_row(parent, label, var, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=10).grid(
            row=row, column=1, sticky="w", padx=(8, 0)
        )

    cfg_tab = ttk.Frame(notebook, padding=16)
    cfg_tab.columnconfigure(1, weight=1)
    notebook.add(cfg_tab, text="  Settings  ")

    _d = Config()
    s_rows = tk.IntVar(value=_d.cards_per_row)
    s_cols = tk.IntVar(value=_d.cards_per_col)
    s_card_w = tk.StringVar(value=f"{_d.card_w_mm:g}")
    s_card_h = tk.StringVar(value=f"{_d.card_h_mm:g}")
    s_gap = tk.StringVar(value=f"{_d.gap_mm:g}")
    s_margin = tk.StringVar(value=f"{_d.margin_in:g}")
    s_quality = tk.IntVar(value=_d.jpeg_quality)
    s_batch = tk.IntVar(value=_d.batch_size)

    section(cfg_tab, "LAYOUT", 0)
    spin_row(cfg_tab, "Cards per row:", s_rows, 1, 1, 10)
    spin_row(cfg_tab, "Cards per column:", s_cols, 2, 1, 10)

    section(cfg_tab, "CARD SIZE", 3)
    entry_row(cfg_tab, "Card width (mm):", s_card_w, 4)
    entry_row(cfg_tab, "Card height (mm):", s_card_h, 5)
    entry_row(cfg_tab, "Gap (mm):", s_gap, 6)

    section(cfg_tab, "PRINT", 7)
    entry_row(cfg_tab, "Printer margin (in):", s_margin, 8)
    spin_row(cfg_tab, "Image quality (%):", s_quality, 9, 1, 100)

    section(cfg_tab, "API", 10)
    spin_row(cfg_tab, "Batch size:", s_batch, 11, 1, 75)

    def build_config() -> Config:
        return Config(
            cards_per_row=int(s_rows.get()),
            cards_per_col=int(s_cols.get()),
            card_w_mm=float(s_card_w.get()),
            card_h_mm=float(s_card_h.get()),
            gap_mm=float(s_gap.get()),
            margin_in=float(s_margin.get()),
            jpeg_quality=int(s_quality.get()),
            batch_size=int(s_batch.get()),
        )

    progress_queue: queue.Queue = queue.Queue()  # sends (current, total) tuples

    # ── Log helpers & polling ────────────────────────────────────────────────
    def append_log(text: str):
        log_box.config(state="normal")
        log_box.insert("end", text)
        log_box.see("end")
        log_box.config(state="disabled")

    def poll_log():
        while True:
            try:
                append_log(log_queue.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                current, total = progress_queue.get_nowait()
                progress_bar.config(maximum=total)
                progress_var.set(current)
                status_var.set(f"Card {current} / {total}")
            except queue.Empty:
                break
        root.after(80, poll_log)

    root.after(80, poll_log)

    # ── Generate action ──────────────────────────────────────────────────────
    def on_generate():
        csv_path = csv_var.get().strip()
        out_path = out_var.get().strip()
        if not csv_path:
            append_log("ERROR: Please select a CSV file.\n")
            return
        if not out_path:
            append_log("ERROR: Please specify an output PDF path.\n")
            return

        try:
            cfg = build_config()
        except ValueError as e:
            append_log(f"ERROR: Invalid setting — {e}\n")
            return

        btn.config(state="disabled")
        progress_var.set(0)
        status_var.set("Starting…")
        append_log(f"\n{'─' * 48}\nStarting generation…\n{'─' * 48}\n")

        def worker():
            def on_progress(current, total):
                progress_queue.put((current, total))

            ok = False
            try:
                run_generation(
                    csv_path,
                    out_path,
                    col_var.get().strip(),
                    qty_var.get().strip(),
                    cfg,
                    on_progress=on_progress,
                )
                log_queue.put("\nDone!\n")
                ok = True
            except Exception as exc:
                log_queue.put(f"\nERROR: {exc}\n")
            finally:
                _ok = ok

                def _restore():
                    btn.config(state="normal")
                    status_var.set("Done" if _ok else "Failed")

                root.after(0, _restore)

        threading.Thread(target=worker, daemon=True).start()

    btn.config(command=on_generate)
    root.mainloop()


# --- CLI ---


def main():
    defaults = Config()
    parser = argparse.ArgumentParser(
        description="Generate a print sheet PDF from Scryfall IDs."
    )
    parser.add_argument("csv_file", help="Path to your CSV file")
    parser.add_argument("--output", default="print_sheet.pdf")
    parser.add_argument("--column", default="Scryfall ID")
    parser.add_argument("--qty-column", default="Quantity")
    parser.add_argument("--cards-per-row", type=int, default=defaults.cards_per_row)
    parser.add_argument("--cards-per-col", type=int, default=defaults.cards_per_col)
    parser.add_argument("--card-w-mm", type=float, default=defaults.card_w_mm)
    parser.add_argument("--card-h-mm", type=float, default=defaults.card_h_mm)
    parser.add_argument("--gap-mm", type=float, default=defaults.gap_mm)
    parser.add_argument("--margin-in", type=float, default=defaults.margin_in)
    parser.add_argument("--jpeg-quality", type=int, default=defaults.jpeg_quality)
    parser.add_argument("--batch-size", type=int, default=defaults.batch_size)
    args = parser.parse_args()

    cfg = Config(
        cards_per_row=args.cards_per_row,
        cards_per_col=args.cards_per_col,
        card_w_mm=args.card_w_mm,
        card_h_mm=args.card_h_mm,
        gap_mm=args.gap_mm,
        margin_in=args.margin_in,
        jpeg_quality=args.jpeg_quality,
        batch_size=args.batch_size,
    )

    try:
        run_generation(args.csv_file, args.output, args.column, args.qty_column, cfg)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        run_gui()
    else:
        main()