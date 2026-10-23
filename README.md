# ConFas ⚡ — ultra-fast local document converter for Windows

**One EXE. Right-click any document → converted. Learns to get faster.**

ConFas is a 100 % local, offline document conversion tool:

* **Right-click menu** in Explorer — *"Convert with ConFas"* on every document/image type.
* **Always running** — after the first double-click it registers itself in **Task Manager → Startup apps** and lives in the system tray forever.
* **Latest-generation output only** — `.doc` never produces legacy Word 97; you get `.docx` (OOXML 2007+), `.xlsx`, `.pptx`, modern image codecs (WebP/AVIF/JPEG XL), UTF-8 text…
* **Adaptive ML routing** *(the "trick")* — a lightweight contextual bandit (LinUCB) learns from every conversion which backend is fastest/most reliable **on your machine for files like yours**, and re-orders the fallback chain accordingly. Speed improves with use; correctness can never degrade (fallbacks are always kept). If you don't want it: `confas config ml=off` or `CONFAS_DISABLE_ML=1`.

---

## How it works (speed & perfection)

| Conversion | Fastest backend (auto-picked) | Fallbacks |
|---|---|---|
| DOCX/XLSX/PPTX/DOC/XLS/PPT/ODT/RTF → PDF | **Microsoft Office COM** (`SaveAs2` FileFormat 17 / ExportAsFixedFormat) | LibreOffice headless |
| PDF → DOCX | **pdf2docx** (layout/table/image preserving) | LibreOffice filter `MS Word 2007 XML` |
| Legacy .doc/.xls/.ppt → modern | Office COM FileFormat **12 / 51 / 24** (= OOXML 2007+) | LibreOffice `MS * 2007 XML` filters |
| Images ↔ images | **Pillow** (C-accelerated, 4:4:4 chroma, quality presets) | LibreOffice |
| TXT/MD/CSV/HTML → DOCX | **python-docx** (real headings/lists/bold/code styles) | LibreOffice |
| DOCX → TXT/MD | python-docx direct read | LibreOffice |
| EPUB → TXT/DOCX/HTML | stdlib zip + spine-order extraction | — |
| PDF → TXT | pypdf (fast, no rendering) | LibreOffice |

Notes on *perfection*:
* LibreOffice filters used are explicitly the **2007 XML** generation ones — never `MS Word 97`.
* Text outputs are written as UTF-8 (with BOM where Windows consumers expect it).
* Every pair has an ordered fallback chain; if all fail you get a clear error popup instead of a broken file.
* Persistent worker pool + daemon means no cold-start per conversion.

## The ML part (`src/confas/learning.py`)

For each `(source→target, backend)` arm the engine records latency + success.
A LinUCB linear bandit over features (file size, path depth, hour-of-day, src/dst format one-hots) picks the next attempt order. State persists in `%USERPROFILE%\.confas\bandit.json`; every run is logged to `history.jsonl` and visible via:

```bat
ConFas.exe stats
```

Cold start falls back to static weights; exploration bonus guarantees the model keeps testing alternatives, but a failing arm is demoted instantly and the engine transparently retries the next backend — so results stay correct even mid-learning.

## Shared learnings (optional cloud sync) — "send the improvements somewhere"

ConFas can **pool everyone's ML learnings** so that a person who downloads the
tool later starts with an already-trained model instead of a cold one. This is
**opt-in and privacy-safe**: only aggregate numbers are exchanged (learned
weight matrices + median latencies per format pair). *Never* file names, paths
or contents. The conversion itself always stays 100% local.

### Recommended setup: a community GitHub Gist (zero backend needed)

1. **Owner (you), once:** create a secret gist containing one small JSON file
   (`merged.json` = `{"version":1,"clock":0,"bandit":{},"median_ms":{}}`),
   note its ID from the URL, then in repo settings add two GitHub Secrets:
   `CONFAS_GIST_ID` and `CONFAS_CLOUD_TOKEN` (fine-grained PAT with `gist`
   scope). The included workflow `.github/workflows/cloud-merge-cron.yml`
   runs every 6 h and merges all `contrib-*.json` files into `merged.json`.
2. Ship the EXE with defaults baked in, or let each user configure it:

```bat
ConFas.exe config cloud_gist=<the-gist-id>
setx CONFAS_CLOUD_TOKEN <your-pat-with-gist-scope>
ConFas.exe config cloud_sync=on
```

That's it. Every conversion now (debounced to ≤1 upload/minute) pushes your
aggregate learning as `contrib-<clientid>.json`, and periodically pulls the
pooled `merged.json` back into your local bandit — so you also benefit from
everyone else's machines. Check state any time with `ConFas.exe stats`
("Cloud learnings sync: ON/OFF, last pull/push").

Alternative: run any tiny REST endpoint speaking `GET /state` + `PUT /state`
(Cloudflare Worker/KV, S3, nginx+file…) and use `config cloud_url=https://...`
instead of the gist.

Merge rules (`src/confas/cloud.py::merge_states`): sample counts sum, reward
vectors sum, covariance matrices average (keeps the ridge prior well-scaled),
latency medians take the minimum, and a Lamport clock guards ordering — so
concurrent users can never corrupt the pool.

### Security model (defence in depth)

* **Where it lives:** only two endpoints are ever contacted —
  `https://api.github.com` (gist mode) or *your own* `https://…` URL. Plaintext
  `http://`, `file://`, `ftp://` and malformed URLs are hard-refused by an
  allow-list (`_check_url`); TLS certificate verification is always on and
  cannot be disabled from config.
