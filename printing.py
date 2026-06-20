"""Direct (dialog-free) receipt printing to a connected printer.

The POS prints receipts straight to a connected printer with no browser print
dialog. We talk to the Windows print spooler via pywin32 and send a plain-text
(RAW) receipt, which is what thermal/ESC-POS receipt printers expect.

If pywin32 isn't available, no printer is connected, or printing fails, the
caller gets a clear reason it can surface to the cashier ("No printer
connected.").
"""
from decimal import Decimal

try:
    import win32print  # type: ignore
except Exception:       # pragma: no cover - non-Windows / pywin32 missing
    win32print = None

# Windows always exposes these virtual "printers"; none of them is a real,
# connected device, so we don't count them when deciding whether to print.
_VIRTUAL_HINTS = ("pdf", "xps", "onenote", "fax", "microsoft print", "to file")

RECEIPT_WIDTH = 42   # characters per line (fits 80mm thermal; readable on A4 too)


def _is_virtual(name):
    low = (name or "").lower()
    return any(h in low for h in _VIRTUAL_HINTS)


def list_physical_printers():
    """Names of real (non-virtual) printers known to Windows."""
    if win32print is None:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        printers = win32print.EnumPrinters(flags)
    except Exception:
        return []
    return [p[2] for p in printers if not _is_virtual(p[2])]


def pick_printer():
    """The printer to print to, or None if no real printer is connected.

    Prefers the Windows default printer when it's a real device; otherwise the
    first physical printer found.
    """
    if win32print is None:
        return None
    physical = list_physical_printers()
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = None
    if default and not _is_virtual(default):
        return default
    return physical[0] if physical else None


def printing_available():
    return win32print is not None


# --------------------------------------------------------------------------
# Receipt text rendering (monospace, fixed width)
# --------------------------------------------------------------------------
def _money(currency, amount):
    return f"{currency} {Decimal(str(amount or 0)):.2f}"


def _line(left, right, width=RECEIPT_WIDTH):
    """A left/right justified row that fits the receipt width."""
    left = str(left)
    right = str(right)
    space = width - len(left) - len(right)
    if space < 1:
        # Keep the amount visible; truncate the label.
        left = left[: max(0, width - len(right) - 1)]
        space = max(1, width - len(left) - len(right))
    return left + " " * space + right


def _center(text, width=RECEIPT_WIDTH):
    text = str(text)
    if len(text) >= width:
        return text
    pad = (width - len(text)) // 2
    return " " * pad + text


def receipt_text(sale, store_name, currency, kra_pin=""):
    """Build the plain-text receipt for `sale` (mirrors the on-screen receipt)."""
    sep = "-" * RECEIPT_WIDTH
    lines = []
    lines.append(_center(store_name))
    if kra_pin:
        lines.append(_center("PIN: %s" % kra_pin))
    lines.append(_center(sale.sale_number))
    lines.append(_center(sale.created_at.strftime("%Y-%m-%d %H:%M")))
    lines.append(_center(f"Cashier: {sale.cashier}"))
    lines.append(sep)
    for it in sale.items:
        qty = format(Decimal(str(it.quantity or 0)), "f").rstrip("0").rstrip(".") or "0"
        lines.append(_line(it.name, _money(currency, it.line_total)))
        lines.append(f"  {qty} x {_money(currency, it.unit_price)}")
    lines.append(sep)
    lines.append(_line("Subtotal", _money(currency, sale.subtotal)))
    if sale.tax and sale.tax > 0:
        lines.append(_line("Tax", _money(currency, sale.tax)))
    lines.append(_line("TOTAL", _money(currency, sale.total)))
    lines.append(_line(f"Paid ({sale.payment_method})", _money(currency, sale.amount_tendered)))
    if sale.payment_ref:
        label = "M-Pesa ref" if sale.payment_method == "mpesa" else "Bank ref"
        lines.append(_line(label, sale.payment_ref))
    if sale.change_due and sale.change_due > 0:
        lines.append(_line("Change", _money(currency, sale.change_due)))
    if sale.currency and sale.currency != currency:
        lines.append(sep)
        lines.append(_line(f"Paid in {sale.currency}", f"{sale.currency} {Decimal(str(sale.tendered_foreign)):.2f}"))
        if sale.change_foreign and sale.change_foreign > 0:
            lines.append(_line(f"Change ({sale.currency})", f"{sale.currency} {Decimal(str(sale.change_foreign)):.2f}"))
    if sale.customer:
        lines.append(sep)
        lines.append(_line("Customer", sale.customer.name or sale.customer.phone))
        if sale.points_earned:
            lines.append(_line("Points earned", f"+{sale.points_earned}"))
        lines.append(_line("Points balance", str(sale.customer.points)))
    lines.append(sep)
    lines.append(_center("Thank you!"))
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Sending to the spooler
# --------------------------------------------------------------------------
def print_receipt(sale, store_name, currency, kra_pin=""):
    """Print `sale`'s receipt directly. Returns (ok: bool, message: str).

    Never raises — the message is safe to show to the cashier.
    """
    if win32print is None:
        return False, "Printing isn't available on this machine."
    printer = pick_printer()
    if not printer:
        return False, "No printer connected."

    text = receipt_text(sale, store_name, currency, kra_pin)
    # ESC @ resets a thermal printer; trailing feed + partial cut where supported.
    payload = b"\x1b@" + text.encode("cp437", errors="replace") + b"\n\n\n\x1d\x56\x42\x00"
    try:
        h = win32print.OpenPrinter(printer)
        try:
            win32print.StartDocPrinter(h, 1, ("Receipt", None, "RAW"))
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, payload)
            win32print.EndPagePrinter(h)
            win32print.EndDocPrinter(h)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        return False, f"Couldn't print: {e}"
    return True, f"Printing to {printer}…"
