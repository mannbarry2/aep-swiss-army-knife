"""Temporary forwarding file: audience_complexity.py is now data_dictionary/audience_complexity_score.py (2026-10-23).
It exists only because data_dictionary_v3.py was mid-run when the move
happened and still imports the old name. Removed once the Data Dictionary
moves into data_dictionary/ and imports the new location. Do not add code."""
import sys

from data_dictionary import audience_complexity_score as _module

if __name__ == "__main__":
    sys.exit("audience_complexity.py has moved: run data_dictionary/audience_complexity_score.py")
sys.modules[__name__] = _module
