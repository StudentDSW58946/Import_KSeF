import csv
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    from defusedxml import ElementTree as ET  # ochrona przed billion laughs itp.
    SAFE_XML = True
except ImportError:  # awaryjnie: biblioteka standardowa (mniej bezpieczna)
    import xml.etree.ElementTree as ET
    SAFE_XML = False

APP_TITLE = "KSeF Converter"
MAX_XML_BYTES = 20 * 1024 * 1024  # limit rozmiaru pojedynczego pliku XML
ITEM_PREFIX = "Fa/FaWiersz/"

# Jeden wiersz wyjściowy = jedna pozycja faktury.
# Ścieżki są względne wobec elementu głównego (Faktura) lub, dla pozycji, wobec FaWiersz.
FIELDS = [
    ("invoice_no", "Numer faktury", "Fa/P_2"),
    ("issue_date", "Data wystawienia", "Fa/P_1"),
    ("sale_date", "Data sprzedaży", "Fa/P_6"),
    ("currency", "Waluta", "Fa/KodWaluty"),
    ("invoice_type", "Rodzaj faktury", "Fa/RodzajFaktury"),
    ("seller_nip", "NIP sprzedawcy", "Podmiot1/DaneIdentyfikacyjne/NIP"),
    ("seller_name", "Nazwa sprzedawcy", "Podmiot1/DaneIdentyfikacyjne/Nazwa"),
    ("buyer_nip", "NIP nabywcy", "Podmiot2/DaneIdentyfikacyjne/NIP"),
    ("buyer_name", "Nazwa nabywcy", "Podmiot2/DaneIdentyfikacyjne/Nazwa"),
    ("line_no", "Numer pozycji", ITEM_PREFIX + "NrWierszaFa"),
    ("before_corr", "Wiersz przed korektą", ITEM_PREFIX + "StanPrzed"),
    ("item_name", "Nazwa pozycji", ITEM_PREFIX + "P_7"),
    ("unit", "Jednostka", ITEM_PREFIX + "P_8A"),
    ("quantity", "Ilość", ITEM_PREFIX + "P_8B"),
    ("unit_net", "Cena jednostkowa netto", ITEM_PREFIX + "P_9A"),
    ("line_net", "Wartość netto pozycji", ITEM_PREFIX + "P_11"),
    ("vat_rate", "Stawka VAT", ITEM_PREFIX + "P_12"),
    ("total_net_23", "Netto 23%", "Fa/P_13_1"),
    ("total_vat_23", "VAT 23%", "Fa/P_14_1"),
    ("total_net_8", "Netto 8%", "Fa/P_13_2"),
    ("total_vat_8", "VAT 8%", "Fa/P_14_2"),
    ("total_net_5", "Netto 5%", "Fa/P_13_3"),
    ("total_vat_5", "VAT 5%", "Fa/P_14_3"),
    ("total_net_0kr", "Netto 0% (krajowa)", "Fa/P_13_6_1"),
    ("total_net_0wdt", "Netto 0% (WDT)", "Fa/P_13_6_2"),
    ("total_net_0ex", "Netto 0% (eksport)", "Fa/P_13_6_3"),
    ("total_net_zw", "Netto ZW", "Fa/P_13_7"),
    ("total_net_np1", "Netto NP (poza terytorium kraju)", "Fa/P_13_8"),
    ("total_net_np2", "Netto NP (art. 100 ust. 1 pkt 4)", "Fa/P_13_9"),
    ("total_net_oo", "Netto odwrotne obciążenie", "Fa/P_13_10"),
    ("total_vat_pln", "VAT w PLN (faktura w walucie)", "Fa/P_14_1W"),
    ("total_gross", "Brutto", "Fa/P_15"),
    ("payment_date", "Data zapłaty", "Fa/Platnosc/DataZaplaty"),
    ("order_no", "Numer zamówienia", "Fa/WarunkiTransakcji/Zamowienia/NrZamowienia"),
]

LABELS = {k: label for k, label, _ in FIELDS}
KEYS = [k for k, _, _ in FIELDS]
ITEM_KEYS = {k for k, _, p in FIELDS if p.startswith(ITEM_PREFIX)}
TOTAL_KEYS = {k for k in KEYS if k.startswith("total_")}
# Pola liczbowe: nie sanityzujemy ich (ujemne kwoty korekt) i możemy zamieniać kropkę na przecinek.
NUMERIC_KEYS = TOTAL_KEYS | {"quantity", "unit_net", "line_net", "line_no"}

