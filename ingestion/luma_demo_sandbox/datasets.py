"""Adobe's Luma material in the repo, the seven datasets, and the tool's local files."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
# Adobe's platform-utils download, kept pristine (still has _yourTenantId).
ADOBE_DIR = REPO_ROOT / "demo" / "luma" / "_raw" / "platform-utils-main"
DATA_DIR = ADOBE_DIR / "luma-data"
# Run-to-run state stays in the repo's output/ (house rule 21), never synced.
STATE_DIR = REPO_ROOT / "output"
CONFIG_FILE = REPO_ROOT / "config.json"
CONFIG_KEY = "luma_demo_sandbox"

SOURCE_TENANT_KEY = "_yourTenantId"


@dataclass(frozen=True)
class Dataset:
    key: str            # name on the command line, e.g. "web-events"
    file: str           # file in luma-data/
    dataset_var: str    # Postman variable that holds the dataset id
    schema_var: str     # Postman variable that holds the schema $id
    is_event: bool      # time-series: timestamps get shifted, _ids regenerated


# Load order; row counts 1000/1000/101/3/1000/1000/1000.
DATASETS = [
    Dataset("loyalty", "luma-loyalty.json", "loyalty_dataset_id", "luma_loyalty_schema_id", False),
    Dataset("crm", "luma-crm.json", "crm_dataset_id", "luma_crm_schema_id", False),
    Dataset("products", "luma-products.json", "product_catalog_dataset_id",
            "luma_product_catalog_schema_id", False),
    Dataset("test-profiles", "luma-test-profiles.json", "luma_test_profiles_dataset_id",
            "luma_test_profiles_schema_id", False),
    Dataset("offline-purchases", "luma-offline-purchases.json",
            "luma_offline_purchase_event_dataset_id", "luma_offline_purchase_event_schema_id", True),
    Dataset("web-events", "luma-web-events.json", "luma_web_events_dataset_id",
            "luma_web_events_schema_id", True),
    Dataset("inventory-events", "luma-inventory-events.json", "product_inventory_event_dataset_id",
            "luma_product_inventory_event_schema_id", True),
]
BY_KEY = {ds.key: ds for ds in DATASETS}
EVENT_KEYS = ("web-events", "offline-purchases", "inventory-events")


def read_rows(ds: Dataset) -> list[dict[str, Any]]:
    return json.loads((DATA_DIR / ds.file).read_text(encoding="utf-8"))


def normalise_tenant(tenant: str) -> str:
    """The Schema Registry returns 'acme'; XDM field paths use '_acme'."""
    tenant = tenant.strip()
    return tenant if tenant.startswith("_") else f"_{tenant}"


def state_path(credential: str, sandbox: str) -> Path:
    return STATE_DIR / f"luma_demo_sandbox_state_{credential}_{sandbox}.json"


def load_state(credential: str, sandbox: str) -> dict[str, Any]:
    path = state_path(credential, sandbox)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(credential: str, sandbox: str, state: dict[str, Any]) -> Path:
    path = state_path(credential, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return path


def load_settings() -> dict[str, Any]:
    """This tool's block in the gitignored config.json (allow-list, inlet URLs)."""
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get(CONFIG_KEY) or {}
