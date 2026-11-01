"""Secrets reach the agent's commands, never the model; a chat surface is where the person types them."""

import io
import json
import sys
import threading
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

VALUE = "sk_test_51Hx-not-a-real-key"
SESSION = "20261101_010101_aaaaaa"


class Surface:
    """A chat app that shows each request and lets the test answer it, like a person would."""

    def __init__(self, shows=True):
        self.shows = shows
        self.shown = []

    def ask(self, session_id, request):
        self.shown.append((session_id, dict(request)))
        return self.shows

    def show_line(self, name):
        return f"SECRET:{name}"


def call(plugin, tool, args, session=SESSION):
    return json.loads(plugin.ctx.tools[tool](args, session_id=session))


def in_background(fn):
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("result", fn()), daemon=True)
    thread.start()
    return thread, out


def until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def chat(plugin, monkeypatch):
    surface = Surface()
    plugin.api.register_surface("chatapp", surface)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "chatapp")
    yield surface
    plugin.api.unregister_surface("chatapp")


def test_a_requested_secret_reaches_commands_but_never_the_model(plugin, chat):
    thread, out = in_background(lambda: call(plugin, "secret_request", {"name": "STRIPE_KEY", "why": "for the checkout script"}))
    until(lambda: chat.shown)
    session, request = chat.shown[0]
    assert session == SESSION
    assert {k: request[k] for k in ("name", "note", "replace")} == {"name": "STRIPE_KEY", "note": "for the checkout script", "replace": False}
    assert VALUE not in json.dumps(chat.shown)

    assert plugin.api.answer(request["id"], VALUE) is True
    thread.join(5)
    assert out["result"]["status"] == "stored"
    assert VALUE not in json.dumps(out["result"])

    # A command gets it by name, both ways.
    printed = io.StringIO()
    with redirect_stdout(printed):
        plugin._cli(SimpleNamespace(secret_action="get", name="STRIPE_KEY"))
    assert printed.getvalue() == VALUE
    command, env = plugin.run_env(["STRIPE_KEY", "--", "curl", "-u", "$STRIPE_KEY"])
    assert command == ["curl", "-u", "$STRIPE_KEY"] and env["STRIPE_KEY"] == VALUE

    # Tool output that carries it reaches the model masked.
    assert plugin.ctx.hooks["transform_tool_result"][0](result=f"curl -u {VALUE}: ok") == "curl -u «secret:STRIPE_KEY»: ok"
    assert plugin.ctx.hooks["transform_terminal_output"][0](output=f"key={VALUE}") == "key=«secret:STRIPE_KEY»"
    assert plugin.ctx.hooks["transform_tool_result"][0](result="nothing secret here") is None


def test_a_value_json_escapes_is_masked_inside_a_json_result(plugin):
    plugin.api.put("ODD_PASS", 'p@ss"w\\rd-42')
    masked = plugin._mask_tool_result(result=json.dumps({"output": 'pw=p@ss"w\\rd-42'}))
    assert json.loads(masked) == {"output": "pw=«secret:ODD_PASS»"}


def test_masking_picks_up_a_value_stored_after_the_first_read(plugin):
    plugin.api.put("FIRST", "first-value-1")
    assert plugin.mask("first-value-1") == "«secret:FIRST»"
    plugin.api.put("SECOND", "second-value-2")
    assert plugin.mask("second-value-2 first-value-1") == "«secret:SECOND» «secret:FIRST»"


def test_asking_for_a_stored_secret_answers_at_once_unless_replacing(plugin, chat):
    plugin.api.put("API_TOKEN", "old-token-value", note="ci token")
    known = call(plugin, "secret_request", {"name": "API_TOKEN", "why": "ci"})
    assert known["status"] == "exists" and known["note"] == "ci token"
    assert chat.shown == []

    thread, out = in_background(lambda: call(plugin, "secret_request", {"name": "API_TOKEN", "why": "expired", "replace": True}))
    until(lambda: chat.shown)
    assert chat.shown[0][1]["replace"] is True
    plugin.api.answer(chat.shown[0][1]["id"], "new-token-value")
    thread.join(5)
    assert out["result"]["status"] == "stored"
    assert plugin.api.get("API_TOKEN") == "new-token-value"
    assert plugin.api.about("API_TOKEN")["note"] == "expired"


def test_a_declined_request_stores_nothing(plugin, chat):
    thread, out = in_background(lambda: call(plugin, "secret_request", {"name": "DB_PASSWORD", "why": "prod db"}))
    until(lambda: chat.shown)
    plugin.api.answer(chat.shown[0][1]["id"], declined=True)
    thread.join(5)
    assert out["result"]["status"] == "declined"
    assert plugin.api.get("DB_PASSWORD") is None


