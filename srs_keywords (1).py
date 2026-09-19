#!/usr/bin/env python3
"""
Spaced-repetition English keywords trainer
------------------------------------------
* Cards are stored in keywords.json (next to this script).
* Scheduling: SM-2 algorithm (the one Anki is based on), with a 10 minute
  re-learn step for wrong answers and an in-session retry.
* Multiple choice: for keyword A with meaning A', the wrong choices are the
  meanings of OTHER cards that are the CLOSEST TEXT to A' (B', C', D'...),
  not random ones (X', Y', Z'), so the test is hard.
* Sound: gTTS (Google, online, cached to ./tts_cache so it also works offline
  for words you already heard) -> falls back to pyttsx3 (offline) automatically.
  Speed + volume sliders. Fine speed control for gTTS uses ffmpeg if installed.

Install:  pip install gTTS pygame pyttsx3      (ffmpeg optional)

Run:  python srs_keywords.py
"""
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
CACHE_DIR = os.path.join(BASE_DIR, "tts_cache")
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json")
NUM_CHOICES = 4
RELEARN_MINUTES = 10

SEED_CARDS = [
    ("ephemeral", "lasting for a very short time"),
    ("ubiquitous", "present or found everywhere at the same time"),
    ("meticulous", "showing great attention to detail; very careful"),
    ("benevolent", "kind, generous and wanting to help other people"),
]


# ----------------------------------------------------------------------------
# Core logic (no GUI)
# ----------------------------------------------------------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_card(keyword, meaning):
    return {
        "keyword": keyword.strip(),
        "meaning": meaning.strip(),
        "ease": 2.5,          # SM-2 easiness factor
        "interval": 0,        # days
        "reps": 0,            # successful reviews in a row
        "lapses": 0,          # times forgotten
        "due": now_iso(),     # new cards are due immediately
    }


def load_cards():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                cards = json.load(f).get("cards", [])
            if cards:
                return cards
        except (json.JSONDecodeError, OSError):
            pass
    cards = [new_card(k, m) for k, m in SEED_CARDS]   # first run: 4 initial cards
    save_cards(cards)
    return cards


def save_cards(cards):
    folder = os.path.dirname(DATA_FILE)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"cards": cards}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)   # atomic write, no corrupted file on crash


def _tokens(text):
    return set(re.findall(r"[a-z']+", text.lower()))


def similarity(a, b):
    """0..1 text closeness: character-level + word-overlap."""
    a, b = a.lower(), b.lower()
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _tokens(a), _tokens(b)
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return 0.6 * seq + 0.4 * jac


def pick_choices(card, cards, n=NUM_CHOICES):
    """Correct meaning + the (n-1) other meanings closest to it, shuffled."""
    others = [c for c in cards
              if c["keyword"].lower() != card["keyword"].lower()
              and c["meaning"].strip().lower() != card["meaning"].strip().lower()]
    others.sort(key=lambda c: similarity(card["meaning"], c["meaning"]), reverse=True)
    choices = [card["meaning"]] + [c["meaning"] for c in others[: n - 1]]
    random.shuffle(choices)
    return choices


