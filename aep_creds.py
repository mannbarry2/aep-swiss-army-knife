"""Temporary forwarding file: aep_creds.py is now toolkit_internals/credential_loader.py (2026-10-23).
It exists only because data_dictionary_v3.py was mid-run when the move
happened and still imports the old name. Removed once the Data Dictionary
moves into data_dictionary/ and imports the new location. Do not add code."""
import sys

from toolkit_internals import credential_loader as _module

if __name__ == "__main__":
    sys.exit("aep_creds.py has moved: run toolkit_internals/credential_loader.py")
sys.modules[__name__] = _module
