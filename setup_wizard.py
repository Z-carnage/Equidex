#!/usr/bin/env python3
"""
EQUIDEX installer — a self-contained setup wizard.

Bundled (via PyInstaller) with the EQUIDEX app payload, this shows a classic multi-step install
wizard (Welcome -> License -> Location -> Installing -> Finish), copies the app into the user's
programs folder, creates Start-Menu and Desktop shortcuts, and registers an Add/Remove Programs
entry with an uninstaller. No administrator rights required (installs per-user).

Run with `--uninstall` to remove a previous installation.
"""
import os, sys, shutil, subprocess, threading, traceback
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME   = "EQUIDEX"
APP_EXE    = "EQUIDEX.exe"
ICON_FILE  = "equidex-light.ico"   # dark mark on transparent — the shortcut/Explorer icon
PUBLISHER  = "EQUIDEX"
VERSION    = "1.0.0"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\EQUIDEX"

# brand palette
BG      = "#0f1216"
PANEL   = "#ffffff"
INK     = "#1c1f24"
MUTED   = "#5a616b"
ACCENT  = "#2f6fd6"

def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

def default_dir():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Programs", APP_NAME)

def start_menu_dir():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")

def desktop_dir():
    return os.path.join(os.path.expanduser("~"), "Desktop")