def schedule(card, correct, seconds):
    """SM-2. Quality: 5 fast & right, 4 right, 3 slow & right, 1 wrong."""
    if correct:
        q = 5 if seconds <= 4 else 4 if seconds <= 10 else 3
    else:
        q = 1
    card["ease"] = max(1.3, card["ease"] + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    now = datetime.now()
    if q < 3:                                   # forgotten -> re-learn
        card["reps"] = 0
        card["interval"] = 0
        card["lapses"] += 1
        due = now + timedelta(minutes=RELEARN_MINUTES)
    else:
        if card["reps"] == 0:
            interval = 1
        elif card["reps"] == 1:
            interval = 3
        else:
            interval = max(card["interval"] + 1, round(card["interval"] * card["ease"]))
        card["reps"] += 1
        card["interval"] = interval
        due = now + timedelta(days=interval)
    card["due"] = due.isoformat(timespec="seconds")


def is_due(card):
    return datetime.fromisoformat(card["due"]) <= datetime.now()


def parse_bulk(text):
    """Lines of 'keyword : meaning' -> list of (keyword, meaning)."""
    pairs = []
    for line in text.splitlines():
        if ":" in line:
            k, m = line.split(":", 1)
            if k.strip() and m.strip():
                pairs.append((k.strip(), m.strip()))
    return pairs


def human_due(card):
    delta = datetime.fromisoformat(card["due"]) - datetime.now()
    s = delta.total_seconds()
    if s <= 0:
        return "now"
    if s < 3600:
        return f"{int(s // 60) + 1} min"
    if s < 86400:
        return f"{int(s // 3600)} h"
    return f"{int(s // 86400)} d"


# ----------------------------------------------------------------------------
# TEXT-TO-SPEECH
# ----------------------------------------------------------------------------
DEFAULT_SETTINGS = {"speed": 1.0, "volume": 0.8, "autoplay": True, "read_meaning": False}


def load_settings():
    st = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            st.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return st


def save_settings(st):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    except OSError:
        pass


def _has(module):
    return importlib.util.find_spec(module) is not None


class Speaker:
    """gTTS (online, cached mp3 played with pygame) -> pyttsx3 (offline) fallback.
    speed: 0.5 .. 1.5   volume: 0.0 .. 1.0.   Runs in a background thread."""

    def __init__(self, speed=1.0, volume=0.8):
        self.speed = speed
        self.volume = volume
        self.status = "Engine: ready"
        self._gen = 0                      # bumped on every speak()/stop() to cancel old audio
        self._engine = None                # current pyttsx3 engine (so it can be stopped)
        self._pyttsx3_lock = threading.Lock()
        self._mixer_ok = False
        self._online, self._online_at = None, 0.0
        self._used_network = False

    # ---- public API ----
    def speak(self, text):
        text = (text or "").strip()
        if not text:
            return
        self.stop()
        threading.Thread(target=self._run, args=(text, self._gen), daemon=True).start()

    def stop(self):
        self._gen += 1
        if self._mixer_ok:
            try:
                import pygame
                pygame.mixer.music.stop()
            except Exception:
                pass
        eng = self._engine
        if eng is not None:
            try:
                eng.stop()
            except Exception:
                pass

    def set_volume(self, v):
        self.volume = max(0.0, min(1.0, v))
        if self._mixer_ok:                 # live change while gTTS audio is playing
            try:
                import pygame
                pygame.mixer.music.set_volume(self.volume)
            except Exception:
                pass

    def set_speed(self, v):
        self.speed = max(0.5, min(1.5, v))

    # ---- internals ----
    def _run(self, text, gen):
        if _has("gtts") and _has("pygame"):
            try:
                path = self._gtts_file(text)
                if gen != self._gen:
                    return
                self.status = ("🔊 gTTS (online)" if self._used_network else "🔊 gTTS (cached, offline OK)")
                self._play_file(path, gen)
                return
            except Exception:
                pass                       # no internet / gTTS error / audio device error -> fallback
        if gen == self._gen:
            self._speak_pyttsx3(text, gen)

    def _is_online(self):
        if self._online is not None and time.monotonic() - self._online_at < 20:
            return self._online
        try:
            socket.create_connection(("translate.google.com", 443), timeout=1.5).close()
            ok = True
        except OSError:
            ok = False
        self._online, self._online_at = ok, time.monotonic()
        return ok

    def _ensure_gtts(self, text, slow, path):
        if os.path.exists(path):
            return path
        if not self._is_online():
            raise ConnectionError("offline")
        from gtts import gTTS
        tmp = path + ".part"
        try:
            gTTS(text=text, lang="en", slow=slow).save(tmp)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)                 # never keep a half-written file
            raise
        self._used_network = True
        return path

    def _gtts_file(self, text):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._used_network = False
        key = hashlib.sha1(f"en|{text}".encode("utf-8")).hexdigest()
        base = os.path.join(CACHE_DIR, key + ".mp3")
        speed = self.speed
        if abs(speed - 1.0) < 0.03:
            return self._ensure_gtts(text, False, base)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:                                   # exact speed via ffmpeg 'atempo'
            target = os.path.join(CACHE_DIR, f"{key}_{speed:.2f}.mp3")
            if os.path.exists(target):
                return target
            self._ensure_gtts(text, False, base)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            tmp = target + ".part.mp3"
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", base,
                                "-filter:a", f"atempo={speed:.2f}", tmp],
                               capture_output=True, timeout=30, creationflags=flags)
            if r.returncode == 0 and os.path.exists(tmp):
                os.replace(tmp, target)
                return target
            return base
        # no ffmpeg: gTTS only knows normal / slow
        if speed < 0.85:
            return self._ensure_gtts(text, True, os.path.join(CACHE_DIR, key + "_slow.mp3"))
        return self._ensure_gtts(text, False, base)

    def _play_file(self, path, gen):
        import pygame
        if not self._mixer_ok:
            pygame.mixer.init()
            self._mixer_ok = True
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(self.volume)
        if gen != self._gen:
            return
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy() and gen == self._gen:
            time.sleep(0.05)

    def _speak_pyttsx3(self, text, gen):
        try:
            import pyttsx3
        except ImportError:
            self.status = "⚠ No speech engine — pip install gTTS pygame pyttsx3"
            return
        with self._pyttsx3_lock:
            if gen != self._gen:
                return
            try:
                engine = pyttsx3.init()
                self._engine = engine
                engine.setProperty("rate", int(190 * self.speed))
                engine.setProperty("volume", self.volume)
                self.status = "🔈 pyttsx3 (offline fallback)"
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                self.status = f"⚠ Speech failed: {str(e)[:60]}"
            finally:
                self._engine = None


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("SLR — Keyword Trainer")
            self.geometry("800x760")
            self.minsize(700, 660)
            self.cards = load_cards()
            self.settings = load_settings()
            self.speaker = Speaker(self.settings["speed"], self.settings["volume"])
            self.speed_var = tk.DoubleVar(value=self.settings["speed"])
            self.vol_var = tk.DoubleVar(value=self.settings["volume"] * 100)
            self.auto_var = tk.BooleanVar(value=self.settings["autoplay"])
            self.meaning_var = tk.BooleanVar(value=self.settings["read_meaning"])
            self.engine_var = tk.StringVar(value=self.speaker.status)
            self.queue, self.retry = [], set()
            self.current, self.answered, self.practice = None, False, False
            self.shown_at, self.choices, self.dirty = 0.0, [], False

            self.build_header()
            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True)
            self.study_tab, self.manage_tab = ttk.Frame(self.nb), ttk.Frame(self.nb)
            self.nb.add(self.study_tab, text="  Study  ")
            self.nb.add(self.manage_tab, text="  Cards  ")
            self.nb.bind("<<NotebookTabChanged>>", self.on_tab)

            self.build_study()
            self.build_manage()
            self.start_session()

            for i in range(NUM_CHOICES):
                self.bind(str(i + 1), lambda e, i=i: self.on_number_key(i))
            self.bind("<Return>", lambda e: self.next_card() if self.answered else None)
            self.bind("s", lambda e: self.on_speak_key())
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.poll_status()

        # ---------------- Header / logo ----------------
        def build_header(self):
            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=12, pady=(10, 4))
            logo = tk.Canvas(bar, width=92, height=44, highlightthickness=0)
            logo.pack(side="left")
            # rounded badge
            x1, y1, x2, y2, r = 2, 2, 90, 42, 12
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            logo.create_polygon(pts, smooth=True, fill="#1d3557", outline="")
            logo.create_text(46, 21, text="SLR", fill="white", font=("Segoe UI", 20, "bold"))
            logo.create_line(22, 36, 70, 36, fill="#e63946", width=3)
            title = ttk.Frame(bar)
            title.pack(side="left", padx=12)
            ttk.Label(title, text="Keyword Trainer", font=("Segoe UI", 14, "bold")).pack(anchor="w")
            ttk.Label(title, text="spaced repetition", foreground="gray").pack(anchor="w")

        # ---------------- Copy helpers ----------------
        def copy_text(self, text):
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()

        def popup_copy(self, event, text_getter, label="Copy"):
            text = text_getter()
            if not text:
                return
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(label=label, command=lambda: self.copy_text(text))
            menu.add_command(label="🔊 Speak", command=lambda: self.speaker.speak(text))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def on_speak_key(self):
            if self.nb.index("current") == 0 and self.current:
                self.speaker.speak(self.current["keyword"])

        def on_speed(self, value):
            v = round(float(value) / 0.05) * 0.05
            self.speaker.set_speed(v)
            self.speed_lbl.config(text=f"{v:.2f}x")

        def on_volume(self, value):
            v = float(value) / 100
            self.speaker.set_volume(v)
            self.vol_lbl.config(text=f"{int(float(value))}%")

        def persist_settings(self, _event=None):
            save_settings({"speed": round(self.speaker.speed, 2), "volume": round(self.speaker.volume, 2),
                           "autoplay": self.auto_var.get(), "read_meaning": self.meaning_var.get()})

        def poll_status(self):
            self.engine_var.set(self.speaker.status)
            self.after(300, self.poll_status)

        def on_close(self):
            self.speaker.stop()
            self.persist_settings()
            self.destroy()

        def on_number_key(self, i):
            if self.nb.index("current") == 0:      # don't hijack digits typed in the Cards tab
                self.answer(i)

        # ---------------- Study tab ----------------
        def build_study(self):
            f = self.study_tab
            self.status = ttk.Label(f, text="", font=("Segoe UI", 10))
            self.status.pack(anchor="w", padx=16, pady=(12, 0))
            kw_row = ttk.Frame(f)
            kw_row.pack(pady=(28, 18))
            # read-only Entry: text can be selected with the mouse and copied (Ctrl+C / right-click)
            self.keyword_var = tk.StringVar()
            self.keyword_lbl = tk.Entry(kw_row, textvariable=self.keyword_var, state="readonly",
                                        readonlybackground=self.cget("bg"), relief="flat",
                                        justify="center", width=20, font=("Segoe UI", 28, "bold"),
                                        highlightthickness=0, cursor="xterm")
            self.keyword_lbl.pack(side="left")
            ttk.Button(kw_row, text="🔊", width=4,
                       command=lambda: self.current and self.speaker.speak(self.keyword_var.get())
                       ).pack(side="left", padx=(8, 0))
            ttk.Button(kw_row, text="Copy", width=6,
                       command=lambda: self.copy_text(self.keyword_var.get())).pack(side="left", padx=8)
            self.keyword_lbl.bind("<Button-3>", lambda e: self.popup_copy(
                e, lambda: self.keyword_var.get(), "Copy keyword"))
            self.keyword_lbl.bind("<Button-2>", lambda e: self.popup_copy(
                e, lambda: self.keyword_var.get(), "Copy keyword"))
            self.btn_frame = ttk.Frame(f)
            self.btn_frame.pack(fill="x", padx=30)
            self.buttons = []
            for i in range(NUM_CHOICES):
                b = tk.Button(self.btn_frame, text="", font=("Segoe UI", 12), anchor="w",
                              justify="left", wraplength=640, padx=12, pady=8,
                              relief="groove", command=lambda i=i: self.answer(i))
                b.pack(fill="x", pady=4)
                for ev in ("<Button-3>", "<Button-2>"):   # right-click a choice -> copy its text
                    b.bind(ev, lambda e, i=i: self.popup_copy(
                        e, lambda: self.choices[i] if i < len(self.choices) else "", "Copy text"))
                self.buttons.append(b)
            self.default_bg = self.buttons[0].cget("bg")
            self.feedback = ttk.Label(f, text="", font=("Segoe UI", 11))
            self.feedback.pack(pady=10)
            row = ttk.Frame(f)
            row.pack()
            self.next_btn = ttk.Button(row, text="Next  (Enter)", command=self.next_card)
            self.next_btn.pack(side="left", padx=6)
            self.practice_btn = ttk.Button(row, text="Practice all (no scheduling)",
                                           command=self.start_practice)
            self.practice_btn.pack(side="left", padx=6)
            snd = ttk.LabelFrame(f, text="Sound")
            snd.pack(fill="x", padx=30, pady=(12, 0))
            r1 = ttk.Frame(snd)
            r1.pack(fill="x", padx=8, pady=(4, 0))
            ttk.Label(r1, text="Speed").pack(side="left")
            sp = ttk.Scale(r1, from_=0.5, to=1.5, variable=self.speed_var, command=self.on_speed)
            sp.pack(side="left", fill="x", expand=True, padx=8)
            self.speed_lbl = ttk.Label(r1, width=6, text=f"{self.settings['speed']:.2f}x")
            self.speed_lbl.pack(side="left", padx=(0, 16))
            ttk.Label(r1, text="Volume").pack(side="left")
            vl = ttk.Scale(r1, from_=0, to=100, variable=self.vol_var, command=self.on_volume)
            vl.pack(side="left", fill="x", expand=True, padx=8)
            self.vol_lbl = ttk.Label(r1, width=5, text=f"{int(self.settings['volume'] * 100)}%")
            self.vol_lbl.pack(side="left")
            for w in (sp, vl):
                w.bind("<ButtonRelease-1>", self.persist_settings)
            r2 = ttk.Frame(snd)
            r2.pack(fill="x", padx=8, pady=(0, 6))
            ttk.Checkbutton(r2, text="Auto-play keyword", variable=self.auto_var,
                            command=self.persist_settings).pack(side="left")
            ttk.Checkbutton(r2, text="Read meaning after answer", variable=self.meaning_var,
                            command=self.persist_settings).pack(side="left", padx=12)
            ttk.Label(r2, textvariable=self.engine_var, foreground="gray").pack(side="right")

            ttk.Label(f, text="Keys: 1-4 answer  •  S speak  •  Enter next  •  right-click any text to copy/speak",
                      foreground="gray").pack(pady=(10, 0))

        def start_session(self):
            self.practice = False
            self.retry.clear()
            self.dirty = False
            due = [c for c in self.cards if is_due(c)]
            due.sort(key=lambda c: c["due"])
            self.queue = due
            self.next_card()

        def start_practice(self):
            self.practice = True
            self.retry.clear()
            self.queue = random.sample(self.cards, len(self.cards))
            self.next_card()

        def update_status(self):
            due = sum(1 for c in self.cards if is_due(c))
            mature = sum(1 for c in self.cards if c["interval"] >= 21)
            mode = "PRACTICE  |  " if self.practice else ""
            self.status.config(
                text=f"{mode}Left this session: {len(self.queue) + (0 if self.answered or not self.current else 1)}"
                     f"   |   Due now: {due}   |   Total: {len(self.cards)}   |   Mastered (21d+): {mature}")

        def next_card(self):
            self.answered = False
            self.feedback.config(text="")
            for b in self.buttons:
                b.config(bg=self.default_bg, state="normal")
            if not self.queue:
                self.current = None
                self.keyword_var.set("All done! 🎉")
                for b in self.buttons:
                    b.config(text="", state="disabled")
                upcoming = sorted((c for c in self.cards if not is_due(c)), key=lambda c: c["due"])
                self.feedback.config(
                    text=f"Next review in {human_due(upcoming[0])}" if upcoming else "No cards yet.")
                self.update_status()
                return
            self.current = self.queue.pop(0)
            self.choices = pick_choices(self.current, self.cards)
            self.keyword_var.set(self.current["keyword"])
            for i, b in enumerate(self.buttons):
                if i < len(self.choices):
                    b.config(text=f"{i + 1}.  {self.choices[i]}", state="normal")
                    b.pack(fill="x", pady=4)
                else:
                    b.pack_forget()
            self.shown_at = time.monotonic()
            self.update_status()
            if self.auto_var.get():
                self.speaker.speak(self.current["keyword"])

        def answer(self, idx):
            if self.answered or self.current is None or idx >= len(self.choices):
                return
            self.answered = True
            seconds = time.monotonic() - self.shown_at
            card = self.current
            correct = self.choices[idx] == card["meaning"]
            for i, b in enumerate(self.buttons[: len(self.choices)]):
                if self.choices[i] == card["meaning"]:
                    b.config(bg="#b7e4c7")
                elif i == idx:
                    b.config(bg="#f5b7b1")
            key = card["keyword"]
            if correct:
                self.feedback.config(text="✅ Correct!", foreground="#2d6a4f")
            else:
                self.feedback.config(text="❌ Wrong — correct answer highlighted", foreground="#b03a2e")

            if self.meaning_var.get():
                self.speaker.speak(card["meaning"])
            if not self.practice:
                if key in self.retry:            # second try in same session: no extra penalty
                    if correct:
                        self.retry.discard(key)
                elif correct:
                    schedule(card, True, seconds)
                else:
                    schedule(card, False, seconds)
                    self.retry.add(key)
                if not correct:                  # see it again a few cards later
                    self.queue.insert(min(3, len(self.queue)), card)
                save_cards(self.cards)
            elif not correct:
                self.queue.insert(min(3, len(self.queue)), card)
            self.update_status()

        # ---------------- Manage tab ----------------
        def build_manage(self):
            f = self.manage_tab
            top = ttk.Frame(f)
            top.pack(fill="x", padx=12, pady=10)
            ttk.Label(top, text="Keyword").grid(row=0, column=0, sticky="w")
            ttk.Label(top, text="Meaning").grid(row=0, column=1, sticky="w")
            self.kw_entry = ttk.Entry(top, width=22)
            self.mn_entry = ttk.Entry(top, width=60)
            self.kw_entry.grid(row=1, column=0, padx=(0, 8))
            self.mn_entry.grid(row=1, column=1, padx=(0, 8), sticky="ew")
            ttk.Button(top, text="Add", command=self.add_one).grid(row=1, column=2)
            top.columnconfigure(1, weight=1)
            self.mn_entry.bind("<Return>", lambda e: self.add_one())

            cols = ("keyword", "meaning", "due", "interval")
            self.tree = ttk.Treeview(f, columns=cols, show="headings", height=10)
            for c, w in zip(cols, (140, 380, 70, 70)):
                self.tree.heading(c, text=c.capitalize())
                self.tree.column(c, width=w, anchor="w")
            self.tree.pack(fill="both", expand=True, padx=12)

            btns = ttk.Frame(f)
            btns.pack(fill="x", padx=12, pady=6)
            ttk.Button(btns, text="Delete selected", command=self.delete_selected).pack(side="left")
            ttk.Button(btns, text="Reset progress of selected",
                       command=self.reset_selected).pack(side="left", padx=6)

            ttk.Label(f, text="Bulk add — one per line:  keyword : meaning").pack(anchor="w", padx=12)
            self.bulk = tk.Text(f, height=5)
            self.bulk.pack(fill="x", padx=12)
            ttk.Button(f, text="Import lines", command=self.bulk_add).pack(anchor="e", padx=12, pady=6)
            self.refresh_tree()

        def refresh_tree(self):
            self.tree.delete(*self.tree.get_children())
            for i, c in enumerate(self.cards):
                self.tree.insert("", "end", iid=str(i),
                                 values=(c["keyword"], c["meaning"], human_due(c), f'{c["interval"]} d'))

        def _exists(self, kw):
            return any(c["keyword"].lower() == kw.lower() for c in self.cards)

        def add_one(self):
            kw, mn = self.kw_entry.get().strip(), self.mn_entry.get().strip()
            if not kw or not mn:
                return
            if self._exists(kw):
                messagebox.showinfo("Duplicate", f'"{kw}" already exists.')
                return
            self.cards.append(new_card(kw, mn))
            self.after_change()
            self.kw_entry.delete(0, "end")
            self.mn_entry.delete(0, "end")
            self.kw_entry.focus()

        def bulk_add(self):
            added = 0
            for kw, mn in parse_bulk(self.bulk.get("1.0", "end")):
                if not self._exists(kw):
                    self.cards.append(new_card(kw, mn))
                    added += 1
            self.bulk.delete("1.0", "end")
            self.after_change()
            messagebox.showinfo("Import", f"Added {added} card(s).")

        def delete_selected(self):
            idx = sorted((int(i) for i in self.tree.selection()), reverse=True)
            if idx and messagebox.askyesno("Delete", f"Delete {len(idx)} card(s)?"):
                for i in idx:
                    del self.cards[i]
                self.after_change()

        def reset_selected(self):
            for i in self.tree.selection():
                c = self.cards[int(i)]
                self.cards[int(i)] = new_card(c["keyword"], c["meaning"])
            self.after_change()

        def after_change(self):
            save_cards(self.cards)
            self.refresh_tree()
            self.dirty = True

        def on_tab(self, _event):
            if self.nb.index("current") == 0 and self.dirty:
                self.start_session()
            elif self.nb.index("current") == 1:
                self.refresh_tree()

    App().mainloop()


if __name__ == "__main__":
    run_gui()
