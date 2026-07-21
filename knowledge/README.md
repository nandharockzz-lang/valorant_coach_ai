# VALORANT Knowledge Base

This folder stores local context for the coach.

- `curated/` contains original coaching rules that are safe to commit and travel with the app.
- `raw/` contains local snapshots downloaded from structured VALORANT data sources during rebuild.
- `index.json` is generated locally and is used for fast prompt retrieval.

Use **Automation -> Knowledge Base -> Rebuild Knowledge** after installing on another PC. The local LLM receives only the most relevant snippets for a clip, not the full knowledge base.
