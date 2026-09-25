# Third-Party Notices

Artificial Girlfriend is licensed under the GNU Affero General Public License
version 3 (see `LICENSE`). This file lists third-party components that carry
their own terms.

**Scope note.** Almost nothing here is redistributed by this repository. Only
NirCmd and the two font files below are third-party files actually stored in the
repository (the bundled model characters under `model_characters/` and
`MotionPNGPlayer/Asset/` are first-party — see the SCOPE section of `LICENSE`); every
Python package, model weight and runtime listed further down is fetched from its
own publisher by `pip` or by the installer, onto the user's machine, together
with its own license file. This list therefore covers obligations and credits,
not a redistribution manifest.

---

## 1. Redistributed in this repository

### NirCmd — Nir Sofer (NirSoft) — proprietary freeware

`nircmd-x64/` (`nircmd.exe`, `nircmdc.exe`, `NirCmd.chm`)
<https://www.nirsoft.net/utils/nircmd.html>

NirCmd is **not** open source. Its license reads:

> This utility is released as freeware. You are allowed to freely distribute
> this utility via floppy disk, CD-ROM, Internet, or in any other way, as long
> as you don't charge anything for this. If you distribute this utility, you
> must include all files in the distribution package, without any
> modification !

This repository contains the complete, unmodified package and is distributed
free of charge, which satisfies both conditions. NirCmd runs as a separate
process (invoked by `backend/tools/command_executor.py`); it is not linked into
the program.

**If you redistribute Artificial Girlfriend for a fee, delete `nircmd-x64/`
first.** Its license forbids charging for it, and the AGPL neither covers nor
relicenses it. Only the optional PC command-execution feature is affected.

NirSoft utilities are frequently flagged by antivirus software as false
positives; see <https://www.nirsoft.net/false_positive_report.html>.

### Source Sans 3 — Adobe — SIL Open Font License 1.1

`fonts/SourceSans3-Regular.ttf.woff2`, `fonts/SourceSans3-Semibold.ttf.woff2`
<https://github.com/adobe-fonts/source-sans> (release 3.052R)

Redistributed unmodified so that the UI needs no external font CDN. Full
license text and the Reserved Font Name declaration: `fonts/OFL.txt`.

---

## 2. Derived work

### MotionPNGTuber — rotejin — MIT License

`MotionPNGPlayer/` is a modified version of *MotionPNGTuber* by rotejin
(<https://github.com/rotejin/MotionPNGTuber>), adapted for Artificial
Girlfriend. It remains under the MIT License; the original copyright notice is
retained in `MotionPNGPlayer/LICENSE`.

---

## 3. Copyleft dependencies (installed by pip, not redistributed here)

These determine the license of the program as a whole.

| Component | License | Role |
|---|---|---|
| `style-bert-vits2` | **AGPL-3.0** | Japanese local TTS. This is why Artificial Girlfriend is AGPL-3.0. |
| `phonemizer-fork` | GPL-3.0-or-later | Grapheme-to-phoneme fallback for English TTS |
| eSpeak NG (shipped inside `espeakng-loader`) | GPL-3.0-or-later | Phonemization backend. Note: the `espeakng-loader` package declares no license metadata of its own; eSpeak NG upstream is GPL-3.0-or-later (<https://github.com/espeak-ng/espeak-ng>). |
| `cmudict` | GPL-3.0-or-later | English pronunciation dictionary |
| `pynput` | LGPL-3.0 | Global hotkeys |
| `pystray` | LGPL-3.0 | Tray icon |
| `num2words` | LGPL-2.1-or-later | Number-to-words in TTS text normalization |
| `certifi` | MPL-2.0 | CA certificate bundle |
| `tld` | MPL-1.1 / GPL-2.0 / LGPL-2.1 (tri-licensed) | URL parsing for Deep Search |

`soundfile` and `av` ship prebuilt native libraries (libsndfile and FFmpeg)
inside their wheels under LGPL terms; consult those packages' own license files
in your virtual environment.

All remaining Python dependencies are under permissive licenses (MIT, BSD,
Apache-2.0, PSF, ISC, Unlicense). Each package installs its own license text
into `venv/Lib/site-packages/<name>.dist-info/`.

---

## 4. Downloaded by the installer or at first use

### Japanese BERT — CC BY-SA 4.0 — attribution required

`ku-nlp/deberta-v2-large-japanese-char-wwm`
<https://huggingface.co/ku-nlp/deberta-v2-large-japanese-char-wwm>

Created by the **Language Media Processing Lab, Kyoto University**, released
under the Creative Commons Attribution-ShareAlike 4.0 International license.
Required by Style-Bert-VITS2 for Japanese synthesis; fetched into `bert/ja/`.

### Other fetched components

| Component | License |
|---|---|
| Kokoro-82M (`hexgrad/Kokoro-82M`) — English TTS model and voice packs | Apache-2.0 |
| Whisper models (`Systran/faster-whisper-*`) — speech recognition | MIT |
| `en_core_web_sm` — spaCy English model | MIT |
| Open JTalk dictionary (via `pyopenjtalk-dict`, © Ryuichi Yamamoto) | MIT wrapper; the dictionary carries its own upstream terms |
| Electron 28.3.3 — runtime for MotionPNGPlayer and AG Client Addon | MIT |

---

## 5. External services

Artificial Girlfriend can call third-party APIs (Ollama, OpenAI, Anthropic,
xAI, Google, ElevenLabs, ELYTH, DuckDuckGo, Tailscale). These are services
governed by their providers' terms of use, not software licenses, and no
provider code is included here.

ELYTH (https://elythworld.com) is a social network for AI characters, created
by Nano. AG characters can join it with a per-character API key.
