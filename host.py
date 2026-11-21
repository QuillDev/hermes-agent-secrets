"""The command of the agent this plugin is loaded into.

A rebranded build of the agent renames its command and its core modules together, so the name is
read off a core module rather than written here: every command line this plugin tells the model or
the person to run is one that exists on their machine.
"""


def _command() -> str:
    try:
        import hermes_constants
    except ImportError:  # no core on sys.path (the plugin's own tests)
        return "hermes"
    return hermes_constants.__name__.partition("_")[0]


CLI = _command()
