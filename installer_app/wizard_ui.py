"""Tiny multi-step wizard framework (tkinter), shared by the ZT POS
installer and uninstaller.

Subclass `Page`, add pages to a `Wizard`, and call `wizard.start()`. The
Wizard drives the Back / Next / Cancel buttons, a header (title + subtitle),
and a `shared` dict that pages read and write to pass data along.

Pages that do background work (install / uninstall) disable navigation while
running and call `wizard.set_next_enabled(True)` when finished, so the user
moves on to the Finish page only once the work succeeds.
"""
import tkinter as tk
from tkinter import ttk, messagebox

WHITE = "#ffffff"
INK = "#0f172a"
MUTED = "#475569"
PANEL_BG = "#0f172a"
PANEL_FG = "#e2e8f0"


class Page:
    """A single wizard screen. Override the hooks you need."""
    title = ""
    subtitle = ""

    def __init__(self, wizard):
        self.wizard = wizard
        self.frame = tk.Frame(wizard.body, bg=WHITE)

    def build(self):
        """Create widgets inside self.frame. Called once when added."""

    def on_enter(self):
        """Called each time this page becomes visible."""

    def validate(self):
        """Return True to allow advancing to the next page."""
        return True

    def handle_next(self):
        """Optional: take over the Next action. Return True if handled (the
        wizard then does nothing further); return False for default behaviour
        (advance, or finish on the last page)."""
        return False

    def next_text(self):
        return "Next"

    def show_back(self):
        return True

    def show_cancel(self):
        return True


class Wizard:
    def __init__(self, title, icon_path=None, width=620, height=520):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{width}x{height}")
        self.root.resizable(False, False)
        self.root.configure(bg=WHITE)
        if icon_path:
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass
        for theme in ("vista", "winnative", "clam"):
            try:
                ttk.Style().theme_use(theme)
                break
            except tk.TclError:
                continue

        self.shared = {}
        self.pages = []
        self.index = 0
        self.on_finish = None
        self._build_chrome()

    # ------------------------------------------------------------------ UI
    def _build_chrome(self):
        header = tk.Frame(self.root, bg=WHITE)
        header.pack(fill="x")
        self._title = tk.Label(header, font=("Segoe UI", 15, "bold"),
                               bg=WHITE, fg=INK, anchor="w", justify="left")
        self._title.pack(fill="x", padx=22, pady=(18, 0))
        self._subtitle = tk.Label(header, bg=WHITE, fg=MUTED, anchor="w",
                                  justify="left", wraplength=560)
        self._subtitle.pack(fill="x", padx=22, pady=(3, 12))
        ttk.Separator(self.root).pack(fill="x")

        self.body = tk.Frame(self.root, bg=WHITE)
        self.body.pack(fill="both", expand=True)

        ttk.Separator(self.root).pack(fill="x")
        footer = tk.Frame(self.root, bg=WHITE)
        footer.pack(fill="x")
        # Packed once, right-aligned: Cancel | Next | Back.
        self.cancel_btn = ttk.Button(footer, text="Cancel", command=self._cancel)
        self.cancel_btn.pack(side="right", padx=(6, 18), pady=12)
        self.next_btn = ttk.Button(footer, text="Next", command=self._next)
        self.next_btn.pack(side="right", pady=12)
        self.back_btn = ttk.Button(footer, text="Back", command=self._back)
        self.back_btn.pack(side="right", padx=6, pady=12)

    # ----------------------------------------------------------- lifecycle
    def add_page(self, page_cls):
        page = page_cls(self)
        page.build()
        self.pages.append(page)
        return page

    def start(self):
        self._show(0)
        self.root.mainloop()

    def after(self, fn):
        """Run fn on the UI thread (use from worker threads)."""
        self.root.after(0, fn)

    # ---------------------------------------------------------- navigation
    def _show(self, idx):
        for p in self.pages:
            p.frame.pack_forget()
        self.index = idx
        page = self.pages[idx]
        self._title.config(text=page.title)
        self._subtitle.config(text=page.subtitle)
        page.frame.pack(fill="both", expand=True, padx=22, pady=16)
        self.refresh_buttons()
        page.on_enter()

    def refresh_buttons(self):
        page = self.pages[self.index]
        self.next_btn.config(text=page.next_text(), state="normal")
        self.back_btn.config(
            state="normal" if (page.show_back() and self.index > 0) else "disabled")
        self.cancel_btn.config(
            state="normal" if page.show_cancel() else "disabled")

    def set_next_enabled(self, on):
        self.next_btn.config(state="normal" if on else "disabled")

    def set_back_enabled(self, on):
        self.back_btn.config(state="normal" if on else "disabled")

    def set_cancel_enabled(self, on):
        self.cancel_btn.config(state="normal" if on else "disabled")

    def _next(self):
        page = self.pages[self.index]
        if not page.validate():
            return
        if page.handle_next():
            return
        if self.index >= len(self.pages) - 1:
            self._finish()
        else:
            self._show(self.index + 1)

    def finish(self):
        self._finish()

    def _back(self):
        if self.index > 0:
            self._show(self.index - 1)

    def _cancel(self):
        if messagebox.askyesno("Cancel", "Exit the wizard?"):
            self.root.destroy()

    def _finish(self):
        if self.on_finish:
            try:
                self.on_finish(self.shared)
            except Exception:
                pass
        self.root.destroy()


def make_log_box(parent):
    """A dark, read-only progress log used by the work pages."""
    box = tk.Text(parent, height=10, state="disabled", relief="flat",
                  bg=PANEL_BG, fg=PANEL_FG, font=("Consolas", 9), wrap="word")
    return box


def append_log(box, msg):
    box.configure(state="normal")
    box.insert("end", msg + "\n")
    box.see("end")
    box.configure(state="disabled")