* **Least-privilege credentials:** the token is a fine-grained GitHub PAT with
  the `gist` scope *only* — it cannot touch repos, code, packages, issues or
  any account data. It is stored locally (`~/.confas/cloud_token`, created via
  `os.open(..., 0o600)`; on Windows use `setx` so it stays in your user
  profile) and is sent exclusively to `api.github.com`. Use **your own** token;
  nothing of yours is ever accessible through someone else's.
* **Nothing identifying leaves the machine:** payload = weight matrices +
  per-pair median latencies + counters. No file names, paths, document content,
  usernames or hardware IDs (client id is a random UUID). Verified by test.
* **Hostile-server protection (model poisoning):** downloaded state is treated
  as untrusted input — 2 MiB response cap, ≤512 arms, ≤1M samples/arm, every
  matrix validated (shape/finiteness/symmetry/positive-semi-definite), rewards
  clamped to [-1, 1], latencies bounded to sane ranges, sha256 of accepted
  state recorded for tamper visibility. Garbage arms are dropped one by one.
* **ML can never break correctness:** even a perfectly poisoned pooled model
  can only reorder *which backend tries first*; the curated fallback chains
  still run, so conversions stay correct offline, online, or under attack.
* **Fail-open locally:** any sync error is logged and ignored; the local bandit
  keeps learning normally. Turning it off is one command:
  `ConFas.exe config cloud_sync=off`.

---

## Download the EXE (no clone, no build)

Once a release is published, the direct download link is:

```
https://github.com/<owner>/<repo>/releases/latest/download/ConFas.exe
```

* `<owner>/<repo>` = this repository once you push it to GitHub.
* `/releases/latest/download/...` **always resolves to the newest published
  version** — one permanent link that never goes stale.

Publishing happens automatically via `.github/workflows/release.yml`:

1. Push the repo to GitHub.
2. Tag and push a version → CI runs the full test suite, builds `ConFas.exe`
   on a Windows runner and attaches it (plus `build-info.txt` with commit +
   timestamp) to a GitHub Release:
   ```bat
   git tag v1.0.0
   git push origin v1.0.0
   ```
3. Share the permanent link above. Anyone who downloads and double-clicks the
   EXE gets the self-install (right-click menu + Task Manager Startup entry).

**"Each EXE downloaded with the latest improvements":** ML learnings are
deliberately *not* baked into the binary (that would force a re-download every
time anyone converts a file). They live in the shared cloud pool (section
"Shared learnings"): every user pulls the pooled state on launch, so a
brand-new install of any release starts warm — improvements arrive
continuously, far faster than any rebuild could ship them. The EXE itself only
needs re-releasing when code changes, via the tag step above.

## Build the EXE yourself

> A Windows `.exe` must be produced on Windows (PyInstaller cannot cross-compile). Two options, both ~3 minutes:

### Option A — one command on your PC
```powershell
git clone <this-repo> && cd <repo>
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```
The script creates a venv, installs deps, runs `build.py`, copies `dist\ConFas.exe` to `%LOCALAPPDATA%\ConFas`, performs the install (startup + right-click menu) and launches the tray daemon.

### Option B — free GitHub CI
Push this repo to GitHub; the included workflow **`.github/workflows/build.yml`** runs tests on Linux and builds `ConFas.exe` on `windows-latest`, publishing it as the downloadable artifact **`ConFas-windows-exe`**.

Manual build if you prefer:
```bat
py -3.12 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt pyinstaller pywin32 pystray
python build.py        REM -> dist\ConFas.exe
```

## Use

1. **Double-click `ConFas.exe` once.** It self-installs:
   * adds **"Convert with ConFas"** to the right-click menu of pdf/doc/docx/xls/xlsx/ppt/pptx/odt/ods/odp/rtf/txt/md/csv/html/epub/png/jpg/… (HKCU shell verbs — no admin needed);
   * writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ConFas` → it now appears in **Task Manager → Startup apps** and starts with Windows automatically;
   * stays running in the tray (watch folder + fast warm engine).
   * Sign out/in once (or restart Explorer) so the new menu entries appear.
2. **Right-click a document → Convert with ConFas** → output appears next to it in the newest format version (PDF→DOCX gives real OOXML `.docx`).
3. Bonus: drop files into **`%USERPROFILE%\ConFasDrop`** → auto-converted.

CLI (same binary):
```bat
ConFas.exe convert report.pdf                REM -> report.docx (latest gen)
ConFas.exe convert a.doc b.xls -t pdf        REM batch, parallel
ConFas.exe status                            REM detected backends / registration
ConFas.exe stats                             REM learned per-backend performance
ConFas.exe config auto_target=docx watch_folder=D:\drop ml=on
ConFas.exe uninstall                         REM removes startup entry + menu
```

## Repository layout
```
src/confas/engine.py     conversion engine + backend routing (newest-gen filters)
src/confas/learning.py   LinUCB adaptive ML backend selector
src/confas/monitor.py    tray daemon, watch folder, startup + context-menu installer
src/confas/cli.py        command line / right-click entry point
entry_point.py           frozen-EXE entry (double-click = install + run forever)
build.py / confas.spec   PyInstaller build
scripts/install.ps1      one-shot Windows setup
scripts/make_icon.py     generates assets/confas.ico
tests/                   13 cross-platform tests (engine, ML convergence, watcher)
.github/workflows/build.yml  CI: tests + windows EXE artifact
```

## Requirements
* Windows 10/11, Python 3.10+ only to *build* (not to run the EXE).
* For best fidelity/speed install either **Microsoft Office** (COM backend) or **LibreOffice** (headless backend). Pillow/python-docx/pdf2docx/pypdf/numpy are bundled inside the EXE.
* Pure image/text/markup conversions work with nothing else installed.

