# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

「台灣輿情情報系統」(Taiwan media/public-opinion intelligence desk) — a
single-page editorial tool for a newsroom workflow:

1. An editor manually feeds in source material (category, priority, source,
   link and/or reference text, editor note) — 投料編輯台.
2. **Webhook 1 (中文情報)**: payload → Make.com → OpenAI produces a Chinese
   intelligence draft, shown in an editable textarea.
3. **Webhook 3 (全網雷同檢查)**: the draft → Gemini web-wide similarity check
   with rewrite suggestions.
4. **Webhook 2 (英文報告)**: the human-revised Chinese draft → faithful English
   translation for management.

The workflow is deliberately two-step: revise the Chinese first, then translate.

## Architecture

- **Single file: `index.html`** — all markup, CSS, and vanilla JavaScript.
  No build step, no framework, no package.json, no tests.
- Backend logic lives in external Make.com scenarios; the three webhook URLs
  are entered by the user and cached in `localStorage`
  (`makeWebhookUrl`, `translateWebhookUrl`, `similarityWebhookUrl`).
- All three send handlers follow the same pattern: build payload → POST JSON →
  parse `result` / `text` / `message` from the JSON response (or raw text) →
  render, with `showMessage()` for ok/err feedback. Follow this pattern when
  adding a new flow.
- Payload field names (`input_id`, `reference_text`, `your_note`,
  `revised_chinese_intel`, `output_type`, …) are contracts with the Make
  scenarios — do not rename them without updating the scenarios.

## Conventions

- UI copy is **Traditional Chinese (zh-Hant)** with an "intelligence desk"
  visual language (English micro-labels like `Webhook Config`, `Chinese Draft`
  in tags are intentional).
- Styling uses CSS custom properties in `:root` (blue/cyan/green gradient
  identity). Reuse existing tokens, `.panel`, `.intel-mini-card`, and button
  classes instead of inventing new styles.
- Validation rule: `source_name` is required; at least one of `url` or
  `reference_text` must be present.
- Do not hard-code webhook URLs, API keys, or any credentials in the repo.

## Testing changes

Open `index.html` in a browser (`python3 -m http.server` works). Without real
webhooks, verify: connection-status counter (0–3 webhooks), form validation
messages, preview generation, and the copy/clear buttons for each result card.
