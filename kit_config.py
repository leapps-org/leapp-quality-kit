"""Load the kit's settings: one JSON file shared by every tool in this repository.

The file is found at $LEAPP_QUALITY_CONFIG, or else ~/.config/leapp-quality-kit/config.json.
Copy config.example.json there and fill it in. Nothing in this repository hardcodes an account,
a corpus path or a repository list.
"""
import json
import os
from pathlib import Path

DEFAULT = Path.home() / '.config' / 'leapp-quality-kit' / 'config.json'


class ConfigError(Exception):
    """The config file is missing or lacks a setting a tool needs."""


def path_of():
    return Path(os.environ.get('LEAPP_QUALITY_CONFIG') or DEFAULT).expanduser()


def load():
    where = path_of()
    try:
        data = json.loads(where.read_text(encoding='utf-8'))
    except OSError as error:
        raise ConfigError(f'no config at {where}; copy config.example.json there') from error
    except ValueError as error:
        raise ConfigError(f'{where} is not valid JSON') from error
    if not isinstance(data, dict):
        raise ConfigError(f'{where} must hold a JSON object')
    return data


def need(data, name):
    if name not in data or data[name] in (None, '', [], {}):
        raise ConfigError(f'set "{name}" in {path_of()}')
    return data[name]


def expand(text):
    return Path(os.path.expandvars(str(text))).expanduser()
