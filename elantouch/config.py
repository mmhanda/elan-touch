"""Optional settings from /etc/elan-touch.conf (INI). Every key has a measured default."""
import configparser

PATH = "/etc/elan-touch.conf"
DEFAULTS = {
    ("matcher", "accept_z"): 6.0,           # worst impostor measured on a clean template: 4.64
    ("service", "touches_per_verify"): 3,   # distinct placements tried before "no match"
    ("service", "lockout_after"): 3,        # consecutive failed prompts before a cooldown
    ("service", "lockout_seconds"): 60,     # first cooldown; doubles each time, capped at 15 min
}


def load(path=PATH):
    parser = configparser.ConfigParser()
    parser.read(path)
    out = {}
    for (section, key), default in DEFAULTS.items():
        try:
            out[key] = type(default)(parser.get(section, key))
        except (configparser.Error, ValueError):
            out[key] = default
    return out
