# Adapter palettes

Each adapter registers a required `palette.baseHue` in OKLCH degrees, from 0 up to but not including 360. Orbtile generates that adapter's working colours, keeps error red and neutrals fixed, and lets the user override the base hue in Settings.

Optional `chroma` ranges from 0.5 to 1.3 (default 1); `lightness` ranges from -0.05 to +0.05 (default 0). Values outside the protocol ranges are rejected, not clamped.

Choose a working hue outside the reserved 345.9°–75.9° arc. A hue inside that arc moves to the nearer edge and produces a warning. Avoid 205°–215° if possible: the derived waiting colour changes direction at 210.9°.

For a base hue about 115°–235°, supply `attentionHue` to make waiting clearly demand attention. Choose an alarm hue in the arc from 280° through 0° to 76°. Attention hues inside 18.9°–55° move to the nearer edge. Built-in waiting colours are approximately 43°, 306° and 355°; choose a distinct colour and read the registration's `warnings`.

Every alarm-arc hue currently produces a `proximity:` warning. The reference examples use attention hues 330° (shell), 18° (Python) and 75° (Node). Prefer a hue distinct from other adapters your users run; a calm hue is not a good alarm merely because it avoids a warning.

Warnings also describe gamut clipping, contrast adjustment and salience guards. They are information about the effective palette, not registration failures. The reply's `resolvedBaseHue` reports the working hue after the reserved-arc adjustment.

## State colours (v1.2)

Optional `palette.states` contains 1–10 six-digit sRGB hex colours, normalized to lowercase. Its editable keys are `idle`, `starting`, `thinking`, `tool`, `streaming`, `done`, `compacting`, `offline`, `waitingPermission` and `waitingQuestion`. `error`, `empty`, unknown states and an empty map are rejected.

Omitted `idle`, `starting`, `thinking`, `done`, `compacting` and `offline` colours are generated from `baseHue`. Omitted `tool` and `streaming` inherit `states.thinking`; omitted `waitingPermission` uses `attentionHue` or the derived attention hue; omitted `waitingQuestion` inherits `states.waitingPermission`. Explicit entries take precedence over inheritance.

When **One alarm colour for all agents** is enabled (`anchorAttention`), both waiting states ignore adapter waiting overrides and use the shared terracotta. Other state overrides remain active. Dragging the hue ring in Settings › Agents replaces all adapter state colours with a single-hue palette; resetting the user hue restores them. `error`, `empty` and surface neutrals never change.

Each override uses the **Hex-Anchored OKLCH ramp**: the input supplies the exact hue and a chroma ratio capped at 2.5, while lit stops receive a lightness nudge bounded to ±0.08. Explicit overrides bypass the normal error-band push, attention-hue exclusion and work-salience attenuation. Text and chip colours still meet WCAG AA 4.5:1.

For `idle` and `offline`, the `hi` stop is the exact input hex and no ramp stop gains chroma beyond the input; their other stops keep the bounded lightness nudge and the same chroma ceiling. Other states retain the normal cap of 2.5 and their anchor can differ from the input when the lightness nudge reaches its bound.

A state hue within 15° of the error hue (30.9°) produces an informational warning; Orbtile does not move an explicitly overridden state hue away from the error hue.
