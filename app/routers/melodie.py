"""Zingen met punten: de melodie die je moet zingen, noot voor noot.

De app legt de toonhoogte van de microfoon (de stemmer, op het toestel zelf)
naast deze noten en telt punten. Hier alleen de noten: uit de losse zang van de
karaoke (`DATA_DIR/karaoke/<id>/zang.m4a`), met de eigen melodiemeter in
app/melodie.py. Dat is numpy op de CPU en kost meestal minder dan een
seconde per nummer, dus geen dienst. Wel op de achtergrond: net als iemand
karaoke aanzet maakt de server ook de mixen voor de zangschuif, en dan duurde
het op 25-10 eens 41 s. Een vraag wacht daar niet op; hij krijgt `bezig` en
vraagt zo opnieuw.

Zonder karaoke geen melodie: de zang moet eerst los zijn. Wie meezingt heeft
de karaokestand aan, dus die vraagt de scheiding al aan. Is die er nog niet,
dan zegt de status `wacht` met de reden `karaoke`, en vraagt de app later
opnieuw.

Bewaard in `DATA_DIR/melodie/<track_id>.json`, met het bronkenmerk, `VERSIE`
en welke scheiding de bron was (`zang`): vervangt RoFormer de snelle scheiding
door een schonere, dan wordt de melodie opnieuw gemeten. Alles naar `/data`,
nooit naar `/music`.

Geen eigen schakelaar: het hoort bij karaoke, dus `karaoke_aan`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select

from .. import config
from ..db import SessionLocal
from ..deps import require_user
from ..models import Track, User, ZangScore
from . import karaoke

router = APIRouter(tags=["melodie"])
log = logging.getLogger("jltamp.melodie")

MELODIE_DIR = config.DATA_DIR / "melodie"

# Verhoog dit als de meter verandert (drempels, gladstrijken): dan wordt wat er
# ligt bij de volgende vraag opnieuw gemeten.
VERSIE = 1

# Eén meting tegelijk, op een eigen draad: twee telefoons die tegelijk een
# nieuw nummer openen hoeven het niet allebei uit te rekenen.
_slot = threading.Lock()
_bezig: set[int] = set()
# Mislukt een nummer, dan niet bij elke vraag opnieuw (de app vraagt elke paar seconden).
_mislukt: dict[int, tuple[float, str]] = {}
MISLUKT_S = 600


def _bestand(track_id: int) -> Path:
    return MELODIE_DIR / f"{track_id}.json"


def _zang_versie(track_id: int):
    return (karaoke._lees_info(track_id) or {}).get("gemaakt")


def _lees(t: Track) -> dict | None:
    try:
        d = json.loads(_bestand(t.id).read_text())
    except Exception:
        return None
    if d.get("versie") != VERSIE or d.get("bron") != karaoke._bron_kenmerk(t):
        return None
    # Is de scheiding intussen vervangen (RoFormer), dan opnieuw meten. Is hij
    # opgeruimd, dan blijft wat er ligt gewoon goed.
    nu = _zang_versie(t.id)
    if nu is not None and nu != d.get("zang"):
        return None
    return d


def _meet(t: Track) -> dict:
    zang = karaoke._map(t.id) / "zang.m4a"
    begin = time.time()
    # In een eigen proces (zie melodie._los): hier deelde het de GIL met de radio.
    r = subprocess.run([sys.executable, "-m", "app.melodie", str(zang)],
                       capture_output=True, text=True, timeout=180, cwd=str(Path(__file__).resolve().parents[2]))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "meten mislukt").strip()[-200:])
    uit = json.loads(r.stdout)
    d = {
        "versie": VERSIE, "bron": karaoke._bron_kenmerk(t), "zang": _zang_versie(t.id),
        "gemaakt": int(time.time()), "seconden": round(time.time() - begin, 2), **uit,
    }
    MELODIE_DIR.mkdir(parents=True, exist_ok=True)
    doel = _bestand(t.id)
    tijdelijk = doel.with_suffix(".tmp")
    tijdelijk.write_text(json.dumps(d, separators=(",", ":")))
    # Pas als het hele bestand er staat: een half bestand mag nooit "klaar" lijken.
    os.replace(tijdelijk, doel)
    log.info("[melodie] nummer %s: %d noten in %.1fs", t.id, len(uit["noten"]), time.time() - begin)
    return d


def _op_achtergrond(t: Track) -> None:
    try:
        if _lees(t) is None:
            _meet(t)
        _mislukt.pop(t.id, None)
    except Exception as e:
        log.warning("[melodie] nummer %s mislukt: %s", t.id, e)
        _mislukt[t.id] = (time.time(), str(e)[:200])
    finally:
        with _slot:
            _bezig.discard(t.id)


def _status(t: Track, aan: bool) -> dict:
    uit = {"aan": aan, "status": "geen", "reden": None, "fout": None,
           "noten": [], "laag": None, "hoog": None}
    if not aan:
        uit["reden"] = "uit"
        return uit
    d = _lees(t)
    if d is None:
        if not karaoke._is_klaar(t):
            uit.update(status="wacht", reden="karaoke")
            return uit
        fout = _mislukt.get(t.id)
        if fout and time.time() - fout[0] < MISLUKT_S:
            uit.update(status="mislukt", fout=fout[1])
            return uit
        with _slot:
            if t.id not in _bezig:
                _bezig.add(t.id)
                threading.Thread(target=_op_achtergrond, args=(t,), name="melodie", daemon=True).start()
        uit["status"] = "bezig"
        return uit
    uit.update(status="klaar", noten=d.get("noten") or [], laag=d.get("laag"), hoog=d.get("hoog"))
    return uit


@router.get("/melodie/{track_id}")
def melodie_status(track_id: str, user: User = Depends(require_user)):
    """De noten om mee te zingen, of waarom ze er (nog) niet zijn."""
    t = karaoke._track(track_id, user)
    return _status(t, karaoke.karaoke_aan(user))


# ── Scores: je record en het scorebord ──────────────────────────────────────
#
# De app stuurt na een gezongen nummer wat hij telde. Hier: bewaren, je eigen
# record, en de top 5 van deze server voor dit nummer — één regel per persoon
# (zijn beste). Wie zijn luistergedrag op "niets" heeft staan (share_activity),
# staat er zonder naam in: dat hij zingt is ook luistergedrag.

MAX_PUNTEN = 10000
TOP = 5


def _naam(u: User) -> str | None:
    if (u.share_activity or "presence") == "none":
        return None
    return u.display_name or (u.email or "").split("@")[0] or None


def _bord(db, track_id: int, user: User) -> dict:
    beste = (select(ZangScore.user_id, func.max(ZangScore.punten).label("p"))
             .where(ZangScore.track_id == track_id).group_by(ZangScore.user_id)
             .order_by(func.max(ZangScore.punten).desc()).limit(TOP))
    rijen = db.execute(beste).all()
    mensen = {u.id: u for u in db.execute(select(User).where(User.id.in_([r[0] for r in rijen]))).scalars()} if rijen else {}
    top = [{"naam": _naam(mensen[r[0]]) if r[0] in mensen else None, "punten": int(r[1]),
            "jij": r[0] == user.id} for r in rijen]
    eigen = db.execute(select(func.max(ZangScore.punten)).where(
        ZangScore.track_id == track_id, ZangScore.user_id == user.id)).scalar()
    return {"record": int(eigen) if eigen is not None else None, "top": top}


@router.get("/melodie/{track_id}/scores")
def scores(track_id: str, user: User = Depends(require_user)):
    """Jouw record en de top van deze server voor dit nummer."""
    t = karaoke._track(track_id, user)
    db = SessionLocal()
    try:
        return _bord(db, t.id, user)
    finally:
        db.close()


@router.post("/melodie/{track_id}/scores")
def score_insturen(track_id: str, punten: int = Body(..., embed=True), perfect: int = Body(0, embed=True),
                   noten: int = Body(0, embed=True), reeks: int = Body(0, embed=True),
                   user: User = Depends(require_user)):
    """Een gezongen nummer bewaren. Geeft of het een record is, en het bord."""
    t = karaoke._track(track_id, user)
    if not karaoke.karaoke_aan(user):
        raise HTTPException(403, "Karaoke staat uit")
    punten = max(0, min(MAX_PUNTEN, int(punten)))
    db = SessionLocal()
    try:
        oud = _bord(db, t.id, user)["record"]
        db.add(ZangScore(user_id=user.id, track_id=t.id, punten=punten, perfect=max(0, int(perfect)),
                         noten=max(0, int(noten)), reeks=max(0, int(reeks)), gemaakt=int(time.time())))
        db.commit()
        uit = _bord(db, t.id, user)
        uit["nieuwRecord"] = oud is None or punten > oud
        uit["vorigRecord"] = oud
        return uit
    finally:
        db.close()
