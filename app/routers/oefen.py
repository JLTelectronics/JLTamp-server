"""Oefenen: een nummer trager, zonder dat de stem vervormt.

De app kan een nummer zelf trager afspelen, maar op Android doet de speler dat
met een eenvoudige rekenmethode (Sonic), en een stem gaat daarvan wiebelen.
Hier doet Rubber Band het, met zijn nieuwste methode (R3, `--fine`): dezelfde
toonhoogte, en de klank van de stem blijft heel. De filter in ffmpeg kent
alleen de oudere R2, en daarin bleef een stem nog wat vervormd (26-10). R3 kost
zo'n 16 s voor een nummer van vijf minuten; ontbreekt het programma, dan R2.
Daarna een zachte begrenzer: R3 komt tot vlak onder 0 dB, en AAC gaat er dan
net overheen.

De bron is wat de app nu speelt: het origineel (`o`), de zangschuif van de
karaoke (`k<pct>`), of een menging van de bandmixer (`b<standen>`, bv. zonder
gitaar). Bewaard in `DATA_DIR/oefen/<id>/`, nooit naar `/music`; wat twee
weken niet gebruikt is, gaat weg.

    GET /oefen/{id}?tempo=75&bron=o          → status (en het maken begint)
    GET /oefen/{id}/audio.m4a?tempo=75&bron=o → het bestand, als het klaar is
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .. import config
from ..deps import require_user
from ..models import Track, User
from . import band, karaoke

router = APIRouter(tags=["oefen"])
log = logging.getLogger("jltamp.oefen")

OEFEN_DIR = config.DATA_DIR / "oefen"
TEMPI = (50, 60, 75, 90)
BEWAAR_S = 14 * 24 * 3600
# Terugval zonder het programma (R2 in ffmpeg): een tik blijft een tik (mixed),
# en de kanalen samen houden het stereobeeld heel.
FILTER = "rubberband=tempo={t}:pitchq=quality:transients=mixed:channels=together"
# Nooit boven -1 dB: zonder dit kraakte het na het coderen op de hardste plekken.
BEGRENS = "alimiter=limit=0.89:level=false"
# In de bestandsnaam: een betere methode = opnieuw maken, niet de oude tonen.
METHODE = "r3" if shutil.which("rubberband") else "r2"

_slot = threading.Lock()
_bezig: set[str] = set()
_mislukt: dict[str, tuple[float, str]] = {}
MISLUKT_S = 600


def _bron_pad(t: Track, bron: str, user: User) -> Path:
    """Het bestand dat de app nu speelt, als invoer."""
    if bron == "o":
        return Path(t.path)
    if not karaoke.karaoke_aan(user):
        raise HTTPException(403, "Karaoke staat uit")
    m = re.fullmatch(r"k(\d{1,3})", bron)
    if m:
        pct = int(m.group(1))
        if not karaoke._is_klaar(t):
            raise HTTPException(404, "Nog niet gescheiden")
        if pct == 0:
            return karaoke._map(t.id) / "begeleiding.m4a"
        if pct not in karaoke.MIX_STANDEN and pct not in karaoke.MIX_BOVEN:
            raise HTTPException(400, "Onbekende stand")
        return karaoke._maak_mix(t.id, pct)
    m = re.fullmatch(r"b([\d,]+)", bron)
    if m:
        if band._info(t) is None:
            raise HTTPException(404, "Nog niet gescheiden")
        return band._meng(t, band._standen(m.group(1)))
    raise HTTPException(400, "Onbekende bron")


def _doel(track_id: int, bron: str, tempo: int) -> Path:
    veilig = re.sub(r"[^0-9a-z]", "-", bron)
    return OEFEN_DIR / str(track_id) / f"{veilig}-{tempo}-{METHODE}.m4a"


def _is_klaar(doel: Path, src: Path) -> bool:
    try:
        # Een nieuwere bron (een schonere scheiding, een nieuw bestand) = opnieuw.
        return doel.exists() and doel.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def _draai(*args: str) -> None:
    r = subprocess.run(list(args), capture_output=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]}: " + (r.stderr.decode(errors="replace").strip()[-200:] or "?"))


def _maak(src: Path, doel: Path, tempo: int) -> None:
    doel.parent.mkdir(parents=True, exist_ok=True)
    tijdelijk = doel.with_suffix(".tmp.m4a")
    los, uit = doel.with_suffix(".in.wav"), doel.with_suffix(".uit.wav")
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    codeer = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tijdelijk)]
    begin = time.time()
    try:
        if METHODE == "r3":
            # Uitpakken, rekken met R3, inpakken. De wav's staan even in /data (~70 MB).
            _draai(*ff, "-i", str(src), "-vn", "-map", "0:a:0", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(los))
            _draai("rubberband", "--fine", "-T", str(tempo / 100), "-q", str(los), str(uit))
            _draai(*ff, "-i", str(uit), "-af", BEGRENS, *codeer)
        else:
            _draai(*ff, "-i", str(src), "-vn", "-map", "0:a:0", "-af", f"{FILTER.format(t=tempo / 100)},{BEGRENS}", *codeer)
    except Exception:
        tijdelijk.unlink(missing_ok=True)
        raise
    finally:
        los.unlink(missing_ok=True)
        uit.unlink(missing_ok=True)
    # Pas als het hele bestand er staat: een half bestand mag nooit "klaar" lijken.
    os.replace(tijdelijk, doel)
    log.info("[oefen] %s in %.1fs", doel.name, time.time() - begin)


def _op_achtergrond(sleutel: str, src: Path, doel: Path, tempo: int) -> None:
    try:
        _maak(src, doel, tempo)
        _mislukt.pop(sleutel, None)
    except Exception as e:
        log.warning("[oefen] %s mislukt: %s", sleutel, e)
        _mislukt[sleutel] = (time.time(), str(e)[:200])
    finally:
        with _slot:
            _bezig.discard(sleutel)


def _opruimen() -> None:
    grens = time.time() - BEWAAR_S
    try:
        for f in OEFEN_DIR.glob("*/*.m4a"):
            if ".tmp" in f.name:
                continue   # wordt nu gemaakt
            # Oud, of van een vorige methode: weg.
            if (f.stat().st_atime < grens and f.stat().st_mtime < grens) or not f.stem.endswith(METHODE):
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _controleer(tempo: int) -> None:
    if tempo not in TEMPI:
        raise HTTPException(400, "Onbekend tempo")


@router.get("/oefen/{track_id}")
def oefen_status(track_id: str, tempo: int = Query(...), bron: str = Query("o"),
                 user: User = Depends(require_user)):
    _controleer(tempo)
    t = karaoke._track(track_id, user)
    src = _bron_pad(t, bron, user)
    doel = _doel(t.id, bron, tempo)
    if _is_klaar(doel, src):
        try:
            os.utime(doel)   # gebruikt: blijft bewaard
        except OSError:
            pass
        return {"status": "klaar"}
    sleutel = f"{t.id}:{bron}:{tempo}"
    fout = _mislukt.get(sleutel)
    if fout and time.time() - fout[0] < MISLUKT_S:
        return {"status": "mislukt", "fout": fout[1]}
    with _slot:
        if sleutel not in _bezig:
            _bezig.add(sleutel)
            _opruimen()
            threading.Thread(target=_op_achtergrond, args=(sleutel, src, doel, tempo), daemon=True).start()
    return {"status": "bezig"}


@router.get("/oefen/{track_id}/audio.m4a")
def oefen_audio(track_id: str, tempo: int = Query(...), bron: str = Query("o"),
                user: User = Depends(require_user)):
    _controleer(tempo)
    t = karaoke._track(track_id, user)
    src = _bron_pad(t, bron, user)
    doel = _doel(t.id, bron, tempo)
    if not _is_klaar(doel, src):
        raise HTTPException(404, "Nog niet klaar")
    return FileResponse(doel, media_type="audio/mp4")