ENCODINGS = {"UTF-8-SIG": "utf-8-sig", "UTF-8": "utf-8", "Windows-1250": "cp1250"}
DELIMS = {"Średnik (;)": ";", "Przecinek (,)": ",", "Tabulator": "\t"}


# ---------------------------------------------------------------- parsowanie

def detect_ns(root):
    """Wykrywa przestrzeń nazw z elementu głównego (działa dla FA(2), FA(3) itd.)."""
    tag = root.tag
    return tag[1:tag.index("}")] if tag.startswith("{") else ""


def qname(ns, tag):
    return f"{{{ns}}}{tag}" if ns else tag


def get_text(node, path, ns):
    """Zwraca tekst wszystkich pasujących elementów (powtarzalne elementy łączone ' | ')."""
    nodes = [node]
    for part in path.split("/"):
        nxt = []
        for n in nodes:
            nxt.extend(n.findall(qname(ns, part)))
        nodes = nxt
        if not nodes:
            return ""
    values = [(n.text or "").strip() for n in nodes]
    return " | ".join(v for v in values if v)


def parse_invoice(path, totals_first_row_only=True):
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_XML_BYTES:
        raise ValueError(f"Plik jest zbyt duży ({size // (1024 * 1024)} MB, limit {MAX_XML_BYTES // (1024 * 1024)} MB).")
    root = ET.parse(str(path)).getroot()
    if root.tag.split("}")[-1] != "Faktura":
        raise ValueError("Plik XML nie ma oczekiwanego elementu głównego Faktura.")
    ns = detect_ns(root)
    fa = root.find(qname(ns, "Fa"))
    if fa is None:
        raise ValueError("Brak sekcji Fa.")

    header = {k: get_text(root, p, ns) for k, _, p in FIELDS if k not in ITEM_KEYS}
    items = fa.findall(qname(ns, "FaWiersz")) or [None]  # faktura bez pozycji = jeden wiersz

    rows = []
    for idx, item in enumerate(items):
        row = dict(header)
        if idx > 0 and totals_first_row_only:
            for k in TOTAL_KEYS:
                row[k] = ""
        for k, _, p in FIELDS:
            if k in ITEM_KEYS:
                if item is None:
                    row[k] = ""
                elif k == "before_corr":
                    row[k] = "TAK" if item.find(qname(ns, "StanPrzed")) is not None else ""
                else:
                    row[k] = get_text(item, p[len(ITEM_PREFIX):], ns)
        rows.append(row)
    return rows


# ------------------------------------------------------------------- zapis

def sanitize(key, value):
    """Ochrona przed wstrzyknięciem formuł (CSV injection) – tylko pola tekstowe."""
    if key not in NUMERIC_KEYS and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def prepare_value(key, value, decimal_comma):
    if decimal_comma and key in NUMERIC_KEYS and value:
        return value.replace(".", ",")
    return sanitize(key, value)


def unique_path(dest):
    """Nie nadpisuje istniejących plików: dopisuje _2, _3, ..."""
    dest = Path(dest)
    if not dest.exists():
        return dest
    n = 2
    while True:
        cand = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        if not cand.exists():
            return cand
        n += 1


def write_csv(rows, columns, dest, delimiter, encoding, decimal_comma):
    with open(dest, "w", newline="", encoding=encoding, errors="replace") as f:
        writer = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([LABELS[c] for c in columns])
        for row in rows:
            writer.writerow([prepare_value(c, row.get(c, ""), decimal_comma) for c in columns])


def write_txt(rows, columns, dest, encoding, decimal_comma):
    with open(dest, "w", encoding=encoding, errors="replace") as f:
        for i, row in enumerate(rows, 1):
            f.write(f"POZYCJA {i}\n")
            for c in columns:
                value = row.get(c, "")
                if decimal_comma and c in NUMERIC_KEYS and value:
                    value = value.replace(".", ",")
                f.write(f"{LABELS[c]}: {value}\n")
            f.write("-" * 50 + "\n")


# --------------------------------------------------------------- konwersja

