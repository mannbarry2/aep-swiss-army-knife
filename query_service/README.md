# query_service/

Query Service: saved query templates, scheduled queries, and who queries from outside.

| Tool | What it does |
|---|---|
| [`query_service_templates_export.py`](query_service_templates_export.py) | Exports every saved query template to local files. Read-only. |
| [`query_service_templates_renamer.py`](query_service_templates_renamer.py) | Suggests tidy names for query templates and renames them in AEP (writes). |
| [`query_service_scheduled_queries_audit.py`](query_service_scheduled_queries_audit.py) | Every scheduled query in every sandbox, classified, with anomalies flagged. |
| [`query_service_external_postgres_client_finder.py`](query_service_external_postgres_client_finder.py) | Finds queries run from external PostgreSQL clients (a route that should be closed). |

Run any tool from the repo root: `python query_service/<tool>.py --help`.
