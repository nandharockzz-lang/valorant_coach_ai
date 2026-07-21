# Detector Signal Definitions

The app must separate confirmed visual evidence from proxy signals.

## Confirmed Enemy Signals

- `enemy_seen_by_detector`: a trained local detector returned an enemy/head box for a frame.
- `enemy_seen_by_vlm`: the local vision model claimed an enemy/contact cue and cited visible frame evidence.
- `enemy_seen_by_user_label`: the user manually labeled an enemy/head frame.

## Proxy Signals

- `contact_proxy`: heuristic pressure signal from motion, red regions, killfeed color, crosshair activity, and HUD changes.
- `enemy_like_red_region`: red-dominant region near center screen. This can be enemy outline, damage UI, ability VFX, or other red content.
- `combat_report_visible`: right-side combat report panel is visible. This is death evidence, not enemy identity.
- `death_event`: killfeed/combat report/manual marker evidence that the player died.

## Rule

Heuristic signals must not be displayed as confirmed enemy detection. They can only support "possible contact" or "pressure" wording.

