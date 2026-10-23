# parked/

Kept in the repo, **not for use**. Work that was stopped part-way, or a one-off
that proved its point and has no reason to run again. Nothing here is maintained.

| File | What it is | Why it is parked |
|---|---|---|
| [`audience_renamer.py`](audience_renamer.py) | Renames a development sandbox's audiences to a naming convention. | Stopped part-way on 2 Oct 2026. It refuses to run. Do not resume unless that is a deliberate decision. |
| [`adhoc_schema_patch_probe.py`](adhoc_schema_patch_probe.py) | Probe that worked out how to PATCH a field onto an ad hoc schema (the fields live on the generated class). | One-off, verified against dev on 21 Sep 2026. |
| [`firefly_port_prompt.md`](firefly_port_prompt.md) | A prompt written for porting work to Firefly. | Reference only. |

These scripts import the shared modules at the repo root (`aep_creds`,
`data_dictionary_v3`), so to run one at all, run it from the repo root with the
root on the path:

```powershell
$env:PYTHONPATH = "."; python parked\adhoc_schema_patch_probe.py --help
```
