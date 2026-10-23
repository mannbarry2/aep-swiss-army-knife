"""Temporary forwarding file: house_xlsx.py is now toolkit_internals/house_style_excel_writer.py (2026-10-23).
It exists only because data_dictionary_v3.py was mid-run when the move
happened and still imports the old name. Removed once the Data Dictionary
moves into data_dictionary/ and imports the new location. Do not add code."""
import sys

from toolkit_internals import house_style_excel_writer as _module

if __name__ == "__main__":
    sys.exit("house_xlsx.py has moved: run toolkit_internals/house_style_excel_writer.py")
sys.modules[__name__] = _module
