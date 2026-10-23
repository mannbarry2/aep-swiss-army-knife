# platform_access_and_audit/

Who can reach the platform, what they do there, and the credentials used to reach it.

| Tool | What it does |
|---|---|
| [`external_access_entitlement_and_usage_audit.py`](external_access_entitlement_and_usage_audit.py) | Entitlement vs actual use, per person: the removal list. |
| [`user_activity_log_from_audit_events.py`](user_activity_log_from_audit_events.py) | What each user and API account did, from the audit log. |
| [`who_can_do_what_permissions_audit.py`](who_can_do_what_permissions_audit.py) | Who holds which role and permission, and how they got it. |
| [`role_to_permission_grid.py`](role_to_permission_grid.py) | The role -> permission grid, no people: for access-request forms. |
| [`who_loaded_data_into_datasets_investigation.py`](who_loaded_data_into_datasets_investigation.py) | Who or what loaded data into given datasets in a time window. |
| [`credential_keyring_manager_and_access_probe.py`](credential_keyring_manager_and_access_probe.py) | Stores credentials in the keyring, tests them, and probes what they can reach. |

Run any tool from the repo root: `python platform_access_and_audit/<tool>.py --help`.
