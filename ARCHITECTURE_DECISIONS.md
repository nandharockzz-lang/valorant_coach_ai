# Architecture Decisions

This file records durable technical decisions for the VALORANT Coach Agent. Keep it updated when the project changes direction or when a trade-off becomes important.

## Current Product Direction

- Build a local-first personal VALORANT coach, not a replacement for public stat trackers.
- Public trackers and Riot API data are useful for match metadata and aggregate stats, but the differentiator is clip-level coaching: what happened before death, what was visible, what mistake repeated, and what the player should practice next.
- Prioritize foundational reliability before adding more advanced features: import video, identify deaths, prepare clips/frames, send local model requests, show readable advice, and preserve coach memory.

## Decisions

### ADR-001: Local-First Privacy Model

**Decision:** Gameplay videos, clips, frames, model requests, memory, and knowledge retrieval stay local by default.

**Rationale:** The user explicitly asked whether clips go online. The app should support LM Studio/Ollama/local command providers and avoid cloud uploads unless a future option is explicitly enabled.

**Implications:**
- Local HTTP to LM Studio/Ollama is acceptable.
- App logs and debug bundles must avoid leaking large raw image payloads.
- Local model audit records should store metadata and prompt previews, not full frame base64 payloads.

### ADR-002: Death Detection Strategy

**Decision:** Use VALORANT HUD evidence as the primary death detector:

- top-right killfeed OCR for the player name, currently `SicaJR`
- right-side combat report as confirmation
- older visual score detector only as fallback

**Rationale:** Static visual heuristics alone produced too many suggestions and too much manual review. The killfeed and combat report are game-specific death signals and should be more reliable.

**Implications:**
- Tesseract is required for the primary detector.
- The UI must clearly show whether the primary OCR detector ran or whether only fallback detection ran.
- Future improvement: parse killfeed row layout so the app distinguishes `SicaJR` as victim versus killer.

### ADR-003: Local Vision Model Flow

**Decision:** Clip Coach should prepare an ordered frame sequence and send images to the configured local model through LM Studio/Ollama/custom command.

Current flow:

1. Prepare frames around the death using ffmpeg.
2. Run deterministic visual signals and optional OCR.
3. Run context extraction using VALORANT vocabulary/knowledge.
4. Send final frame sequence plus structured context to the local vision model.
5. Normalize model output into one readable coach review.
6. Update local coach memory from accepted/reviewed outputs.

**Rationale:** A few keyframes were insufficient for fast enemy appearances. Dense frame sequences and multi-pass review are more likely to catch brief contact.

**Implications:**
- ffmpeg failures can stop Clip Coach before LM Studio receives any request.
- The app logs must show each stage: frame prep, context extraction POST, final review POST, response received.
- Windows subprocess output must be decoded with `encoding="utf-8", errors="replace"` to avoid cp1252 crashes.
- Every local model request must fit the configured context window, default `8192` tokens. The app should reserve output tokens, estimate image tokens, trim prompt context, and reduce frame count before sending requests to LM Studio/Ollama.

### ADR-004: Knowledge Base Role

**Decision:** The VALORANT knowledge base is context, not visual evidence.

**Rationale:** The model can use the KB to understand agents, roles, weapons, maps, callouts, and coaching rules, but it must not use KB facts to claim something was visible in the clip.

**Implications:**
- Prompts should separate "visible evidence" from "game-specific coaching constraints."
- OCR/context extraction can use KB vocabulary to normalize map, agent, weapon, and callout candidates.
- Advice confidence should drop when visual evidence is insufficient.

### ADR-005: Personal Coach Memory

**Decision:** The app should accumulate local player memory from reviews, labels, annotations, and feedback.

**Rationale:** The goal is a personal coach, not static advice. The coach should notice repeated mistakes and rank advice based on the player's history.

**Implications:**
- Accepted/rejected death suggestions and Clip Coach feedback should influence detector tuning and prompt guidance.
- Memory is stored locally in SQLite and exported/imported through local JSON.
- Future changes should preserve existing memory instead of overwriting it.

### ADR-006: Debuggability Over Silent Failure

**Decision:** When Clip Coach or detection fails, the app should tell the user which stage failed and log the traceback/details locally.

**Rationale:** Many failures happen before the LLM receives a request: missing saved settings, ffmpeg missing, frame extraction failure, OCR failure, or subprocess decode errors. Without logs, LM Studio appears idle and the user cannot tell why.

**Implications:**
- Successful Local AI tests are saved automatically for Clip Coach.
- `/api/logs` and the Automation/Tools Logs panel are the primary local debugging surface.
- Server 500s should include local traceback logs in `app_logs`.

### ADR-007: Long Video Scans Must Be Background Jobs

**Decision:** Find Deaths and other full-VOD scans must run through the job system instead of blocking the HTTP request.

**Rationale:** Full VOD death detection can involve frame extraction, image metric passes, and OCR. Running that synchronously made the UI appear frozen for long recordings.

**Implications:**
- The Find Deaths button queues a cancellable job and reports stage-level progress.
- OCR scans are bounded: use lower death-scan FPS than general analysis, pre-filter likely HUD frames before Tesseract, cap OCR frames, and timeout each OCR crop.
- If the player-name killfeed is blocked by facecam or overlay, visible combat report may create lower-confidence death suggestions instead of being ignored.
- Confirmed markers must remain preserved; only pending duplicate suggestions are cleaned.

## Skill Usage

- Use `senior-architect` when making architecture decisions, ADRs, major trade-offs, or system design changes.
- Use focused implementation and validation for code changes: inspect current code, patch narrowly, run compile/smoke checks, restart the app when needed, then commit/push if the change should reach the other PC.
- For future multi-session work, add or update this file when a decision changes the product direction, privacy model, model pipeline, storage model, or UX workflow.