def run_conversion(files, columns, outdir, fmt, mode, delim, enc, decimal_comma, totals_first):
    """Czysta logika bez GUI – zwraca (liczba_poprawnych, błędy, utworzone_pliki)."""
    errors, outputs, parsed = [], [], []
    for p in files:
        try:
            parsed.append((p, parse_invoice(p, totals_first)))
        except Exception as e:  # noqa: BLE001 - każdy błąd pliku trafia do raportu
            errors.append(f"{Path(p).name}: {e}")

    def save(kind, rows, dest):
        try:
            dest = unique_path(dest)
            if kind == "csv":
                write_csv(rows, columns, dest, delim, enc, decimal_comma)
            else:
                write_txt(rows, columns, dest, enc, decimal_comma)
            outputs.append(dest.name)
        except OSError as e:
            errors.append(f"Zapis {dest.name}: {e}")

    want_csv = fmt in ("CSV", "CSV i TXT")
    want_txt = fmt in ("TXT", "CSV i TXT")
    if mode == "Jeden zbiorczy plik":
        all_rows = [r for _, rows in parsed for r in rows]
        if all_rows:
            if want_csv:
                save("csv", all_rows, Path(outdir) / "KSeF_zbiorczo.csv")
            if want_txt:
                save("txt", all_rows, Path(outdir) / "KSeF_zbiorczo.txt")
    else:
        for p, rows in parsed:
            if want_csv:
                save("csv", rows, Path(outdir) / f"{Path(p).stem}.csv")
            if want_txt:
                save("txt", rows, Path(outdir) / f"{Path(p).stem}.txt")

    if errors:
        try:
            report = unique_path(Path(outdir) / "Raport_bledow.txt")
            report.write_text("\n".join(errors), encoding="utf-8")
            outputs.append(report.name)
        except OSError:
            pass
    return len(parsed), errors, outputs


