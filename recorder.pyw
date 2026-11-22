"""مسجل شاشة خفيف: زر واحد للتسجيل والإيقاف، والتسجيل نفسه يتم عن طريق ffmpeg."""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

FROZEN = getattr(sys, "frozen", False)  # شغال كـ exe معمول بـ PyInstaller
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)  # مكان ffmpeg المدمج جوه الـ exe
OUT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "ScreenRecords")
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", APP_DIR),
                             "ScreenRecorder", "settings.json")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# الاختيارات: النص اللي بيظهر -> القيمة
RESOLUTIONS = {"720p (أخف)": 1280, "1080p": 1920, "الأصلي (أتقل)": 0}
FPS_CHOICES = {"10": 10, "15": 15, "24": 24, "30": 30, "60 (تقيل)": 60}
CRF_CHOICES = {
    "منخفضة - CRF 34": 34,
    "متوسطة - CRF 28": 28,
    "عالية - CRF 23": 23,
    "عالية جدًا - CRF 18": 18,
}
DEFAULTS = {"res": "1080p", "fps": "15", "crf": "متوسطة - CRF 28", "mic": True}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    # لو قيمة محفوظة مش موجودة في القوائم ارجع للافتراضي
    for key, table in (("res", RESOLUTIONS), ("fps", FPS_CHOICES), ("crf", CRF_CHOICES)):
        if s[key] not in table:
            s[key] = DEFAULTS[key]
    return s


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB = None


def tie_to_app(proc):
    """يربط ffmpeg بالبرنامج: لو البرنامج اتقفل بأي شكل، ffmpeg يقفل معاه."""
    global _JOB
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(ctypes.c_void_p(job), 9,
                                               ctypes.byref(info), ctypes.sizeof(info)):
                return
            _JOB = job
        k32.AssignProcessToJobObject(ctypes.c_void_p(_JOB), ctypes.c_void_p(int(proc._handle)))
    except Exception:
        pass  # مش ضروري للتسجيل نفسه


HOTKEY_NAME = "F9"
_VK_F9 = 0x78


def start_hotkey_thread(on_press, on_fail):
    """يسجّل F9 على مستوى الويندوز كله. on_press بتتنادى من thread تاني."""
    def run():
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        if not u32.RegisterHotKey(None, 1, 0x4000, _VK_F9):  # MOD_NOREPEAT
            on_fail()
            return
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                on_press()
    threading.Thread(target=run, daemon=True).start()


def hide_from_capture(win):
    """يخفي النافذة من التسجيل (ويندوز 10 إصدار 2004 وأحدث). لو مش مدعوم تفضل ظاهرة."""
    try:
        u32 = ctypes.windll.user32
        u32.GetAncestor.restype = ctypes.c_void_p
        hwnd = u32.GetAncestor(ctypes.c_void_p(win.winfo_id()), 2)  # GA_ROOT
        u32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), 0x11)  # EXCLUDEFROMCAPTURE
    except Exception:
        pass


