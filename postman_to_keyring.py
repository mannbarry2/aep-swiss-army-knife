"""Import an Adobe 'Download for Postman' environment JSON straight into the
keyring vault, in the shape aep_creds expects.

Nothing is written to disk and no secret value is ever printed -- only key
names and a masked length. Run from the repo root with the venv active:

    python postman_to_keyring.py "C:\path\to\X.postman_environment.json" aep-prod
    python postman_to_keyring.py "...json" aep-prod --dry-run
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aep_creds  # noqa: E402

# Postman variable name -> aep_creds key. First match wins.
MAPPING = {
    "client_id":       ("CLIENT_ID", "CLIENTID"),
    "client_secret":   ("CLIENT_SECRET", "CLIENTSECRET"),
    "org_id":          ("IMS_ORG", "ORG_ID", "IMS_ORG_ID", "ORGID"),
    "tech_account_id": ("TECHNICAL_ACCOUNT_ID", "TECH_ACCOUNT_ID"),
    "scopes":          ("SCOPES", "META_SCOPE"),
    "api_key":         ("API_KEY", "APIKEY"),
    "sandbox":         ("SANDBOX_NAME", "SANDBOX"),
}


def flatten(doc: dict) -> dict[str, str]:
    """Postman env {'values':[{key,value,enabled}]} -> {KEY: value}."""
    out = {}
    for item in doc.get("values", []):
        if item.get("enabled") is False:
            continue
        key = str(item.get("key", "")).strip().upper()
        val = str(item.get("value", "") or "").strip()
        if key and val:
            out[key] = val
    return out


def flatten_console(doc: dict) -> dict[str, str]:
    """Developer Console 'Download JSON' for an OAuth Server-to-Server
    credential -> the same flat {KEY: value} shape as a Postman env."""
    proj = doc.get("project", {})
    out: dict[str, str] = {}

    ims_org = proj.get("org", {}).get("ims_org_id", "")
    if ims_org:
        out["IMS_ORG"] = ims_org

    creds = proj.get("workspace", {}).get("details", {}).get("credentials", [])
    oauth = {}
    for cred in creds:
        if cred.get("oauth_server_to_server"):
            oauth = cred["oauth_server_to_server"]
            break
    if not oauth:
        return out

    if oauth.get("client_id"):
        out["CLIENT_ID"] = oauth["client_id"]
    secrets = oauth.get("client_secrets") or []
    if secrets:
        out["CLIENT_SECRET"] = secrets[0]
    if oauth.get("technical_account_id"):
        out["TECHNICAL_ACCOUNT_ID"] = oauth["technical_account_id"]
    scopes = oauth.get("scopes") or []
    if scopes:
        out["SCOPES"] = ",".join(scopes)
    return out


def load_any(doc: dict) -> tuple[dict[str, str], str]:
    """Return (flat vars, format label) for either supported layout."""
    if "values" in doc:
        return flatten(doc), "Postman environment"
    if "project" in doc:
        return flatten_console(doc), "Developer Console JSON"
    return {}, "unrecognised"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="Postman environment JSON from Adobe.")
    ap.add_argument("service", help="Vault service name, e.g. aep-prod.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be stored; write nothing.")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"ERROR: no such file: {path}")
        return 2

    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"ERROR: not valid JSON: {exc}")
        return 2

    pm, fmt = load_any(doc)
    if not pm:
        print(f"ERROR: no populated credential fields found ({fmt}).")
        return 2

    print(f"Source : {path.name}")
    print(f"Format : {fmt}")
    label = doc.get("name") or doc.get("project", {}).get("title", "(unnamed)")
    print(f"Env    : {label}")
    print(f"Service: {args.service}\n")

    conf: dict[str, str] = {}
    for target, candidates in MAPPING.items():
        for cand in candidates:
            if cand in pm:
                conf[target] = pm[cand]
                print(f"  {target:16} <- {cand:22} (len={len(pm[cand])})")
                break

    # api_key only earns a slot when it differs from client_id.
    if conf.get("api_key") and conf["api_key"] == conf.get("client_id"):
        conf.pop("api_key")
        print("  api_key          -- same as client_id, skipped")

    # A non-default IMS host implies a non-default token endpoint.
    ims = pm.get("IMS", "")
    if ims and "ims-na1.adobelogin.com" not in ims:
        host = ims.replace("https://", "").replace("http://", "").strip("/")
        conf["oauth_url"] = f"https://{host}/ims/token/v3"
        print(f"  oauth_url        <- IMS ({host})")

    missing = [k for k in aep_creds.REQUIRED_KEYS if not conf.get(k)]
    unmapped = sorted(set(pm) - {c for cs in MAPPING.values() for c in cs}
                      - {"IMS", "ACCESS_TOKEN"})
    if unmapped:
        print(f"\n  (ignored Postman vars: {', '.join(unmapped)})")

    if missing:
        print(f"\nERROR: missing required key(s): {', '.join(missing)}")
        print("Add them by hand:  python credential_validator_v2.py store "
              f"{args.service}")
        return 1

    if args.dry_run:
        print(f"\nDRY RUN -- nothing written. {len(conf)} keys would be stored.")
        return 0

    if not aep_creds.keyring_available():
        print(f"\nERROR: {aep_creds.backend_name()} -- no usable keyring "
              "backend, refusing to write.")
        return 1

    for key, val in conf.items():
        aep_creds.kr_set(args.service, key, val)
    aep_creds.registry_add(args.service)

    print(f"\nStored {len(conf)} keys under '{args.service}' in "
          f"{aep_creds.backend_name()}.")
    print("Verify with:  python credential_validator_v2.py validate "
          f"{args.service}")
    print("Then delete the Postman JSON -- it still holds the secret in "
          "plaintext.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
