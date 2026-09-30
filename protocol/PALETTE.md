# Adapter palettes

Register a required `palette.baseHue` in OKLCH degrees, from 0 up to but not including 360. Orbtile generates the working colours, keeps error red and neutrals fixed, and lets the user override the base hue in Settings.

Optional `chroma` ranges from 0.5 to 1.3 (default 1); `lightness` ranges from -0.05 to +0.05 (default 0). Values outside the protocol ranges are rejected, not clamped.

Choose a working hue outside the reserved 345.9°–75.9° arc. A hue inside that arc moves to the nearer edge and produces a warning. Avoid 205°–215° if possible: the derived waiting colour changes direction at 210.9°.

For a base hue about 115°–235°, supply `attentionHue` to make waiting clearly demand attention. Choose an alarm hue in the arc from 280° through 0° to 76°. Attention hues inside 18.9°–55° move to the nearer edge. Built-in waiting colours are approximately 43°, 306° and 355°; choose a distinct colour and read the registration's `warnings`.

Every alarm-arc hue currently produces a `proximity:` warning. The reference examples use attention hues 330° (shell), 18° (Python) and 75° (Node). Prefer a hue distinct from other adapters your users run; a calm hue is not a good alarm merely because it avoids a warning.

Warnings also describe gamut clipping, contrast adjustment and salience guards. They are information about the effective palette, not registration failures. The reply's `resolvedBaseHue` reports the working hue after the reserved-arc adjustment.
