"""Alles van één gebruiker wissen — voor zelf verwijderen én voor de beheerder.

Voorheen stonden er twee bijna gelijke lijstjes in auth.delete_me en
admin_users.delete_user, en die vergaten allebei een deel: storingsmeldingen,
zoekopdrachten zonder resultaat, lidmaatschap van andermans gedeelde lijsten,
openstaande importnummers en de profielfoto bleven achter. SQLite draait hier
zonder foreign_keys=ON, dus niets ruimt zichzelf op. De verwijderpagina (Play-
eis) belooft "alles weg", dus dat moet op één plek kloppen.

Wat bewust blijft, maar zonder naam eraan:
- nummers die iemand aan een gedeelde lijst van een ander toevoegde: die horen
  nu bij die lijst; `added_by` wordt leeggemaakt.
- het beheerlogboek: wat er in Beheer veranderde blijft staan, op naam van
  niemand (user_id 0).
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete, select, update

from .models import (
    BeheerLog, DiagReport, Meta, LikedTrack, MissedSearch,
    OpenPlaylistItem, PlayEvent, Playlist, PlaylistItem, PlaylistMember,
    Session as AuthSession, SubsonicKey, SubsonicStar, User, UserLibraryAccess, UserTrackState,
    ZangScore,
)

log = logging.getLogger("account")


def wis_gebruiker(db, user: User) -> None:
    """Wist de gebruiker met alles wat aan hem hangt. Commit niet zelf."""
    uid = user.id
    foto = getattr(user, "thumb_path", None)

    eigen = [p.id for p in db.execute(select(Playlist).where(Playlist.user_id == uid)).scalars()]
    if eigen:
        db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id.in_(eigen)))
        db.execute(delete(PlaylistMember).where(PlaylistMember.playlist_id.in_(eigen)))
        db.execute(delete(OpenPlaylistItem).where(OpenPlaylistItem.playlist_id.in_(eigen)))
    db.execute(delete(Playlist).where(Playlist.user_id == uid))

    for model in (AuthSession, UserLibraryAccess, LikedTrack, UserTrackState, PlayEvent,
                  PlaylistMember, OpenPlaylistItem, DiagReport, MissedSearch,
                  SubsonicKey, SubsonicStar, ZangScore):
        db.execute(delete(model).where(model.user_id == uid))

    db.execute(update(PlaylistItem).where(PlaylistItem.added_by == uid).values(added_by=None))
    db.execute(update(BeheerLog).where(BeheerLog.user_id == uid).values(user_id=0))

    # De opgeslagen wachtrij van een Subsonic-app (routers/subsonic.py).
    db.execute(delete(Meta).where(Meta.key == f"subsonic_queue:{uid}"))

    db.delete(user)

    if foto:
        try:
            Path(foto).unlink(missing_ok=True)
        except OSError as e:
            # De rij is weg; een foto die niet weg kan mag het wissen niet tegenhouden.
            log.warning("profielfoto van gebruiker %s niet gewist: %s", uid, e)
