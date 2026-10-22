import os
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Windowed builds have no console: give yt-dlp somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import yt_dlp

APP = "GrabIt"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SEM = threading.Semaphore(3)


def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


FFMPEG_DIR = os.path.join(base_dir(), "bin")

QUALITIES = {
    "Best quality": "bv*+ba/b",
    "Up to 1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "Up to 720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "Up to 480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "Audio only (MP3)": "ba/b",
}


def find_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,);") if m else None


def fmt_size(n):
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.2f TB" % n


def fmt_eta(s):
    if s is None:
        return ""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, sec)) if h else ("%d:%02d" % (m, sec))


class Job:
    def __init__(self, jid, url):
        self.id = jid
        self.url = url
        self.title = url
        self.status = "Queued"
        self.pct = 0.0
        self.size = ""
        self.speed = ""
        self.eta = ""
        self.error = ""
        self.stop = False


class Stopped(yt_dlp.utils.DownloadCancelled):
    pass


class QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def run_job(job, outdir, quality, playlist):
    def hook(d):
        if job.stop:
            raise Stopped()
        st = d.get("status")
        info = d.get("info_dict") or {}
        if info.get("title"):
            job.title = info["title"]
        if st == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if tot:
                job.pct = done * 100.0 / tot
                job.size = fmt_size(done) + " / " + fmt_size(tot)
            else:
                job.size = fmt_size(done)
            sp = d.get("speed")
            job.speed = (fmt_size(sp) + "/s") if sp else ""
            job.eta = fmt_eta(d.get("eta"))
            job.status = "Downloading"
        elif st == "finished":
            job.status = "Processing"
            job.pct = 100.0
            job.speed = ""
            job.eta = ""

    opts = {
        "format": QUALITIES.get(quality, QUALITIES["Best quality"]),
        "outtmpl": os.path.join(outdir, "%(title).150s [%(id)s].%(ext)s"),
        "noplaylist": not playlist,
        "progress_hooks": [hook],
        "ffmpeg_location": FFMPEG_DIR,
        "merge_output_format": "mp4",
        "retries": 20,
        "fragment_retries": 50,
        "file_access_retries": 5,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "logger": QuietLogger(),
    }
    if quality.startswith("Audio"):
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    try:
        job.status = "Starting"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job.url])
        job.status = "Done"
        job.pct = 100.0
        job.speed = ""
        job.eta = ""
    except yt_dlp.utils.DownloadCancelled:
        job.status = "Stopped"
        job.speed = ""
        job.eta = ""
    except yt_dlp.utils.DownloadError as e:
        msg = ANSI_RE.sub("", str(e)).replace("ERROR: ", "").strip()
        job.error = msg
        job.status = "Failed"
        job.speed = ""
        job.eta = ""
    except Exception as e:
        job.error = "%s: %s" % (type(e).__name__, e)
        job.status = "Failed"
        job.speed = ""
        job.eta = ""


