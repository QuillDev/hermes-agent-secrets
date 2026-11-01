"""What other plugins use: the vault, and a way to be the place a person types a secret in.

Another plugin reaches this module as `sys.modules["agent_secrets"]` (set when this plugin
registers; check it at call time, since plugins load in any order):

    secrets = sys.modules.get("agent_secrets")
    if secrets is not None:
        secrets.register_surface("myplatform", MySurface())

A surface is the UI for one platform. It shows a request and reports the answer:

    class MySurface:
        def ask(self, session_id, request) -> bool:
            '''Show {id, name, note, replace} in that chat; False if it can't (not one of yours).
            Called on the agent's tool thread.'''
        def show_line(self, name) -> str | None:
            '''Text the agent puts in a reply to show a stored secret there, e.g. "SECRET:NAME".'''

When the person answers, the surface calls `answer(request_id, value)` (or `declined=True`),
from any thread. The value goes straight into the vault of the profile that asked; the agent
only learns that it was stored. `close(request_id)` ends one nobody will answer. `ask` can come
again with an id already shown (the agent asked again); show it once.

Reads and writes (`names`, `about`, `get`, `put`, `remove`) act on the current Hermes home, so
call them inside the profile scope you mean. `on_change(fn)` hears this process's changes.
"""

from __future__ import annotations

import threading
import uuid

from .vault import Vault, check_name

_VAULTS = {}
_VAULTS_LOCK = threading.Lock()
SURFACES = {}
_LISTENERS = []
# Open requests: id -> {home, session, name, note, done (Event), outcome}
_PENDING = {}
# How requests ended, for a wait() that comes after.
_ENDED = {}
ENDED_KEPT = 200
_PENDING_LOCK = threading.Lock()


def _home():
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def vault(home=None):
    home = str(home or _home())
    with _VAULTS_LOCK:
        if home not in _VAULTS:
            _VAULTS[home] = Vault(home)
        return _VAULTS[home]


def _changed(home):
    for listener in list(_LISTENERS):
        try:
            listener(home)
        except Exception:
            pass


def on_change(listener):
    """listener(home) runs after this process stores or deletes a secret."""
    _LISTENERS.append(listener)


# The vault, for surfaces (Settings pages) and the CLI.


def names():
    return vault().names()


def about(name):
    return vault().about(name)


def get(name):
    return vault().get(name)


def put(name, value, *, note=None, origin="you"):
    home = _home()
    stored = vault(home).put(name, value, note=note, origin=origin)
    _changed(home)
    return stored


def remove(name):
    home = _home()
    removed = vault(home).remove(name)
    if removed:
        _changed(home)
    return removed


# Surfaces


def register_surface(platform, surface):
    SURFACES[str(platform)] = surface


def unregister_surface(platform, surface=None):
    if surface is None or SURFACES.get(str(platform)) is surface:
        SURFACES.pop(str(platform), None)


def surface_for(platform):
    return SURFACES.get(str(platform or ""))


def ask(session_id, platform, name, note="", replace=False):
    """Open a request in the person's chat. {id} to wait on, {status: "exists", secret} when one by
    that name is stored and not being replaced. ValueError when nowhere can show it."""
    name = check_name(name)
    home = _home()
    known = vault(home).about(name)
    if known is not None and not replace:
        return {"status": "exists", "secret": known}
    surface = surface_for(platform)
    if surface is None:
        raise ValueError(f"Nowhere in this chat can take a secret. Ask the person to store it with `hermes secret set {name}` on this machine, then use it by that name.")
    with _PENDING_LOCK:
        # Asking again after a wait ran out picks up the request that's still open. The surface is
        # asked again with the same id, so a chat app that restarted meanwhile shows it again.
        entry = next(
            ((rid, e) for rid, e in _PENDING.items() if (e["home"], e["session"], e["name"]) == (home, session_id, name)),
            None,
        )
    if entry is not None:
        request_id, entry = entry
    else:
        request_id = uuid.uuid4().hex
        entry = {"home": home, "session": session_id, "name": name, "note": str(note or "").strip()[:500], "done": threading.Event(), "outcome": None}
        with _PENDING_LOCK:
            _PENDING[request_id] = entry
    shown = surface.ask(session_id, {"id": request_id, "name": name, "note": entry["note"], "replace": known is not None})
    if not shown:
        with _PENDING_LOCK:
            _PENDING.pop(request_id, None)
        raise ValueError(f"This chat can't show a secret field. Ask the person to store it with `hermes secret set {name}`, then use it by that name.")
    return {"id": request_id}


def _finish(request_id, outcome):
    with _PENDING_LOCK:
        entry = _PENDING.pop(request_id, None)
        if entry is None:
            return None
        # Kept a while, for a tool that looks after the request already ended.
        _ENDED[request_id] = outcome
        while len(_ENDED) > ENDED_KEPT:
            _ENDED.pop(next(iter(_ENDED)))
    entry["outcome"] = outcome
    entry["done"].set()
    return entry


def answer(request_id, value=None, *, declined=False):
    """The person answered a request: store the value in the vault that asked. False if it's gone."""
    with _PENDING_LOCK:
        entry = _PENDING.get(str(request_id))
    if entry is None:
        return False
    if not declined:
        vault(entry["home"]).put(entry["name"], value, note=entry["note"] or None, origin="you")
        _changed(entry["home"])
    _finish(str(request_id), "declined" if declined else "stored")
    return True


def close(request_id):
    """Nobody will answer this one (its chat went away)."""
    _finish(str(request_id), "closed")


def wait(request_id, timeout):
    """How a request ended (stored, declined, closed), or "waiting" after `timeout` seconds."""
    with _PENDING_LOCK:
        entry = _PENDING.get(str(request_id))
    if entry is None:
        return _ENDED.get(str(request_id), "closed")
    if not entry["done"].wait(timeout):
        return "waiting"
    return entry["outcome"]
