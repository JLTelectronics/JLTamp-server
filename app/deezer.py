"""Een Deezer-lijst lezen via de open Deezer-API.

Anders dan Spotify (zie spotify.py) heeft Deezer een publieke API die voor
*publieke* lijsten en albums geen login vereist: api.deezer.com/playlist/{id},
/album/{id}, /track/{id}. Vorm van de uitkomst is gelijk aan `spotify.lees`,
zodat de importer beide gelijk kan behandelen:

    {titel, items, bron, afgekapt, soort, mix}

`items` = [{title, artist, position}, …]. `soort` is "album" | "playlist" |
"track". `mix` is waar als de lijst van Deezer zelf komt (redactioneel) in
plaats van van een persoon — te zien aan de `creator`.

Alleen GET's, geen sleutel, geen schrijfacties. Veilig vanaf elke context.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

API = "https://api.deezer.com"
# www.deezer.com/<taal>/playlist/123, deezer.com/album/123, link.deezer.com/… (kort)
_LINK = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(playlist|album|track)/(\d+)", re.I)
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class DeezerFout(Exception):
    """Iets ging mis bij het lezen van een Deezer-lijst (voor de gebruiker)."""


def _spaties(tekst: str | None) -> str:
    return re.sub(r"\s+", " ", (tekst or "")).strip()


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA,
                                               "Accept": "application/json"})
    try:
        rauw = urllib.request.urlopen(req, timeout=25).read().decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DeezerFout("Die Deezer-lijst bestaat niet (of staat op privé)")
        raise DeezerFout("Deezer is even niet bereikbaar")
    except Exception:
        raise DeezerFout("Deezer is even niet bereikbaar")
    try:
        data = json.loads(rauw)
    except Exception:
        raise DeezerFout("Deezer gaf geen leesbaar antwoord")
    # Deezer meldt fouten in het lichaam, niet via de HTTP-code.
    if isinstance(data, dict) and data.get("error"):
        fout = data["error"]
        boodschap = (fout.get("message") if isinstance(fout, dict) else "") or ""
        if "private" in boodschap.lower() or "permission" in boodschap.lower():
            raise DeezerFout("Die Deezer-lijst staat op privé")
        raise DeezerFout("Deezer geeft deze lijst niet vrij")
    return data


def _is_editorial(creator: dict) -> bool:
    """Deezers eigen redactionele lijsten staan op naam van een Deezer-account:
    "Deezer", "Deezer Editoriale", maar ook genoemde redacteuren als
    "Rod - Deezer Country Editor". Vandaar: "deezer" ergens in de makernaam."""
    naam = str((creator or {}).get("name") or "").strip().lower()
    return "deezer" in naam


def _spoor(rijen: list[dict], vanaf: int) -> list[dict]:
    uit = []
    for n, t in enumerate(rijen):
        titel = _spaties(t.get("title") or t.get("title_short"))
        if not titel:
            continue
        uit.append({
            "title": titel,
            "artist": _spaties((t.get("artist") or {}).get("name")),
            "position": vanaf + n,
        })
    return uit


def _playlist(pid: str) -> dict:
    kop = _get(f"{API}/playlist/{pid}")
    naam = _spaties(kop.get("title")) or "Deezer-playlist"
    mix = _is_editorial(kop.get("creator") or {})
    totaal = int(kop.get("nb_tracks") or 0)
    items: list[dict] = []
    index = 0
    while True:
        blok = _get(f"{API}/playlist/{pid}/tracks?index={index}&limit=100")
        rijen = blok.get("data") or []
        if not rijen:
            break
        items.extend(_spoor(rijen, index))
        index += len(rijen)
        if totaal and index >= totaal:
            break
        if index >= 5000:        # een ongeluk, geen import
            break
        if not blok.get("next"):
            break
    return {"titel": naam, "items": items, "bron": "api", "afgekapt": False,
            "soort": "playlist", "mix": mix}


def _album(aid: str) -> dict:
    kop = _get(f"{API}/album/{aid}")
    naam = _spaties(kop.get("title")) or "Deezer-album"
    albumartiest = _spaties((kop.get("artist") or {}).get("name"))
    rijen = (kop.get("tracks") or {}).get("data") or []
    items = _spoor(rijen, 0)
    for it in items:                        # op een album is de albumartiest de maat
        it["artist"] = it["artist"] or albumartiest
    return {"titel": naam, "items": items, "bron": "api", "afgekapt": False,
            "soort": "album", "mix": False}


def _track(tid: str) -> dict:
    kop = _get(f"{API}/track/{tid}")
    naam = _spaties(kop.get("title")) or "Deezer-nummer"
    artiest = _spaties((kop.get("artist") or {}).get("name"))
    return {"titel": naam, "items": [{"title": naam, "artist": artiest, "position": 0}],
            "bron": "api", "afgekapt": False, "soort": "track", "mix": False}


def is_deezer(url: str) -> bool:
    return bool(_LINK.search(url or ""))


def ontleed(url: str) -> tuple[str, str] | None:
    """(soort, id) uit een Deezer-link, of None. soort = playlist|album|track."""
    m = _LINK.search(url or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def lees(url: str) -> dict:
    """Een Deezer-link → {titel, items, bron, afgekapt, soort, mix}."""
    m = _LINK.search(url or "")
    if not m:
        raise DeezerFout("Dat lijkt geen Deezer-link")
    soort, pid = m.group(1).lower(), m.group(2)
    if soort == "album":
        return _album(pid)
    if soort == "track":
        return _track(pid)
    return _playlist(pid)