def find_ffmpeg():
    for p in (os.path.join(BUNDLE_DIR, "ffmpeg.exe"),
              os.path.join(APP_DIR, "ffmpeg.exe"),
              os.path.join(APP_DIR, "bin", "ffmpeg.exe"),
              shutil.which("ffmpeg"),
              os.path.join(APP_DIR, "..", "bin", "ffmpeg.exe")):
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def find_mic(ffmpeg):
    """يرجع اسم أول ميكروفون يظهر لـ ffmpeg، أو None."""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, errors="replace",
                           creationflags=NO_WINDOW, timeout=15)
    except Exception:
        return None
    m = re.search(r'"([^"]+)"\s*\(audio\)', r.stderr)
    return m.group(1) if m else None


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.remux = None
        self._ddagrab_ok = None
        self.start_time = 0
        self.out_file = ""
        self.ffmpeg = find_ffmpeg()

        root.title("مسجل الشاشة")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        s = load_settings()
        self.res_var = tk.StringVar(value=s["res"])
        self.fps_var = tk.StringVar(value=s["fps"])
        self.crf_var = tk.StringVar(value=s["crf"])
        self.mic_var = tk.BooleanVar(value=bool(s["mic"]))

        box = tk.Frame(root)
        box.pack(padx=16, pady=(12, 4))
        self.combos = []
        for row, (label, var, table) in enumerate((
                ("الوضوح:", self.res_var, RESOLUTIONS),
                ("الفريمات/ثانية:", self.fps_var, FPS_CHOICES),
                ("الجودة:", self.crf_var, CRF_CHOICES))):
            cb = ttk.Combobox(box, textvariable=var, values=list(table),
                              state="readonly", width=20, justify="center")
            cb.grid(row=row, column=0, pady=2)
            tk.Label(box, text=label, font=("Segoe UI", 10)).grid(
                row=row, column=1, sticky="e", padx=(6, 0))
            self.combos.append(cb)

        self.mic_chk = tk.Checkbutton(root, text="تسجيل صوت الميكروفون",
                                      variable=self.mic_var, font=("Segoe UI", 10))
        self.mic_chk.pack(padx=16, pady=(2, 4))

        self.btn = tk.Button(root, text="● ابدأ التسجيل", width=20,
                             font=("Segoe UI", 12, "bold"), bg="#d9342b",
                             fg="white", activebackground="#b02a22",
                             command=self.toggle)
        self.btn.pack(padx=16, pady=6)

        self.status = tk.Label(root, text="جاهز", font=("Segoe UI", 10))
        self.status.pack(padx=16, pady=(0, 4))

        tk.Button(root, text="فتح مجلد التسجيلات", font=("Segoe UI", 9),
                  command=self.open_folder).pack(padx=16, pady=(0, 4))

        self.hotkey_label = tk.Label(
            root, text=f"اختصار: {HOTKEY_NAME} يبدأ ويوقف التسجيل من أي مكان",
            font=("Segoe UI", 9), fg="#666666")
        self.hotkey_label.pack(padx=16, pady=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # الاختصار شغال في thread منفصل، وبنقرا النتيجة هنا كل شوية (tkinter مش thread-safe)
        self._hotkey_hits = 0
        self._hotkey_failed = False
        start_hotkey_thread(self._on_hotkey, self._on_hotkey_fail)
        self._poll_hotkey()

        if not self.ffmpeg:
            self.btn.config(state="disabled")
            messagebox.showerror(
                "ffmpeg غير موجود",
                "البرنامج محتاج ffmpeg.exe.\n"
                "حط ffmpeg.exe جنب البرنامج في نفس المجلد:\n" + APP_DIR)

    def _on_hotkey(self):
        self._hotkey_hits += 1

    def _on_hotkey_fail(self):
        self._hotkey_failed = True

    def _poll_hotkey(self):
        if self._hotkey_failed:
            self._hotkey_failed = False
            self.hotkey_label.config(
                text=f"الاختصار {HOTKEY_NAME} مش شغال: برنامج تاني ماسكه", fg="#b02a22")
        if self._hotkey_hits:
            self._hotkey_hits = 0
            if str(self.btn["state"]) == "normal":  # مش وقت تجهيز ملف mp4
                self.toggle()
        self.root.after(150, self._poll_hotkey)

    def use_ddagrab(self):
        """بيجرب ddagrab مرة واحدة ويحفظ النتيجة."""
        if self._ddagrab_ok is None:
            self.status.config(text="بيجهّز التسجيل...")
            self.root.update()
            try:
                r = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error",
                                    "-f", "lavfi", "-i", "ddagrab=framerate=5",
                                    "-frames:v", "1", "-vf", "hwdownload,format=bgra",
                                    "-f", "null", "-"],
                                   capture_output=True, creationflags=NO_WINDOW,
                                   timeout=10)
                self._ddagrab_ok = r.returncode == 0
            except Exception:
                self._ddagrab_ok = False
        return self._ddagrab_ok

    def toggle(self):
        if self.proc:
            self.stop()
        else:
            self.start()

    def save_settings(self):
        try:
            os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({"res": self.res_var.get(), "fps": self.fps_var.get(),
                           "crf": self.crf_var.get(), "mic": self.mic_var.get()},
                          f, ensure_ascii=False)
        except Exception:
            pass  # الحفظ مش ضروري للتسجيل

    def set_controls(self, enabled):
        for cb in self.combos:
            cb.config(state="readonly" if enabled else "disabled")
        self.mic_chk.config(state="normal" if enabled else "disabled")

    def start(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.save_settings()
        # التسجيل بيتم في mkv لأنه بيفضل سليم لو البرنامج أو الجهاز وقع،
        # وبعد الإيقاف بيتحوّل لـ mp4 (نسخ بدون إعادة ضغط = ثواني)
        self.out_file = os.path.join(
            OUT_DIR, datetime.now().strftime("Record_%Y-%m-%d_%H-%M-%S.mkv"))

        fps = FPS_CHOICES[self.fps_var.get()]
        crf = CRF_CHOICES[self.crf_var.get()]
        max_w = RESOLUTIONS[self.res_var.get()]
        if max_w:
            # تصغير لحد العرض المختار، وأبعاد زوجية عشان x264
            vf = f"scale=w='trunc(min({max_w},iw)/2)*2':h=-2"
        else:
            vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if self.use_ddagrab():
            # التقاط بكارت الشاشة (Desktop Duplication): أسرع بكتير والماوس ناعم
            cmd += ["-f", "lavfi", "-i", f"ddagrab=framerate={fps}:draw_mouse=1"]
            vf = "hwdownload,format=bgra," + vf
        else:  # ويندوز قديم أو جلسة Remote Desktop
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                    "-i", "desktop"]

        mic = None
        if self.mic_var.get():
            self.status.config(text="بدور على الميكروفون...")
            self.root.update()
            mic = find_mic(self.ffmpeg)
            if mic:
                cmd += ["-f", "dshow", "-audio_buffer_size", "50",
                        "-i", "audio=" + mic]

        cmd += ["-c:v", "libx264", "-preset", "ultrafast",  # بدون zerolatency: كانت بتقلل الفريمات للنص مع الميكروفون
                "-crf", str(crf), "-pix_fmt", "yuv420p", "-vf", vf,
                "-g", str(fps * 2)]  # keyframe كل ثانيتين
        if mic:
            cmd += ["-c:a", "aac", "-b:a", "96k"]
        cmd += [self.out_file]

        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE,
                                         creationflags=NO_WINDOW)
        except Exception as e:
            self.proc = None
            messagebox.showerror("فشل بدء التسجيل", f"ffmpeg ما اشتغلش:\n{e}")
            self.status.config(text="جاهز")
            return

        tie_to_app(self.proc)
        self.start_time = time.time()
        self.set_controls(False)
        self.btn.config(text="■ إيقاف التسجيل", bg="#333333",
                        activebackground="#111111")
        self.note = "" if mic or not self.mic_var.get() else "  (بدون صوت: مفيش ميكروفون)"
        self.show_bar()
        self.tick()

    # ---- شريط صغير عائم فيه الوقت وزر الإيقاف، بيظهر بدل النافذة أثناء التسجيل ----
    def show_bar(self):
        self.root.withdraw()
        bar = self.bar = tk.Toplevel(self.root, bg="#222222")
        bar.overrideredirect(True)        # من غير شريط عنوان
        bar.attributes("-topmost", True)  # فوق كل النوافذ
        self.bar_label = tk.Label(bar, text="● 00:00", fg="#ff4d4d", bg="#222222",
                                  font=("Segoe UI", 11, "bold"))
        self.bar_label.pack(side="left", padx=(10, 6), pady=4)
        tk.Button(bar, text=f"■ إيقاف ({HOTKEY_NAME})", font=("Segoe UI", 10, "bold"),
                  bg="#d9342b", fg="white", activebackground="#b02a22",
                  relief="flat", command=self.stop).pack(side="left", padx=(0, 6), pady=4)
        bar.update_idletasks()
        x = (bar.winfo_screenwidth() - bar.winfo_reqwidth()) // 2
        bar.geometry(f"+{x}+8")  # أعلى نص الشاشة
        # السحب بالماوس لتغيير مكان الشريط
        for w in (bar, self.bar_label):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        bar.update()
        hide_from_capture(bar)

    def hide_bar(self):
        if getattr(self, "bar", None):
            self.bar.destroy()
            self.bar = None
        self.root.deiconify()
        self.root.lift()

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.bar.winfo_x(), e.y_root - self.bar.winfo_y()

    def _drag_move(self, e):
        self.bar.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def tick(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:  # ffmpeg وقف لوحده = حصل خطأ
            err = self.proc.stderr.read().decode(errors="replace").strip()
            self.proc = None
            self.hide_bar()
            self.reset_button()
            messagebox.showerror("التسجيل وقف",
                                 "ffmpeg وقف فجأة:\n" + (err[-600:] or "بدون رسالة خطأ"))
            return
        s = int(time.time() - self.start_time)
        t = f"{s // 60:02d}:{s % 60:02d}"
        self.status.config(text=f"بيسجل  {t}{self.note}")
        if getattr(self, "bar", None):
            self.bar_label.config(text="● " + t)
        self.root.after(500, self.tick)

    def stop(self):
        if not self.proc:
            return
        self.hide_bar()
        proc, self.proc = self.proc, None
        self.status.config(text="بيحفظ الملف...")
        self.root.update()
        try:
            proc.stdin.write(b"q")  # إيقاف نظيف عشان ملف mp4 يتقفل صح
            proc.stdin.flush()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        self.reset_button()
        self.btn.config(state="disabled")
        self.status.config(text="بيجهّز ملف mp4...")
        mkv = self.out_file
        mp4 = os.path.splitext(mkv)[0] + ".mp4"
        try:
            self.remux = subprocess.Popen(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", mkv,
                 "-c", "copy", "-movflags", "+faststart", mp4],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            tie_to_app(self.remux)
        except Exception:
            self.remux = None
        self.wait_remux(mkv, mp4)

    def wait_remux(self, mkv, mp4):
        if self.remux and self.remux.poll() is None:
            self.root.after(300, self.wait_remux, mkv, mp4)
            return
        ok = bool(self.remux) and self.remux.returncode == 0 and os.path.isfile(mp4)
        self.remux = None
        if ok:
            try:
                os.remove(mkv)
            except OSError:
                pass
            self.out_file = mp4
        else:  # التحويل فشل: نسيب ملف mkv نفسه، وهو شغال عادي
            try:
                os.remove(mp4)
            except OSError:
                pass
        self.btn.config(state="normal")
        self.status.config(text="اتحفظ: " + os.path.basename(self.out_file))

    def reset_button(self):
        self.set_controls(True)
        self.btn.config(text="● ابدأ التسجيل", bg="#d9342b",
                        activebackground="#b02a22")
        self.status.config(text="جاهز")

    def open_folder(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        os.startfile(OUT_DIR)

    def on_close(self):
        if self.proc:
            if not messagebox.askyesno("التسجيل شغال", "توقف التسجيل وتحفظه وتقفل؟"):
                return
            self.stop()
        while self.remux:  # استنى تحويل mp4 يخلص قبل القفل
            self.root.update()
            time.sleep(0.1)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
    sys.exit(0)
