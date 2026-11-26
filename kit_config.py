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


NOTES_AUDIT_DEFAULTS = {
    'input_dir': '{work_dir}/notes-audit-input',
    'findings_dir': '{work_dir}/notes-audit-findings/{date} round {round} ({core})',
    'ledger': '{kit}/notes-audit/audited.json',
}


def notes_audit(data):
    """The notes audit's folders from the optional "notes_audit" block, with defaults under work_dir.

    Returns input_dir and ledger as paths, findings_dir as a template with {date}, {round} and
    {core}, and findings_marker: the fixed text of the findings folder that the leak check looks for
    and that a round folder must stay out of.
    """
    block = data.get('notes_audit') or {}
    if not isinstance(block, dict):
        raise ConfigError(f'"notes_audit" must be an object in {path_of()}')
    fill = {'work_dir': str(expand(need(data, 'work_dir'))), 'kit': str(Path(__file__).resolve().parent)}
    text = {name: str(block.get(name) or default) for name, default in NOTES_AUDIT_DEFAULTS.items()}
    for name in ('input_dir', 'ledger', 'findings_dir'):
        for key, value in fill.items():
            text[name] = text[name].replace('{' + key + '}', value)
    fixed = str(expand(text['findings_dir'].split('{', 1)[0]))
    marker = next((part.strip() for part in reversed(Path(fixed).parts) if part.strip()), '')
    if len(marker) < 4:
        raise ConfigError(f'"notes_audit.findings_dir" needs a fixed folder name before its first field in {path_of()}')
    return {'input_dir': expand(text['input_dir']), 'ledger': expand(text['ledger']),
            'findings_dir': text['findings_dir'], 'findings_marker': marker}