def _ps(script):
    """Run a PowerShell snippet, hidden."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          startupinfo=si, capture_output=True, text=True)

def make_shortcut(lnk_path, target, icon, workdir):
    _ps("$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut('%s'); "
        "$s.TargetPath='%s'; $s.IconLocation='%s'; $s.WorkingDirectory='%s'; "
        "$s.Description='EQUIDEX research terminal'; $s.Save()"
        % (lnk_path, target, icon, workdir))

def reg_register(install_dir, size_kb):
    exe = os.path.join(install_dir, APP_EXE)
    uninst = 'cmd /c "%s"' % os.path.join(install_dir, "uninstall.bat")
    ps = (
        "$k='HKCU:\\%s';"
        "New-Item -Path $k -Force | Out-Null;"
        "Set-ItemProperty $k DisplayName '%s';"
        "Set-ItemProperty $k DisplayVersion '%s';"
        "Set-ItemProperty $k Publisher '%s';"
        "Set-ItemProperty $k DisplayIcon '%s';"
        "Set-ItemProperty $k InstallLocation '%s';"
        "Set-ItemProperty $k UninstallString '%s';"
        "Set-ItemProperty $k NoModify 1 -Type DWord;"
        "Set-ItemProperty $k NoRepair 1 -Type DWord;"
        "Set-ItemProperty $k EstimatedSize %d -Type DWord;"
        % (UNINSTALL_KEY.replace("\\", "\\\\"), APP_NAME, VERSION, PUBLISHER,
           exe, install_dir, uninst.replace("'", "''"), int(size_kb))
    )
    _ps(ps)

def write_uninstaller(install_dir):
    sm = os.path.join(start_menu_dir(), APP_NAME + ".lnk")
    dt = os.path.join(desktop_dir(), APP_NAME + ".lnk")
    bat = (
        "@echo off\r\n"
        "reg delete \"HKCU\\%s\" /f >nul 2>&1\r\n"
        "del \"%s\" >nul 2>&1\r\n"
        "del \"%s\" >nul 2>&1\r\n"
        "start \"\" cmd /c \"timeout /t 1 >nul & rmdir /s /q \"%s\"\"\r\n"
        % (UNINSTALL_KEY, sm, dt, install_dir)
    )
    with open(os.path.join(install_dir, "uninstall.bat"), "w", encoding="ascii", errors="ignore") as f:
        f.write(bat)


# ----------------------------------------------------------------------------- uninstall path
def run_uninstall():
    r = tk.Tk(); r.withdraw()
    if not messagebox.askyesno(APP_NAME + " — Uninstall", "Remove EQUIDEX from this computer?"):
        return
    install_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    bat = os.path.join(install_dir, "uninstall.bat")
    if os.path.exists(bat):
        subprocess.Popen(["cmd", "/c", bat])
    messagebox.showinfo(APP_NAME, "EQUIDEX has been removed.")
    r.destroy()


# ----------------------------------------------------------------------------- wizard
class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME + " Setup")
        self.resizable(False, False)
        self.configure(bg=PANEL)
        try: self.iconbitmap(resource(ICON_FILE))
        except Exception: pass
        W, H = 540, 400
        x = (self.winfo_screenwidth() - W) // 2
        y = (self.winfo_screenheight() - H) // 3
        self.geometry(f"{W}x{H}+{x}+{y}")

        self.install_dir = tk.StringVar(value=default_dir())
        self.desktop_ic  = tk.BooleanVar(value=True)
        self.launch_after = tk.BooleanVar(value=True)
        self.accepted = tk.BooleanVar(value=False)

        self._style()
        self._header()
        self.body = tk.Frame(self, bg=PANEL); self.body.pack(fill="both", expand=True, padx=0, pady=0)
        self._footer()
        self.pages = [self._page_welcome, self._page_license, self._page_location,
                      self._page_install, self._page_finish]
        self.idx = 0
        self._show(0)

    def _style(self):
        s = ttk.Style(self)
        try: s.theme_use("vista")
        except Exception: pass
        s.configure("TButton", padding=(14, 6))
        s.configure("Accent.TButton", padding=(14, 6))
        s.configure("TCheckbutton", background=PANEL, foreground=INK)
        s.configure("Horizontal.TProgressbar", troughcolor="#e6e8ec", background=ACCENT)

    def _header(self):
        h = tk.Frame(self, bg="#0f1216", height=64); h.pack(fill="x"); h.pack_propagate(False)
        # icon
        self._logo = None
        try:
            self._logo = tk.PhotoImage(file=resource("equidex_hdr.png"))
            tk.Label(h, image=self._logo, bg="#0f1216").pack(side="left", padx=(16, 10))
        except Exception:
            pass
        box = tk.Frame(h, bg="#0f1216"); box.pack(side="left", fill="y")
        tk.Label(box, text="EQUIDEX", bg="#0f1216", fg="#ffffff",
                 font=("Segoe UI Semibold", 15)).pack(anchor="w", pady=(12, 0))
        self.subtitle = tk.Label(box, text="Setup", bg="#0f1216", fg="#9aa3ad",
                                 font=("Segoe UI", 9))
        self.subtitle.pack(anchor="w")

    def _footer(self):
        sep = tk.Frame(self, bg="#dfe2e7", height=1); sep.pack(fill="x", side="bottom")
        f = tk.Frame(self, bg=PANEL, height=52); f.pack(fill="x", side="bottom"); f.pack_propagate(False)
        self.btn_cancel = ttk.Button(f, text="Cancel", command=self._cancel)
        self.btn_cancel.pack(side="right", padx=(6, 14), pady=10)
        self.btn_next = ttk.Button(f, text="Next", command=self._next)
        self.btn_next.pack(side="right", pady=10)
        self.btn_back = ttk.Button(f, text="Back", command=self._back)
        self.btn_back.pack(side="right", padx=6, pady=10)

    def _clear(self):
        for w in self.body.winfo_children(): w.destroy()

    def _content(self):
        c = tk.Frame(self.body, bg=PANEL); c.pack(fill="both", expand=True, padx=22, pady=18)
        return c

    # pages -------------------------------------------------------------------
    def _page_welcome(self):
        self.subtitle.config(text="Welcome")
        c = self._content()
        tk.Label(c, text="Welcome to the EQUIDEX Setup Wizard", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 13)).pack(anchor="w")
        msg = ("This will install EQUIDEX — a keyboard-driven equity research terminal for US & "
               "Indian markets — on your computer.\n\n"
               "EQUIDEX runs as a desktop app and fetches live data from free public sources; an "
               "internet connection is required.\n\n"
               "Click Next to continue.")
        tk.Label(c, text=msg, bg=PANEL, fg=MUTED, font=("Segoe UI", 10),
                 wraplength=470, justify="left").pack(anchor="w", pady=(14, 0))
        self.btn_back.state(["disabled"]); self.btn_next.config(text="Next")

    def _page_license(self):
        self.subtitle.config(text="Terms & Conditions")
        c = self._content()
        tk.Label(c, text="Please read the Terms and Conditions", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        box = tk.Frame(c, bg=PANEL); box.pack(fill="both", expand=True, pady=(10, 8))
        txt = tk.Text(box, wrap="word", font=("Segoe UI", 9), bg="#f6f7f9", fg=INK,
                      relief="solid", bd=1, padx=10, pady=8, height=10)
        sb = ttk.Scrollbar(box, command=txt.yview); txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); txt.pack(side="left", fill="both", expand=True)
        loaded = False
        for src in ("TERMS.md", "SETUP.md"):     # prefer the Terms & Conditions; fall back to notices
            try:
                with open(resource(src), encoding="utf-8") as f:
                    txt.insert("1.0", f.read()); loaded = True; break
            except Exception:
                pass
        if not loaded:
            txt.insert("1.0", "EQUIDEX — for research and information only. Not investment advice; "
                              "no warranty. Data from third-party sources subject to their terms.")
        txt.config(state="disabled")
        ttk.Checkbutton(c, text="I have read and accept the Terms and Conditions", variable=self.accepted,
                        command=self._sync_next).pack(anchor="w")
        self.btn_back.state(["!disabled"]); self._sync_next()

    def _page_location(self):
        self.subtitle.config(text="Install location")
        c = self._content()
        tk.Label(c, text="Choose where to install EQUIDEX", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(c, text="Setup will install EQUIDEX into the following folder. It installs for the "
                        "current user and needs no administrator rights.", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 10), wraplength=470, justify="left").pack(anchor="w", pady=(8, 14))
        row = tk.Frame(c, bg=PANEL); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.install_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(c, text="Create a Desktop shortcut", variable=self.desktop_ic).pack(anchor="w", pady=(18, 0))
        self.accepted.set(True)  # already accepted on previous page
        self.btn_next.config(text="Install"); self.btn_back.state(["!disabled"]); self.btn_next.state(["!disabled"])

    def _page_install(self):
        self.subtitle.config(text="Installing")
        c = self._content()
        tk.Label(c, text="Installing EQUIDEX…", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        self.status = tk.Label(c, text="Preparing…", bg=PANEL, fg=MUTED, font=("Segoe UI", 10))
        self.status.pack(anchor="w", pady=(12, 6))
        self.pb = ttk.Progressbar(c, mode="determinate", maximum=100, length=470)
        self.pb.pack(anchor="w", pady=(4, 0))
        self.btn_back.state(["disabled"]); self.btn_next.state(["disabled"]); self.btn_cancel.state(["disabled"])
        threading.Thread(target=self._do_install, daemon=True).start()

    def _page_finish(self):
        self.subtitle.config(text="Complete")
        c = self._content()
        ok = getattr(self, "install_ok", False)
        tk.Label(c, text=("EQUIDEX has been installed" if ok else "Installation failed"),
                 bg=PANEL, fg=INK, font=("Segoe UI Semibold", 13)).pack(anchor="w")
        if ok:
            tk.Label(c, text="Setup finished installing EQUIDEX on your computer. You can launch it "
                            "from the Start Menu or the Desktop shortcut.", bg=PANEL, fg=MUTED,
                     font=("Segoe UI", 10), wraplength=470, justify="left").pack(anchor="w", pady=(12, 14))
            ttk.Checkbutton(c, text="Launch EQUIDEX now", variable=self.launch_after).pack(anchor="w")
        else:
            tk.Label(c, text=getattr(self, "install_err", "An unexpected error occurred."),
                     bg=PANEL, fg="#b3261e", font=("Segoe UI", 10), wraplength=470,
                     justify="left").pack(anchor="w", pady=(12, 0))
        self.btn_back.state(["disabled"]); self.btn_cancel.state(["disabled"])
        self.btn_next.state(["!disabled"]); self.btn_next.config(text="Finish", command=self._finish)

    # install worker ----------------------------------------------------------
    def _do_install(self):
        try:
            d = self.install_dir.get().strip() or default_dir()
            self._set(5, "Creating install folder…"); os.makedirs(d, exist_ok=True)
            self._set(15, "Copying application…")
            shutil.copy2(resource(APP_EXE), os.path.join(d, APP_EXE))
            self._set(60, "Copying resources…")
            for extra in (ICON_FILE, "SETUP.md", "TERMS.md", "THIRD-PARTY-LICENSES.txt"):
                try: shutil.copy2(resource(extra), os.path.join(d, extra))
                except Exception: pass
            exe = os.path.join(d, APP_EXE); ico = os.path.join(d, ICON_FILE)
            self._set(72, "Creating shortcuts…")
            try:
                os.makedirs(start_menu_dir(), exist_ok=True)
                make_shortcut(os.path.join(start_menu_dir(), APP_NAME + ".lnk"), exe, ico, d)
                if self.desktop_ic.get():
                    make_shortcut(os.path.join(desktop_dir(), APP_NAME + ".lnk"), exe, ico, d)
            except Exception:
                pass
            self._set(85, "Writing uninstaller…"); write_uninstaller(d)
            self._set(93, "Registering…")
            try:
                size_kb = os.path.getsize(os.path.join(d, APP_EXE)) // 1024
                reg_register(d, size_kb)
            except Exception: pass
            self._set(100, "Done."); self.install_ok = True
        except Exception as e:
            self.install_ok = False
            self.install_err = "%s\n\n%s" % (e, traceback.format_exc()[-400:])
        self.after(400, lambda: self._show(4))

    def _set(self, pct, msg):
        self.after(0, lambda: (self.pb.config(value=pct), self.status.config(text=msg)))

    # navigation --------------------------------------------------------------
    def _show(self, i):
        self.idx = i; self._clear(); self.pages[i]()

    def _sync_next(self):
        if self.idx == 1:
            (self.btn_next.state(["!disabled"]) if self.accepted.get() else self.btn_next.state(["disabled"]))

    def _next(self):
        if self.idx == 1 and not self.accepted.get(): return
        if self.idx == 2:  # from location -> install
            self._show(3); return
        if self.idx < len(self.pages) - 1:
            self._show(self.idx + 1)

    def _back(self):
        if self.idx > 0 and self.idx not in (3, 4):
            self._show(self.idx - 1)

    def _browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(initialdir=self.install_dir.get() or os.path.expanduser("~"))
        if d: self.install_dir.set(os.path.join(d, APP_NAME) if os.path.basename(d) != APP_NAME else d)

    def _cancel(self):
        if messagebox.askyesno(APP_NAME + " Setup", "Cancel installation?"):
            self.destroy()

    def _finish(self):
        if getattr(self, "install_ok", False) and self.launch_after.get():
            try: subprocess.Popen([os.path.join(self.install_dir.get(), APP_EXE)], cwd=self.install_dir.get())
            except Exception: pass
        self.destroy()


def _selftest():
    """Headless check that the bundled payload extracts and copies (writes a result file)."""
    res = os.path.join(os.environ.get("TEMP", "."), "equidex_selftest_result.txt")
    try:
        d = os.path.join(os.environ.get("TEMP", "."), "EQUIDEX_selftest")
        os.makedirs(d, exist_ok=True)
        shutil.copy2(resource(APP_EXE), os.path.join(d, APP_EXE))
        for x in (ICON_FILE, "SETUP.md"):
            shutil.copy2(resource(x), os.path.join(d, x))
        sz = os.path.getsize(os.path.join(d, APP_EXE))
        with open(res, "w") as f:
            f.write("SELFTEST_OK exe_bytes=%d icon=%s setup_md=%s\n" % (
                sz, os.path.exists(os.path.join(d, ICON_FILE)), os.path.exists(os.path.join(d, "SETUP.md"))))
        shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        with open(res, "w") as f:
            f.write("SELFTEST_FAIL %s\n%s" % (e, traceback.format_exc()))


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        run_uninstall()
    elif "--selftest" in sys.argv:
        _selftest()
    else:
        Wizard().mainloop()
