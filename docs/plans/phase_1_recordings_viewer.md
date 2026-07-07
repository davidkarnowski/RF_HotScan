# Phase 1: Database Migration & In-App Recordings Viewer

**Status:** Planned
**Focus:** UI & Data Persistence

This phase adds the foundational database columns required for healed transcripts and introduces a dedicated "Recordings" tab in the `rf_hotscan.py` GUI. This phase does not implement the actual LLM calls; it just ensures the data layer can store them and the UI can display and play historical recordings. This phase can be implemented entirely independently of Phase 2.

## 1. Database Updates (`recorder.py`)

The SQLite schema needs additive migration columns to store the healed transcription and the healing engine's name.

### File: `recorder.py`
**Diff / Instructions:**
1. Locate `_MIGRATE` tuple. Add the new healing columns to it.
```diff
--- a/recorder.py
+++ b/recorder.py
@@ -61,8 +61,9 @@
 
 # additive columns for DBs created before STT existed (guarded migration)
-_MIGRATE = ("transcript TEXT", "transcript_engine TEXT", "transcript_model TEXT",
-            "transcript_rt REAL", "transcribed_at REAL")
+_MIGRATE = ("transcript TEXT", "transcript_engine TEXT", "transcript_model TEXT",
+            "transcript_rt REAL", "transcribed_at REAL",
+            "healed_transcript TEXT", "healed_by_engine TEXT", "healed_at REAL")
```
2. The `RecordingsDB.set_transcript` method already accepts `**fields`, so no changes are needed there to support `healed_transcript=...`.

## 2. In-App Recordings Viewer UI (`rf_hotscan.py`)

Create a new tab in the main `ttk.Notebook` to query `RecordingsDB` and list historical transmissions. Integrate `player.py` for audio playback.

### File: `rf_hotscan.py`
**Diff / Instructions:**
1. Import `player` module explicitly if not done.
```diff
--- a/rf_hotscan.py
+++ b/rf_hotscan.py
@@ -58,6 +58,7 @@
 # Transmission playback (stdlib + lazy sounddevice). Independent of the SDR.
 try:
     import player
+    PLAYER_AVAILABLE = True
 except Exception:
     player = None
+    PLAYER_AVAILABLE = False
```

2. Create a `RecordingsView` class. This should be placed near the other view classes (e.g., above or below `ScannerView`).
```python
class RecordingsView:
    def __init__(self, parent, db):
        self.parent = parent
        self.db = db
        self.player = player.WavPlayer() if player else None
        
        # UI Layout: Top controls (Refresh, Filter), Middle Treeview, Bottom Detail/Player
        top_frame = ttk.Frame(parent)
        top_frame.pack(side="top", fill="x", padx=5, pady=5)
        
        ttk.Button(top_frame, text="↻ Refresh", command=self.load).pack(side="left")
        ttk.Button(top_frame, text="▶ Play", command=self.play_selected).pack(side="left", padx=5)
        ttk.Button(top_frame, text="⏸ Pause/Stop", command=self.stop_playback).pack(side="left")
        
        self.tree = ttk.Treeview(parent, columns=("time", "tag", "channel", "dur", "transcript", "healed"), show="headings")
        self.tree.heading("time", text="Time")
        self.tree.heading("tag", text="Tag")
        self.tree.heading("channel", text="Channel")
        self.tree.heading("dur", text="Dur")
        self.tree.heading("transcript", text="Raw STT")
        self.tree.heading("healed", text="Healed STT")
        
        self.tree.column("time", width=120)
        self.tree.column("tag", width=60)
        self.tree.column("channel", width=150)
        self.tree.column("dur", width=50)
        self.tree.column("transcript", width=250)
        self.tree.column("healed", width=250)
        self.tree.pack(fill="both", expand=True, padx=5, pady=5)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        
        self.detail_text = tk.Text(parent, height=6, bg="#1e1e1e", fg="#e6e6e6", wrap="word")
        self.detail_text.pack(fill="x", padx=5, pady=5)
        
        self.load()

    def load(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.db: return
        recs = self.db.list(limit=300)
        for r in recs:
            t_str = r.get("iso_start", "")[11:19]
            self.tree.insert("", "end", iid=r["id"], values=(
                t_str, r.get("tag", ""), r.get("name", ""), f"{r.get('duration_s', 0):.1f}s",
                r.get("transcript", ""), r.get("healed_transcript", "")
            ))

    def on_select(self, event):
        sel = self.tree.selection()
        if not sel: return
        rec = self.db.get(sel[0])
        if not rec: return
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("end", "--- RAW STT ---\n")
        self.detail_text.insert("end", rec.get("transcript", "") + "\n\n")
        self.detail_text.insert("end", "--- HEALED STT ---\n")
        self.detail_text.insert("end", rec.get("healed_transcript", ""))

    def play_selected(self):
        sel = self.tree.selection()
        if not sel or not self.player: return
        rec = self.db.get(sel[0])
        if rec and rec.get("wav_path"):
            self.player.play(rec["wav_path"])

    def stop_playback(self):
        if self.player:
            self.player.stop()
```

3. Attach it to the main `Application` notebook.
```diff
--- a/rf_hotscan.py
+++ b/rf_hotscan.py
@@ -1900,6 +1900,10 @@
         self.heatmap_tab = ttk.Frame(self.nb)
         self.nb.add(self.heatmap_tab, text="🔥 Heatmap")
 
+        self.rec_tab = ttk.Frame(self.nb)
+        self.nb.add(self.rec_tab, text="📼 Recordings")
+        self.recordings_view = RecordingsView(self.rec_tab, getattr(self, 'rec_db', None))
+
         self._build_scanner_tab()
         self._build_heatmap_tab()
```
*Note: Ensure `self.rec_db` (or equivalent `RecordingsDB` instance) is passed down properly depending on initialization order in `Application`. If `rec_db` isn't globally kept by Application, instantiate a new `recorder.RecordingsDB()` in `RecordingsView`.*
