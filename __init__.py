"""Secrets for the agent without the values entering the conversation.

The person types a secret into whatever chat surface supports it (a plugin registers itself as
the surface for its platform, see `api.py`), or stores one with `hermes secret set`. The agent
only ever sees names: commands read values with `hermes secret get NAME` or
`hermes secret run NAME -- <command>`, and any tool result that still contains a stored value
is masked before the model reads it. The other way, the agent stores a value it made (from a
file or a command's output) and the surface shows it to the person.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import api
from .vault import check_name

TOOLSET = "secrets"
# Other plugins find the API here (see api.py).
API_MODULE = "agent_secrets"
# The tool waits this long for the person, in short polls so a /stop gets through.
WAIT = 300
POLL = 5
# Shorter values would mask ordinary words and numbers all over tool output.
MASK_MIN = 6

USE = (
    "Use it by name and never print it: `hermes secret run {name} -- <command>` runs a command with "
    "${name} set, and `$(hermes secret get {name})` gives the value inline. Tool output that contains it "
    "shows «secret:{name}» instead."
)

GUIDANCE = (
    "Whenever work needs a secret value (password, API key, token, private key, recovery code), use the "
    "secret_* tools: never ask the person to paste one into the chat, never write one into a reply, and don't "
    "refuse or stall on work just because it needs credentials. "
)


def _platform():
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        platform = ""
    return platform or os.environ.get("HERMES_SESSION_PLATFORM", "")


def _interrupted():
    try:
        from tools.interrupt import is_interrupted
    except ImportError:
        return False
    return is_interrupted()


# Tools


def request(args, session):
    name = check_name(args.get("name"))
    asked = api.ask(session, _platform(), name, str(args.get("why") or ""), bool(args.get("replace")))
    if asked.get("status") == "exists":
        return {"status": "exists", "name": name, "note": asked["secret"].get("note") or "", "use": USE.format(name=name)}
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if _interrupted():
            return {"status": "interrupted", "name": name, "note": "The request stays open in the chat; a value typed later is still stored."}
        outcome = api.wait(asked["id"], min(POLL, max(0.0, deadline - time.monotonic())))
        if outcome == "stored":
            return {"status": "stored", "name": name, "use": USE.format(name=name)}
        if outcome == "declined":
            return {"status": "declined", "name": name, "note": "They chose not to give it. Carry on without it, or ask in the chat what they'd prefer."}
        if outcome != "waiting":
            return {"status": "closed", "name": name, "note": "The request closed without an answer."}
    return {
        "status": "waiting",
        "name": name,
        "note": "No answer yet. The request stays open in the chat; call secret_request again with the same name to keep waiting.",
    }


def share(args, _session):
    name = check_name(args.get("name"))
    path = args.get("from_file")
    if path:
        file = Path(str(path)).expanduser()
        value = file.read_text()
        # A file written by `echo`/`openssl ... >` ends in one newline that isn't part of the value.
        value = value[:-1] if value.endswith("\n") else value
        api.put(name, value, note=args.get("note"), origin="agent")
        if args.get("delete_file"):
            file.unlink()
    elif api.about(name) is None:
        raise ValueError(f"no secret named {name}: store it first (from_file, or `<command> | hermes secret set {name}`)")
    surface = api.surface_for(_platform())
    line = surface.show_line(name) if surface is not None else None
    if line:
        return {"status": "ready", "name": name, "show": line, "note": f"Put {line} on a line of its own in your reply; the person sees it there behind a reveal button."}
    return {"status": "ready", "name": name, "note": f"This chat can't show secrets. Tell the person to run `hermes secret get {name}` on this machine."}


def listing(_args, _session):
    return {"secrets": [{k: s[k] for k in ("name", "note", "origin", "updated_at")} for s in api.names()]}


TOOLS = [
    (
        "secret_request",
        request,
        GUIDANCE + "Ask the person for a secret without it entering this conversation: their chat shows a "
        "masked field, the value is stored encrypted on this machine, and you get back only its name and how to use "
        "it in commands. If one by that name is already stored you get that at once (pass replace to ask for a new "
        "value). Waits up to a few minutes for them.",
        {
            "name": {"type": "string", "description": "Env-var style name, e.g. STRIPE_TEST_KEY. Reuse an existing name for the same secret."},
            "why": {"type": "string", "description": "One short line the person sees: what it's for and where to find it."},
            "replace": {"type": "boolean", "description": "Ask for a new value even though one is stored (it's wrong or expired)."},
        },
        ["name", "why"],
    ),
    (
        "secret_share",
        share,
        GUIDANCE + "Give the person a secret value (a password you generated, a key you created) without it entering "
        "this conversation. Write the value to a file with a command (e.g. `openssl rand -base64 24 > /tmp/pw`) "
        "and pass from_file, or pipe it into `hermes secret set NAME` and pass just the name. The result says how to "
        "show it to them in your reply.",
        {
            "name": {"type": "string", "description": "Env-var style name, e.g. DB_ADMIN_PASSWORD."},
            "note": {"type": "string", "description": "One short line about what it is, shown with it."},
            "from_file": {"type": "string", "description": "Path of a file holding the value (one trailing newline is dropped)."},
            "delete_file": {"type": "boolean", "description": "Delete that file once it's stored."},
        },
        ["name"],
    ),
    (
        "secret_list",
        listing,
        "The secrets stored for you on this machine: names, notes and who added them, never values. "
        "Check here before asking the person for one.",
        {},
        [],
    ),
]


def _wrap(fn):
    def handler(args, **kwargs):
        try:
            return json.dumps(fn(args or {}, kwargs.get("session_id") or os.environ.get("HERMES_SESSION_ID") or None))
        except Exception as error:
            return json.dumps({"error": str(error)})

    return handler


# Masking stored values in what tools hand back to the model.

_KNOWN = {}


def _values():
    """Stored (name, value) pairs for the current home, longest value first, reread only after a change."""
    try:
        store = api.vault()
    except Exception:
        return []
    revision = store.revision()
    known = _KNOWN.get(str(store.dir))
    if known is None or known[0] != revision:
        known = (revision, sorted(store.values(), key=lambda pair: len(pair[1]), reverse=True))
        _KNOWN[str(store.dir)] = known
    return known[1]


def mask(text):
    if not isinstance(text, str) or not text:
        return text
    for name, value in _values():
        if len(value) < MASK_MIN:
            continue
        # The value as written, and as it reads inside a JSON string (tool results usually are).
        for form in {value, json.dumps(value)[1:-1]}:
            if form in text:
                text = text.replace(form, f"«secret:{name}»")
    return text


def _mask_tool_result(result=None, **_):
    masked = mask(result)
    return masked if isinstance(result, str) and masked != result else None


def _mask_terminal_output(output=None, **_):
    masked = mask(output)
    return masked if isinstance(output, str) and masked != output else None


# `hermes secret ...`


def _setup_cli(parser):
    sub = parser.add_subparsers(dest="secret_action", required=True)
    sub.add_parser("list", help="Stored secrets: names and notes, no values")
    get = sub.add_parser("get", help="Print a secret's value (for $(...) in a command)")
    get.add_argument("name")
    put = sub.add_parser("set", help="Store a value read from stdin or a file")
    put.add_argument("name")
    put.add_argument("--file", help="Read the value from this file instead of stdin")
    put.add_argument("--note", help="One line about what it is")
    rm = sub.add_parser("rm", help="Delete a secret")
    rm.add_argument("name")
    run = sub.add_parser("run", help="Run a command with secrets as env vars: hermes secret run NAME [NAME...] -- cmd")
    run.add_argument("rest", nargs=argparse.REMAINDER, help="NAME [NAME...] -- command")


def _cli(args):
    try:
        if args.secret_action == "list":
            for s in api.names():
                print(f"{s['name']:32} {s['origin']:5} {s.get('note') or ''}".rstrip())
        elif args.secret_action == "get":
            value = api.get(args.name)
            if value is None:
                raise ValueError(f"no secret named {args.name}")
            sys.stdout.write(value + ("\n" if sys.stdout.isatty() else ""))
        elif args.secret_action == "set":
            value = Path(args.file).expanduser().read_text() if args.file else sys.stdin.read()
            value = value[:-1] if value.endswith("\n") else value
            # A value piped in from a command is the agent's; one typed at a terminal is the person's.
            api.put(args.name, value, note=args.note, origin="you" if sys.stdin.isatty() and not args.file else "agent")
            print(f"stored {args.name}", file=sys.stderr)
        elif args.secret_action == "rm":
            print("removed" if api.remove(args.name) else f"no secret named {args.name}", file=sys.stderr)
        elif args.secret_action == "run":
            _run(args.rest)
    except (ValueError, OSError) as error:
        raise SystemExit(f"hermes secret: {error}")


def run_env(rest):
    """`NAME [NAME...] -- command` as (command, env with those secrets set)."""
    rest = list(rest)
    if "--" not in rest or rest.index("--") == 0 or rest.index("--") == len(rest) - 1:
        raise ValueError("give names, then the command after --, e.g. hermes secret run API_KEY -- curl ...")
    split = rest.index("--")
    env = dict(os.environ)
    for name in rest[:split]:
        value = api.get(name)
        if value is None:
            raise ValueError(f"no secret named {name}")
        env[check_name(name)] = value
    return rest[split + 1 :], env


def _run(rest):
    command, env = run_env(rest)
    os.execvpe(command[0], command, env)


def register(ctx):
    sys.modules[API_MODULE] = api
    for name, fn, description, properties, required in TOOLS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema={"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}},
            handler=_wrap(fn),
            description=description,
            emoji="🔑",
        )
    ctx.register_hook("transform_tool_result", _mask_tool_result)
    ctx.register_hook("transform_terminal_output", _mask_terminal_output)
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            name="secret",
            help="Secrets the agent can use by name",
            setup_fn=_setup_cli,
            handler_fn=_cli,
            description="Read and store the agent's secrets, so commands can use them without printing them.",
        )
