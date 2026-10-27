# data_dictionary/

Everything that describes what exists in a sandbox: schemas, fields, datasets, audiences, journeys, snapshots. The Data Dictionary itself joins this folder once its current run has finished.

| Tool | What it does |
|---|---|
| [`single_schema_and_sample_data_extractor.py`](single_schema_and_sample_data_extractor.py) | One schema, end to end, with sampled data -- a cut-down Data Dictionary. |
| [`journeys_and_audiences_census.py`](journeys_and_audiences_census.py) | Every journey and the audience behind it; the whole audience estate with rules. |
| [`streaming_audiences_high_throughput_upgrade_triage.py`](streaming_audiences_high_throughput_upgrade_triage.py) | Every streaming audience, triaged for the High Throughput Streaming upgrade. |
| [`data_distiller_audience_expiry_countdown.py`](data_distiller_audience_expiry_countdown.py) | Data Distiller audiences and how long each has before its data expires. |
| [`journey_definitions_full_export.py`](journey_definitions_full_export.py) | Every AJO journey definition, every node, exported to JSON (needs a browser token). |
| [`profile_snapshot_export_tables_inventory.py`](profile_snapshot_export_tables_inventory.py) | Every Profile snapshot export table and its merge policy (also a Data Dictionary tab). |
| [`profile_dataset_overlap_report.py`](profile_dataset_overlap_report.py) | The Profile dataset overlap report and the cut-list (also Data Dictionary tabs). |
| [`audience_complexity_score.py`](audience_complexity_score.py) | Library: the 0-100 complexity score the Data Dictionary gives every audience. |
| [`release_notes_html_builder.py`](release_notes_html_builder.py) | Builds RELEASE_NOTES.html from RELEASE_NOTES.md. |
| [`data_dictionary_explorer_html_builder.py`](data_dictionary_explorer_html_builder.py) | Prototype: a Data Dictionary workbook as one web page, with search and clickable lineage. Draws the workbook only. |
| [`distribution_pack_builder.py`](distribution_pack_builder.py) | The hand-on zip: Explorer + workbook per market and a READ ME FIRST, version in every name. |
| [`lineage_sources_journeys_and_destinations.py`](lineage_sources_journeys_and_destinations.py) | Library: sources into datasets, AJO journeys and destinations out of audiences (Data Dictionary lineage tabs; also used by the journey census and streaming triage). |
| [`audience_rule_reader.py`](audience_rule_reader.py) | Library: the fields and audiences an audience rule reads (Data Dictionary Audiences tab). |

Run any tool from the repo root: `python data_dictionary/<tool>.py --help`.
