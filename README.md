# agent-secrets

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent work with secrets (API keys, passwords, tokens) without the values ever entering the conversation.

Models tend to refuse or stall on work that needs credentials, because the only way they have to get one is to ask you to paste it into the chat. This plugin gives them a safe path instead.

## What the agent gets

- `secret_request(name, why)` asks you for a secret. Your chat app shows a masked field, the value goes into an encrypted store on this machine, and the agent gets back only "stored" and the name.
- `secret_share(name, from_file)` hands you a value the agent made (a generated password, a new key) without the agent reading it. Your chat app shows it behind a reveal button.
- `secret_list()` lists the names and notes, never values.
- `hermes secret get NAME` and `hermes secret run NAME [NAME...] -- <command>` let the agent's commands use a value by name.
- Any tool result that still contains a stored value (6+ characters, raw or JSON-escaped) is masked as `«secret:NAME»` before the model reads it.

The tool descriptions tell the agent to use these for any secret instead of asking in chat or refusing.

## Where you type and see secrets

This plugin has no UI of its own. A chat app plugin registers itself as the *surface* for its platform and draws the request and share cards; [Nako Portal](https://github.com/QuillDev/nako-portal) does. Without a surface, the agent tells you to store the value yourself:

```sh
hermes secret set STRIPE_KEY          # reads the value from stdin
hermes secret list
hermes secret rm STRIPE_KEY
```

## Writing a surface

Other plugins reach the API as `sys.modules["agent_secrets"]` once this plugin has loaded (check at call time). See `api.py` for the full contract:

```python
secrets = sys.modules.get("agent_secrets")
if secrets is not None:
    secrets.register_surface("myplatform", MySurface())

class MySurface:
    def ask(self, session_id, request):   # {id, name, note, replace}; show it, return True
        ...
    def show_line(self, name):            # what the agent writes in a reply to show one
        return f"SECRET:{name}"

# when the person answers:
secrets.answer(request_id, value)       # or answer(request_id, declined=True)
```

`names()`, `about()`, `get()`, `put()`, `remove()` act on the current Hermes profile, for a settings page.

## Storage

One SQLite file per Hermes profile at `<HERMES_HOME>/agent-secrets/vault.db`, values encrypted with Fernet under `vault.key` (0600, in a 0700 folder). That protects copies of the file, such as backups. It doesn't protect against anything already running as your account, which can read both. That's by design, since the agent's commands need the values.

## Install

```sh
hermes plugins install https://github.com/QuillDev/hermes-agent-secrets
hermes gateway restart
```

## Tests

```sh
python -m pytest tests   # needs pytest and cryptography
```
