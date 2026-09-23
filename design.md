# Design — OKX Quant Pro

A locked design system for this app. Every page redesign reads this file before
emitting code. Do not regenerate per page — extend or amend this file when the
system needs to grow.

## Genre
modern-minimal (dev-tool / quant instrument panel register)

## Macrostructure family
- App pages: 05-Workbench (real-time tabular monitoring, toolbars, modal confirmation, instant density)
- Nav: N13-Inline ⌘K Search & Command Pill
- Footer: Ft2-Inline Single Line

## Theme (Cobalt Dark Variant / High-Density Slate)
- `--color-paper`:     oklch(15% 0.015 255)  /* deep cobalt slate base */
- `--color-paper-2`:   oklch(19% 0.018 255)  /* panel card surface */
- `--color-paper-3`:   oklch(24% 0.022 255)  /* elevated hover / controls */
- `--color-ink`:       oklch(96% 0.005 255)  /* primary readable ink */
- `--color-ink-2`:     oklch(80% 0.012 255)  /* secondary captions */
- `--color-muted`:     oklch(62% 0.012 255)  /* muted labels and borders */
- `--color-rule`:      oklch(28% 0.015 255)  /* precise hairlines */
- `--color-accent`:    oklch(62% 0.20 256)   /* electric cobalt signal */
- `--color-accent-ink`: oklch(98% 0.002 256) /* crisp text on cobalt */
- `--color-focus`:     oklch(68% 0.22 256)
- `--color-pos`:       oklch(68% 0.17 152)   /* emerald green */
- `--color-neg`:       oklch(63% 0.22 25)    /* coral red */
- `--color-warn`:      oklch(76% 0.18 78)    /* amber signal */

## Typography
- Display: Inter Tight / Inter 600, style normal (no italic headers)
- Body:    Inter 400/500
- Mono:    JetBrains Mono 400/500/600 (strictly tabular numbers, uppercase tags)
- Type scale: compact fintech instrument (11px labels, 13px tabular rows, 20px KPI display)

## Spacing & Radii
- 4-point scale: `--space-3xs` (2px) to `--space-xl` (24px)
- Technical radii: 6px on buttons & inputs, 8px on panels, 9999px on pill chips (strict ruler-drawn discipline)

## Motion
- Motion intensity: 2 (sparse, no bounce, respects prefers-reduced-motion)
- Live indicator: 2s micro-pulse on status dot
- Transitions: explicit properties (color, border-color, background-color, transform) ≤ 150ms

## Microinteractions & Safety
- Silent success for polling updates
- Modal dialog with Esc, backdrop click, focus management
- 8 states implemented for interactive buttons: default, hover, focus-visible, active, disabled
