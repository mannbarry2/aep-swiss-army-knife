"""Temporary forwarding file: snapshot_tables.py is now data_dictionary/profile_snapshot_export_tables_inventory.py (2026-10-23).
It exists only because data_dictionary_v3.py was mid-run when the move
happened and still imports the old name. Removed once the Data Dictionary
moves into data_dictionary/ and imports the new location. Do not add code."""
import sys

from data_dictionary import profile_snapshot_export_tables_inventory as _module

if __name__ == "__main__":
    sys.exit("snapshot_tables.py has moved: run data_dictionary/profile_snapshot_export_tables_inventory.py")
sys.modules[__name__] = _module
