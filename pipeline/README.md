# Pipeline scripts

The Python entry points keep their original filenames in this directory. Each
one supports both direct-file execution and package-module execution from the
repository root:

```bash
python3 pipeline/run_character.py "Character Name"
python3 -m pipeline.run_character "Character Name"
```

The main groups are:

- Character generation: `run_character.py`, `expand_character.py`, `gen.py`,
  and `tripo.py`
- Mesh conversion and inspection: `convert_glb.py`, `convert_rigged.py`,
  `auto_skin.py`, `texture_check.py`, and the `render_*.py` tools
- UI generation: `gen_ui_assets.py`, `build_glyph_atlas.py`,
  `emblem_stencil.py`, `pixel_font.py`, and `sprite_codec.py`
- Announcer generation: `generate_announcer.py`, `announcer_voice.py`,
  `dump_fgm_bank.py`, and `render_announcer_refs.py`
- Evaluation support: `run_eval.py`, `make_replay.py`, `frames_from_log.py`,
  `compare_runs.py`, `crop_fighters.py`, and `report.py`

Batch sweep drivers live in `scripts/`. Evaluation experiments and generated
results remain in `eval/`.

## Tripo API v3

`tripo.py` and `web-prototype/tools/generate_mesh.py` use
`https://openapi.tripo3d.ai/v3` with the existing `TRIPO_API_KEY`.
API versions are independent of model versions: character generation retains
`v3.0-20250812`, 4,000 target faces, and the original `v1.0-20240301` rig with
Tripo bone names. Upgrading the rig model requires separate retargeting validation.

The character CLI preserves `image_token` and `consumed_credit` for existing
callers, translating v3's `file_token` and `credits_consumed`. Status output is
complete JSON, and downloads prefer `output.model_url`. Saved task IDs are
polled unchanged through `/tasks/{id}`; an existing v2 character's generation
and rig tasks were successfully queried during migration validation.

Run the offline contract tests from the repository root:

```bash
python3 -m unittest pipeline.test_tripo pipeline.test_tripo_v3
node --test web-prototype/shared/fighter-job-ui.test.js
```

Live validation on 2026-10-30 completed image upload, generation, rigging,
download, game-skeleton conversion, and binary fighter export. It consumed
30 generation credits plus 25 rigging credits. The asset generator's upload,
polling, and download paths were also exercised live; its text and multiview
request formats are covered by the offline tests.

A controlled v2/v3 comparison on the same reference and seeds (424242) produced
3,792 versus 3,635 triangles. Repeating the identical v3 generation request
produced 3,920 triangles, so generation was not byte-stable even within v3.
Rigging the exact same v2-generated mesh through v2 and v3 produced
byte-identical GLBs and final `.osb` files. Local validation artifacts (not
checked in) are in `artifacts/experiments/tripo-v2-v3-ab/`; this is a single-sample
compatibility check, not proof of universal generation equivalence.

Additional live v3 API checks re-rigged Abraham Lincoln, Quasimodo, Donatello, and
Bigfoot from their saved source mesh tasks using `v1.0-20240301`. Each new
bundle loaded in the native game and completed the 1,826-frame replay with
14 captured poses (56 screenshots total). Lincoln and Donatello's changes
from saved rigs were small numerical differences. Quasimodo's skin weights
changed more and its extreme stretch metric worsened versus the historical
rig; a fresh v2 rig of that exact source was byte-identical to the new v3
rig, excluding an API-version-specific difference in that control. Bigfoot
also had skin-weight changes from the saved rig and completed the live tour.
Local results, scripts, and contact sheets (not checked in) are under
`artifacts/experiments/tripo-v3-character-rigs/`. Existing roster assets
were not replaced. This validates API v3 with the existing rig model; it
does not test the separately versioned v3.0 rigging model.