# --------------------------------------------------------------------- GUI

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x820")
        self.minsize(760, 680)
        self.files = []
        self.selected = {k: tk.BooleanVar(value=True) for k in KEYS}
        self.q = queue.Queue()
        self._build()

    def _build(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="KSeF Converter", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        subtitle = "Lokalna konwersja faktur XML KSeF do CSV i TXT"
        if not SAFE_XML:
            subtitle += "  |  UWAGA: brak biblioteki defusedxml (pip install defusedxml)"
        ttk.Label(root, text=subtitle, foreground="#555" if SAFE_XML else "#b00020").pack(anchor="w", pady=(0, 12))

        filebox = ttk.LabelFrame(root, text="1. Pliki XML", padding=10)
        filebox.pack(fill="x")
        bar = ttk.Frame(filebox)
        bar.pack(fill="x")
        ttk.Button(bar, text="Dodaj pliki XML", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Dodaj folder", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Wyczyść listę", command=self.clear_files).pack(side="left")
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="z podfolderami", variable=self.recursive).pack(side="left", padx=12)
        self.file_label = ttk.Label(filebox, text="Nie wybrano plików.")
        self.file_label.pack(anchor="w", pady=(8, 0))

        opts = ttk.LabelFrame(root, text="2. Ustawienia eksportu", padding=10)
        opts.pack(fill="x", pady=10)
        line = ttk.Frame(opts)
        line.pack(fill="x", pady=3)
        ttk.Label(line, text="Format:").pack(side="left")
        self.format = tk.StringVar(value="CSV")
        ttk.Combobox(line, textvariable=self.format, values=["CSV", "TXT", "CSV i TXT"], state="readonly", width=16).pack(side="left", padx=6)
        ttk.Label(line, text="Zapis:").pack(side="left", padx=(18, 0))
        self.mode = tk.StringVar(value="Jeden zbiorczy plik")
        ttk.Combobox(line, textvariable=self.mode, values=["Jeden zbiorczy plik", "Osobny plik dla każdej faktury"], state="readonly", width=30).pack(side="left", padx=6)
        line2 = ttk.Frame(opts)
        line2.pack(fill="x", pady=3)
        ttk.Label(line2, text="Separator CSV:").pack(side="left")
        self.delimiter = tk.StringVar(value="Średnik (;)")
        ttk.Combobox(line2, textvariable=self.delimiter, values=list(DELIMS), state="readonly", width=16).pack(side="left", padx=6)
        ttk.Label(line2, text="Kodowanie:").pack(side="left", padx=(18, 0))
        self.encoding = tk.StringVar(value="UTF-8-SIG")
        ttk.Combobox(line2, textvariable=self.encoding, values=list(ENCODINGS), state="readonly", width=16).pack(side="left", padx=6)
        line3 = ttk.Frame(opts)
        line3.pack(fill="x", pady=3)
        self.decimal_comma = tk.BooleanVar(value=True)
        ttk.Checkbutton(line3, text="Przecinek jako separator dziesiętny (polski Excel)", variable=self.decimal_comma).pack(side="left")
        self.totals_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(line3, text="Sumy faktury tylko w pierwszym wierszu", variable=self.totals_first).pack(side="left", padx=18)

        cols = ttk.LabelFrame(root, text="3. Kolumny eksportu (można zaznaczać i odznaczać)", padding=10)
        cols.pack(fill="both", expand=True)
        canvas = tk.Canvas(cols, height=240, highlightthickness=0)
        scroll = ttk.Scrollbar(cols, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for i, (key, label, _) in enumerate(FIELDS):
            ttk.Checkbutton(inner, text=label, variable=self.selected[key]).grid(row=i // 2, column=i % 2, sticky="w", padx=8, pady=3)
        # przewijanie kółkiem myszy tylko gdy kursor jest nad listą kolumn
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Zaznacz wszystkie", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bottom, text="Odznacz wszystkie", command=lambda: self.set_all(False)).pack(side="left", padx=6)
        self.convert_btn = ttk.Button(bottom, text="Konwertuj", command=self.convert)
        self.convert_btn.pack(side="right")
        self.status = ttk.Label(root, text="Gotowy.", foreground="#555")
        self.status.pack(anchor="w", pady=(8, 0))

    def set_all(self, value):
        for v in self.selected.values():
            v.set(value)

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Wybierz pliki XML", filetypes=[("Pliki XML", "*.xml"), ("Wszystkie pliki", "*.*")])
        self.files.extend(Path(p) for p in paths)
        self.refresh_files()

    def add_folder(self):
        folder = filedialog.askdirectory(title="Wybierz folder z XML")
        if folder:
            it = Path(folder).rglob("*") if self.recursive.get() else Path(folder).iterdir()
            self.files.extend(sorted(p for p in it if p.is_file() and p.suffix.lower() == ".xml"))
            self.refresh_files()

    def clear_files(self):
        self.files = []
        self.refresh_files()

    def refresh_files(self):
        self.files = list(dict.fromkeys(p.resolve() for p in self.files))
        self.file_label.config(text=f"Wybrano plików: {len(self.files)}" + (f" | {self.files[0].name}" if self.files else ""))

    def convert(self):
        if not self.files:
            messagebox.showwarning("Brak plików", "Najpierw dodaj pliki XML.")
            return
        columns = [k for k in KEYS if self.selected[k].get()]  # zachowuje kolejność z FIELDS
        if not columns:
            messagebox.showwarning("Brak kolumn", "Wybierz co najmniej jedną kolumnę.")
            return
        outdir = filedialog.askdirectory(title="Wybierz folder zapisu wyników")
        if not outdir:
            return
        args = (list(self.files), columns, outdir, self.format.get(), self.mode.get(),
                DELIMS[self.delimiter.get()], ENCODINGS[self.encoding.get()],
                self.decimal_comma.get(), self.totals_first.get())
        self.convert_btn.config(state="disabled")
        self.status.config(text="Trwa konwersja...")
        threading.Thread(target=self._worker, args=args, daemon=True).start()
        self.after(100, self._poll)

    def _worker(self, *args):
        try:
            self.q.put(("ok", run_conversion(*args)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("fail", str(e)))

    def _poll(self):
        try:
            kind, payload = self.q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.convert_btn.config(state="normal")
        if kind == "fail":
            self.status.config(text="Błąd konwersji.")
            messagebox.showerror("Błąd", f"Konwersja nie powiodła się:\n{payload}")
            return
        ok, errors, outputs = payload
        msg = f"Przetworzono poprawnie: {ok} plików XML.\nBłędy: {len(errors)}.\nUtworzone pliki: {', '.join(outputs) or 'brak'}"
        self.status.config(text=msg.replace("\n", " | "))
        if ok == 0:
            messagebox.showerror("Konwersja nieudana", msg + ("\n\n" + "\n".join(errors[:5]) if errors else ""))
        elif errors:
            messagebox.showwarning("Zakończono z błędami", msg)
        else:
            messagebox.showinfo("Zakończono konwersję", msg)


if __name__ == "__main__":
    App().mainloop()