class App:
    ACTIVE = ("Starting", "Queued", "Downloading", "Processing")

    def __init__(self, root):
        self.root = root
        root.title("GrabIt - video and file downloader")
        root.geometry("920x540")
        root.minsize(760, 420)
        self.jobs = []
        self.next_id = 1
        self.last_clip = ""

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Link:").grid(row=0, column=0, sticky="w")
        self.url = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<Return>", lambda e: self.add())
        ttk.Button(top, text="Paste", command=self.paste).grid(row=0, column=2)
        ttk.Button(top, text="Download", command=self.add).grid(row=0, column=3, padx=(6, 0))
        top.columnconfigure(1, weight=1)

        opt = ttk.Frame(root, padding=(10, 0))
        opt.pack(fill="x")
        ttk.Label(opt, text="Quality:").pack(side="left")
        self.q = tk.StringVar(value="Best quality")
        ttk.Combobox(opt, textvariable=self.q, values=list(QUALITIES), state="readonly", width=18).pack(
            side="left", padx=6
        )
        self.pl = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Whole playlist", variable=self.pl).pack(side="left", padx=10)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Auto-download copied links", variable=self.auto).pack(side="left", padx=10)

        fr = ttk.Frame(root, padding=10)
        fr.pack(fill="x")
        ttk.Label(fr, text="Save to:").pack(side="left")
        self.folder = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "GrabIt"))
        ttk.Entry(fr, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(fr, text="Browse", command=self.browse).pack(side="left")
        ttk.Button(fr, text="Open folder", command=self.open_folder).pack(side="left", padx=(6, 0))

        cols = ("title", "status", "pct", "size", "speed", "eta")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=12, selectmode="extended")
        heads = {"title": "Name", "status": "Status", "pct": "%", "size": "Size", "speed": "Speed", "eta": "ETA"}
        widths = {"title": 330, "status": 100, "pct": 55, "size": 150, "speed": 100, "eta": 70}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "title"))
        self.tree.pack(fill="both", expand=True, padx=10)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        bar = ttk.Frame(root, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="Stop", command=self.stop_sel).pack(side="left")
        ttk.Button(bar, text="Resume / Retry", command=self.resume_sel).pack(side="left", padx=6)
        ttk.Button(bar, text="Remove", command=self.remove_sel).pack(side="left")
        ttk.Button(bar, text="Clear finished", command=self.clear_done).pack(side="left", padx=6)
        self.note = tk.StringVar(value="Paste a movie or video page link, or a direct file link, then press Download.")
        ttk.Label(root, textvariable=self.note, wraplength=880, padding=(10, 0, 10, 10)).pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_clip()
        self.refresh()

    # ---- helpers
    def clip_text(self):
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            return ""

    def paste(self):
        u = find_url(self.clip_text())
        if u:
            self.url.set(u)
        else:
            self.note.set("No link found in the clipboard.")

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or os.path.expanduser("~"))
        if d:
            self.folder.set(d)

    def open_folder(self):
        d = self.folder.get()
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            pass

    def poll_clip(self):
        u = find_url(self.clip_text())
        if u and u != self.last_clip:
            self.last_clip = u
            if self.auto.get():
                self.add(u)
            else:
                self.url.set(u)
                self.note.set("Link detected from your clipboard. Press Download to start.")
        self.root.after(1000, self.poll_clip)

    # ---- jobs
    def add(self, url=None):
        url = url or find_url(self.url.get())
        if not url:
            self.note.set("Paste a link that starts with http:// or https://")
            return
        for j in self.jobs:
            if j.url == url and j.status in self.ACTIVE:
                self.note.set("That link is already downloading.")
                return
        job = Job(self.next_id, url)
        self.next_id += 1
        self.jobs.append(job)
        self.url.set("")
        self.start(job)

    def start(self, job):
        job.stop = False
        job.error = ""
        job.status = "Queued"
        outdir = self.folder.get().strip() or os.path.join(os.path.expanduser("~"), "Downloads", "GrabIt")
        quality = self.q.get()
        playlist = self.pl.get()
        t = threading.Thread(target=self.work, args=(job, outdir, quality, playlist), daemon=True)
        t.start()

    def work(self, job, outdir, quality, playlist):
        with SEM:
            if job.stop:
                job.status = "Stopped"
                return
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                job.error = "Cannot create folder: %s" % e
                job.status = "Failed"
                return
            run_job(job, outdir, quality, playlist)

    def selected(self):
        ids = {int(i) for i in self.tree.selection()}
        return [j for j in self.jobs if j.id in ids]

    def stop_sel(self):
        for j in self.selected():
            if j.status in self.ACTIVE:
                j.stop = True
                if j.status == "Queued":
                    j.status = "Stopped"

    def resume_sel(self):
        for j in self.selected():
            if j.status in ("Stopped", "Failed"):
                self.start(j)

    def remove_sel(self):
        for j in self.selected():
            j.stop = True
            self.jobs.remove(j)
            if self.tree.exists(str(j.id)):
                self.tree.delete(str(j.id))

    def clear_done(self):
        for j in [j for j in self.jobs if j.status == "Done"]:
            self.jobs.remove(j)
            if self.tree.exists(str(j.id)):
                self.tree.delete(str(j.id))

    def on_select(self, _event=None):
        sel = self.selected()
        if len(sel) == 1:
            j = sel[0]
            self.note.set(("Error: " + j.error) if j.error else j.url)

    def refresh(self):
        for j in self.jobs:
            iid = str(j.id)
            vals = (j.title[:90], j.status, "%d%%" % j.pct, j.size, j.speed, j.eta)
            if self.tree.exists(iid):
                self.tree.item(iid, values=vals)
            else:
                self.tree.insert("", 0, iid=iid, values=vals)
        self.root.after(500, self.refresh)

    def on_close(self):
        if any(j.status in self.ACTIVE for j in self.jobs):
            if not messagebox.askyesno(APP, "Downloads are still running. Quit anyway?\nYou can resume them later."):
                return
        for j in self.jobs:
            j.stop = True
        self.root.destroy()


def main():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
