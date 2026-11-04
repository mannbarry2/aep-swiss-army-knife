"""Where a tool's output goes: one folder per client, so clients never mix.

The clients live in the gitignored config.json, never in code:

    "output_by_client": {
        "clients": {
            "<client>": {"org_ids": ["<org id>@AdobeOrg"], "root": "<folder>"},
            "<other client>": {"org_ids": ["..."]}
        }
    }

A client with a "root" writes there (e.g. a work OneDrive folder that syncs to
the work machine); one without writes to <output>/<client>. <output> is
AEP_OUTPUT_DIR, else the repo's gitignored output/ folder. Inside a client
folder each kind of output has its own folder, plus one archive:

    <client>/data_dictionary/            the newest workbooks
    <client>/data_dictionary_explorer/   the newest Explorer pages
    <client>/pql/                        the newest full-PQL sidecars
    <client>/archive/                    everything older

An org no client lists keeps the old flat layout (<output>/data_dictionary/...),
so nothing changes for anyone without the block.

    from toolkit_internals import output_paths
    folder = output_paths.folder(conf["org_id"], "data_dictionary")
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / "config.json"
CONFIG_KEY = "output_by_client"
KINDS = ("data_dictionary", "data_dictionary_explorer", "pql", "archive")


def output_root() -> Path:
    return Path(os.environ.get("AEP_OUTPUT_DIR") or REPO_ROOT / "output")


def clients(config_file: Path = CONFIG_FILE) -> dict[str, dict]:
    if not config_file.exists():
        return {}
    block = json.loads(config_file.read_text(encoding="utf-8")).get(CONFIG_KEY) or {}
    return {name: c for name, c in (block.get("clients") or {}).items() if isinstance(c, dict)}


def client_for(org_id: str, config_file: Path = CONFIG_FILE) -> str:
    """The client an org belongs to, or '' when none lists it."""
    for name, c in clients(config_file).items():
        if org_id and org_id in (c.get("org_ids") or []):
            return name
    return ""


def client_root(name: str, config_file: Path = CONFIG_FILE) -> Path:
    c = clients(config_file).get(name) or {}
    return Path(c["root"]) if c.get("root") else output_root() / name


def folder(org_id: str, kind: str, config_file: Path = CONFIG_FILE) -> Path:
    """Folder for one kind of output for this org's client (not created)."""
    if kind not in KINDS:
        raise ValueError(f"unknown output kind {kind!r}; one of {', '.join(KINDS)}")
    name = client_for(org_id, config_file)
    if not name:       # the old flat layout
        base = output_root() / "data_dictionary"
        return {"data_dictionary": base, "data_dictionary_explorer": base,
                "pql": base / "pql", "archive": base / "archive"}[kind]
    return client_root(name, config_file) / kind


def all_folders(kind: str, config_file: Path = CONFIG_FILE) -> list[tuple[str, Path]]:
    """(client, folder) for every client, then the old flat one as ''."""
    out = [(name, client_root(name, config_file) / kind) for name in clients(config_file)]
    return out + [("", folder("", kind, config_file))]