def test_a_request_left_open_is_picked_up_by_the_next_ask(plugin, chat, monkeypatch):
    monkeypatch.setattr(plugin, "WAIT", 0.2)
    assert call(plugin, "secret_request", {"name": "SMTP_PASS", "why": "mail"})["status"] == "waiting"
    monkeypatch.setattr(plugin, "WAIT", 10)
    thread, out = in_background(lambda: call(plugin, "secret_request", {"name": "SMTP_PASS", "why": "mail"}))
    until(lambda: len(chat.shown) == 2)
    # Shown again under the same id, so the chat keeps one card (and gets it back after a restart).
    assert chat.shown[0][1]["id"] == chat.shown[1][1]["id"]
    plugin.api.answer(chat.shown[1][1]["id"], "hunter2-but-longer")
    thread.join(5)
    assert out["result"]["status"] == "stored"


def test_a_closed_request_ends_the_wait(plugin, chat):
    thread, out = in_background(lambda: call(plugin, "secret_request", {"name": "GH_TOKEN", "why": "repo"}))
    until(lambda: chat.shown)
    plugin.api.close(chat.shown[0][1]["id"])
    thread.join(5)
    assert out["result"]["status"] == "closed"
    assert plugin.api.answer(chat.shown[0][1]["id"], "too-late-value") is False
    assert plugin.api.get("GH_TOKEN") is None


def test_without_a_surface_the_agent_is_told_how_the_person_can_add_it(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    result = call(plugin, "secret_request", {"name": "GH_TOKEN", "why": "repo"})
    assert "hermes secret set GH_TOKEN" in result["error"]
    refusing = Surface(shows=False)
    plugin.api.register_surface("slack", refusing)
    result = call(plugin, "secret_request", {"name": "GH_TOKEN", "why": "repo"})
    assert "hermes secret set GH_TOKEN" in result["error"]
    assert len(refusing.shown) == 1


def test_an_answer_lands_in_the_profile_that_asked(plugin, chat, home, tmp_path):
    asking = home["home"]
    thread, _ = in_background(lambda: call(plugin, "secret_request", {"name": "WORK_KEY", "why": "work"}))
    until(lambda: chat.shown)
    # The answer arrives while another profile is current (the chat app's own thread).
    other = tmp_path / "other"
    other.mkdir()
    home["home"] = other
    plugin.api.answer(chat.shown[0][1]["id"], "work-only-value")
    thread.join(5)
    assert plugin.api.get("WORK_KEY") is None
    home["home"] = asking
    assert plugin.api.get("WORK_KEY") == "work-only-value"


def test_the_agent_shares_a_value_it_made_without_reading_it(plugin, chat, tmp_path):
    made = tmp_path / "generated"
    made.write_text("Gen3rated-Passw0rd\n")
    shared = call(plugin, "secret_share", {"name": "DB_ADMIN", "note": "admin login", "from_file": str(made), "delete_file": True})
    assert shared["show"] == "SECRET:DB_ADMIN"
    assert "Gen3rated" not in json.dumps(shared)
    assert not made.exists()
    assert plugin.api.get("DB_ADMIN") == "Gen3rated-Passw0rd"
    assert plugin.api.about("DB_ADMIN")["origin"] == "agent"
    assert "error" in call(plugin, "secret_share", {"name": "NOT_STORED"})


def test_changes_are_heard_by_whoever_listens(plugin, home):
    heard = []
    plugin.api.on_change(heard.append)
    plugin.api.put("HEARD", "heard-value-1")
    plugin.api.remove("HEARD")
    plugin.api.remove("HEARD")
    assert heard == [str(home["home"])] * 2


def test_other_plugins_find_the_api(plugin):
    assert sys.modules[plugin.API_MODULE] is plugin.api
    assert set(plugin.ctx.tools) == {"secret_request", "secret_share", "secret_list"}
    assert "secret" in plugin.ctx.commands


@pytest.mark.parametrize("name", ["has space", "1STARTS", "dash-ed", "", "X" * 65])
def test_names_that_cant_be_env_vars_are_refused(plugin, name):
    with pytest.raises(ValueError):
        plugin.api.put(name, "something-long")
    assert plugin.api.names() == []


def test_values_are_encrypted_on_disk(plugin):
    plugin.api.put("PLAIN_CHECK", "find-me-in-the-database")
    store = plugin.api.vault()
    store.db.execute("PRAGMA wal_checkpoint(FULL)")
    assert b"find-me-in-the-database" not in b"".join(p.read_bytes() for p in store.dir.glob("vault.db*"))
    assert plugin.api.get("PLAIN_CHECK") == "find-me-in-the-database"
    assert oct(store.key_path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.dir.stat().st_mode & 0o777) == "0o700"
