"""Optional settings from /etc/elan-touch.conf (INI). Every key has a measured default."""
import configparser

PATH = "/etc/elan-touch.conf"
DEFAULTS = {
    ("matcher", "accept_z"): 5.0,           # worst of 108 impostor attempts measured 4.64
    ("matcher", "learn_z"): 5.5,            # only confident matches may extend a template
    ("service", "touches_per_verify"): 5,   # distinct placements tried before "no match"
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
