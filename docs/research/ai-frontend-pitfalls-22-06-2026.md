# AI-Generated Frontend: Failure Modes & Counters

**Date:** 22-06-2026  
**Scope:** Pitfalls of LLM/AI coding agents building React/TypeScript frontend, with concrete mitigations per pipeline stage.  
**Audience:** Engineering teams running autonomous agent pipelines with no human visual review.

> **Verification note:** The "92% / 78%" empty-state statistics cited on blog.vibecoder.me have no traceable primary source (not NNG). They are treated as illustrative, not authoritative. All other claims are cross-verified across ≥2 independent sources.

---

## Failure Mode Map

### FM-1 — Visual / Aesthetic Mediocrity ("Generic AI Look")

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | LLMs are trained on billions of examples, so they regress to statistical averages: `indigo-600`/`slate-900` default Tailwind palette (appears in "roughly a billion tutorials"), centered hero→stacked-sections→cards layout, `Inter` for everything, `rounded-2xl shadow-lg p-6` on every card. The model produces "the visual equivalent of technically correct prose with zero voice." [Addy Osmani](https://addyosmani.com/blog/ai-coding-workflow/), [Sean Fay / Medium](https://medium.com/@ssbob98/your-ai-agent-can-code-it-cant-design-here-s-how-i-fixed-that-e1ced4c444ca), [Alan West / DEV](https://dev.to/alanwest/how-to-fix-the-ai-generated-look-in-your-frontend-1ahh) |
| **How to detect** | Side-by-side visual audit: does the palette look like a Tailwind tutorial? Is the layout hero→features→CTA? Does every card have the same shadow? ESLint rule flagging banned class names (`indigo-*`, `slate-*`, `rounded-2xl`) catches it in CI. |
| **Concrete counters** | **Spec gate:** Supply a `design-tokens.json` and `design-brief.md` with exact hex/HSL palette, spacing scale (8-point grid), typography pairs (not "Inter"), shadow recipe, and named border-radius variants — before any generation. **Build:** Replace the entire Tailwind color config with custom token names (`ink`, `ember`) so banned defaults produce a build error. **Build:** Provide 2–3 reference-screen screenshots or a Figma link — "layout like Linear's issue list" is more effective than adjectives. **Automated check:** ESLint rule that fails CI on banned class patterns. |
| **Pipeline stage** | Spec gate → Build (constraints) → Automated check (linter) |

---

### FM-2 — Inconsistency Across Screens / Components

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | Each agent invocation has no memory of prior sessions. Without a shared vocabulary, the same Figma color produces different code across sessions; 5 different button styles can appear within 3 weeks of autonomous generation. One developer described the result as "like 10 devs worked on it without talking to each other." [Addy Osmani](https://addyosmani.com/blog/ai-coding-workflow/), [Rajkumar Samra](https://www.rajkumarsamra.me/blog/frontend-engineering-with-ai-agents) |
| **How to detect** | Automated: grep/AST scan for inline hex values, direct Tailwind color literals, duplicate component definitions (e.g. multiple `Button.tsx` variants). Visual regression baseline diff across all routes. |
| **Concrete counters** | **Spec gate:** A single `CLAUDE.md` / `AGENTS.md` listing every approved component with import path, props, and usage example — agents look up, not invent. **Build:** Enforce component wrappers: thin, typed abstractions over the vendor library that expose only intended props; JSDoc every parameter. **Automated check:** CI linter forbidding direct vendor imports (e.g. `from '@mui/material/Button'` instead of `from '@/components/ui/Button'`). **Human review:** Periodic visual audit of one full user flow per sprint; cheapest meaningful checkpoint. |
| **Pipeline stage** | Spec gate → Build (component wrappers) → Automated check (import linter) → Human review (periodic) |

---

### FM-3 — Design-Token Drift & Hardcoded Styles

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | When agents lack a token dictionary they invent values: `#6366F1`, `padding: '16px'`, undocumented prop choices. "LLMs are bad at consistent invention. They're excellent at lookups." Without enforcement, codebases accumulate a "drift tax" with every generation cycle. [Ali Afsah-Noudeh / Medium](https://medium.com/@aliafsah1988/your-ai-coding-agent-is-only-as-good-as-your-design-system-6055e4667fa9), [managed-code.com](https://www.managed-code.com/blog-post/ai-slop-in-design) |
| **How to detect** | ESLint: flag any hex literal or bare pixel value outside a token file. CI token-audit script: count components that reference `theme.*` vs. raw values. Shadow-recipe audit: inconsistent box-shadow formulas across components. |
| **Concrete counters** | **Spec gate:** Publish `tokens.json` (colors, spacing, typography, elevation, radius) as the only source of truth; include a Figma↔token mapping table in `AGENTS.md`. **Build:** Zod schema over token values so hallucinated tokens fail TypeScript compilation. **Automated check:** ESLint rule + CI script rejecting hex literals and magic numbers in style props. **Automated check:** "Normalize-into-system" gate: sample 10 components, require ≥8 map cleanly across color/type/spacing/radius/elevation before merge. |
| **Pipeline stage** | Spec gate → Build (Zod/TS enforcement) → Automated check (linter + token-audit) |

---

### FM-4 — Hallucinated / Wrong Component APIs & Props

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | LLMs sample from a probability distribution over component surface areas. Large vendor libraries (MUI, Chakra) expose enormous APIs with conflicting options; the model passes `isLoading` where the real prop is `loading`, uses `direction: "ascending"` where the API expects `sortDirection: 0 \| 1`, or passes a prop that was removed in a major version. Above ~6 props, the model starts guessing combinations. Index signatures are "a blank cheque." A single wrong closing `</div>` or invalid prop can white-screen the app. [Yash Kapure](https://www.yashkapure.com/en/blog/frontend-architecture-for-ai-codegen/), [DEV.to / wisethewizard](https://dev.to/wisethewizard/why-asking-llms-to-generate-reactnested-code-is-a-dead-end-for-agent-ui-2821) |
| **How to detect** | TypeScript strict mode at compile time (cheapest). Runtime: type errors in test output. Integration tests that mount every screen and assert no console errors. |
| **Concrete counters** | **Spec gate:** Pin library versions in `AGENTS.md`; include condensed prop tables for each approved component. **Build:** Discriminated unions instead of loose boolean flags. Replace bare `string` props with fixed union types — wrong values fail to compile. Keep component APIs to ≤6 meaningful props; anything more should be a compound component. **Automated check:** `tsc --noEmit` in CI as a hard gate. **Automated check:** Component-mount smoke tests (render every route, assert no thrown errors). |
| **Pipeline stage** | Spec gate → Build (TypeScript discipline) → Automated check (tsc + smoke tests) |

---

### FM-5 — Wrong / Over-Heavy UI Controls (Poor IA)

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | LLMs optimize for "plausible-looking" patterns from training data. A `<select>` dropdown is statistically more common than a toggle or radio group, so the model defaults to it even when 2 options exist. It "reorders steps to be helpful" without understanding the user's mental model, breaking flows. [Francesca Tabor](https://www.francescatabor.com/articles/2025/9/6/ux-design-without-designers-how-llms-are-rewriting-ui-in-real-time), [gendesigns.ai](https://gendesigns.ai/blog/ai-generated-ui-mistakes-how-to-fix) |
| **How to detect** | Spec review: for every data-entry field, check: is the control the minimum necessary friction? Automated: custom ESLint rule flagging `<select>` when the schema constrains to ≤3 options (requires prop introspection — partially tractable). Manual heuristic review per screen. |
| **Concrete counters** | **Spec gate:** For each field in the data model, specify the exact control type and justify it in the spec (`status: 2 values → toggle, not select`). Provide a control-selection heuristic table in `AGENTS.md` (0-2 options = toggle; 3-5 = radio group; 5+ = select; ≥10 searchable = combobox). **Build:** Require the agent to output a "control rationale" comment for every interactive element. **Human review:** UX walkthrough of each new form/screen against the heuristic table. |
| **Pipeline stage** | Spec gate (control spec) → Build (rationale comments) → Human review (form audit) |

---

### FM-6 — Missing Loading / Error / Empty / Disabled States

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | The model generates the "happy path" by default. State machines for loading/error/empty/disabled are not visible in the training examples that match "build a data table." Real apps spend roughly 30% of time in off-happy-path states; AI generates only the 70%. [vibecoder.me](https://blog.vibecoder.me/empty-states-loading-states-error-states) (statistics unverified), [gendesigns.ai](https://gendesigns.ai/blog/ai-generated-ui-mistakes-how-to-fix), [ministryofprogramming.com](https://ministryofprogramming.com/blog/why-ai-generated-ui-fails-in-production) |
| **How to detect** | Automated: vitest stories / Storybook stories — assert that every data-fetching component has a `loading`, `error`, and `empty` story. CI gate: grep component files for `isLoading`, `error`, `isEmpty` (or equivalent) and block merge if absent. |
| **Concrete counters** | **Spec gate:** Every screen spec must enumerate required states explicitly: `loading (skeleton), error (message + retry), empty (explanation + CTA), disabled (visual + aria)`. **Build:** Skeleton screens (not spinners) as the default loading pattern — specify this in `AGENTS.md`. Error state must answer: what went wrong (plain language), what the user can do, how to return. Empty state must explain why + offer a next action. **Automated check:** Storybook/Chromatic stories for each state as a CI merge gate. **Automated check:** `jest-axe` test that mounts each state and runs axe-core. |
| **Pipeline stage** | Spec gate → Build (explicit state spec) → Automated check (Storybook + axe stories) |

---

### FM-7 — Accessibility Omissions

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | "Most LLMs optimize for visual output while generating near-zero semantic information" — they see rendered pixels, not the accessibility tree. Common outputs: `<div onClick>` instead of `<button>`, missing `aria-expanded`/`aria-controls`, unfocusable elements, no `onKeyDown`, unlabeled SVG icons, absent list semantics, no landmark roles. A typical AI-generated sidebar component has 10 distinct accessibility failures. Prevention costs 3–8 min/component; remediation costs 45–90 min/component. [Frontend Masters](https://frontendmasters.com/blog/ai-generated-ui-is-inaccessible-by-default/) |
| **How to detect** | Static: `eslint-plugin-jsx-a11y` (catches `<div onClick>`, missing labels — cheap, deterministic). Runtime: `jest-axe` in unit tests. CI: Playwright + axe-core on rendered routes. Manual: keyboard-only walkthrough + screen reader spot-check (covers the ~15–30% automated tools miss). |
| **Concrete counters** | **Spec gate:** Bake WCAG AA requirements into `AGENTS.md`: "all interactive elements must be `<button>` or `<a>`; all custom controls must carry `role`, `aria-*` state, and `onKeyDown`; minimum contrast 4.5:1 normal text, 3:1 large text; touch targets ≥44px." **Build:** Use headless-UI primitives (Radix UI, React Aria) — encode semantics into the component API so the agent cannot omit them. **Automated check:** `eslint-plugin-jsx-a11y` as a build error. `jest-axe` in vitest suite as a merge gate. **Human review:** Keyboard-only tab traversal of each new form/modal before release. |
| **Pipeline stage** | Spec gate → Build (Radix/React Aria) → Automated check (eslint-a11y + jest-axe) → Human review (keyboard nav) |

---

### FM-8 — Responsive & Overflow Breakage

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | Agents generate desktop-first layouts; mobile breakpoints are afterthoughts. Missing viewport meta tag, incomplete Tailwind config, absent CSS `overflow` handling, and no test at non-default viewport widths cause silent breakage. The model never sees the rendered page so it cannot observe that a sidebar overflows at 375px. [BrowserStack responsive guide](https://www.browserstack.com/guide/responsive-design-breakpoints), [humansfix.ai Tailwind guide](https://humansfix.ai/guides/v0/tailwind-responsive-breakpoints-mobile) |
| **How to detect** | Playwright visual test at three viewports (375px, 768px, 1280px) — screenshot diff catches layout collapse. `overflow: hidden` audit: grep for elements that might clip content. Chromatic/Percy cross-browser snapshots. |
| **Concrete counters** | **Spec gate:** Every screen spec declares target viewports and lists components that must reflow. Enforce content-driven breakpoints, not device-specific widths. **Build:** In `AGENTS.md`: "every layout component must include sm/md/lg Tailwind variants; no fixed-pixel widths on containers; test at 375, 768, 1280." **Automated check:** Playwright screenshot tests at three viewports as CI gate. ESLint rule banning `w-[Xpx]` on container-level elements. |
| **Pipeline stage** | Spec gate → Build (responsive mandate) → Automated check (Playwright multi-viewport) |

---

### FM-9 — Over-Engineering & Unnecessary Complexity

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | Agents misdiagnose scope and "overengineering" is one of the three problems the Martin Fowler harness article identifies as hardest to catch — it evades both computational and inferential sensors because it stems from incomplete specs. The agent adds abstraction layers, context providers, and complex state machines where a simple prop-pass would do. "Every model fails past a certain complexity threshold" — and when an agent handles too many simultaneous responsibilities, hallucinations increase. [Martin Fowler / harness-engineering](https://martinfowler.com/articles/harness-engineering.html), [Isaac Hagoel / DEV](https://dev.to/isaachagoel/read-this-before-building-ai-agents-lessons-from-the-trenches-333i) |
| **How to detect** | Cyclomatic complexity metric (computationally cheap, deterministic). File line-count gate. PR diff size gate (over N lines for a single feature = red flag). Code review: "can I code this without losing functionality?" test per sub-task. |
| **Concrete counters** | **Spec gate:** Spec includes explicit "NOT goals": "do not introduce new state management libraries; use React's built-in state; no new abstractions beyond what is in the component library." **Build:** Scoped, single-task agent invocations — one component at a time. Ask: "Can this be done deterministically without the LLM?" — if yes, use a script. **Automated check:** Complexity linter (ESLint complexity rule, max 10). File/function size limits enforced in CI. **Human review:** Architectural review for any new abstraction layer (cheaply: a 5-min scan of the diff). |
| **Pipeline stage** | Spec gate (NOT goals) → Build (scoped tasks) → Automated check (complexity linter) → Human review (new abstractions) |

---

### FM-10 — No Visual Feedback Loop (Tests Check Behavior, Not Appearance)

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | The agent never sees what it built. Vitest/Jest checks DOM structure and behavior but cannot detect that a button is invisible against its background, that a table overflows its card, or that two screens use different type scales. "AI-generated test suite is green" provides false confidence. The behavioral harness "remains unsolved" as a standalone quality signal. [Martin Fowler](https://martinfowler.com/articles/harness-engineering.html), [Sauce Labs visual testing](https://saucelabs.com/resources/blog/comparing-the-20-best-visual-testing-tools-of-2026) |
| **How to detect** | The absence itself is the signal: if the pipeline has no screenshot/snapshot step, it has this failure mode. |
| **Concrete counters** | **Automated check — Tier 1 (cheapest):** Playwright's built-in `toHaveScreenshot()` — no extra service, run in CI, captures per-route baseline images. Diff on every PR. **Automated check — Tier 2 (component-level):** Storybook + Chromatic (built by Storybook team, component-level snapshots, fewer false positives than full-page). Free tier: 5,000 snapshots/month. **Automated check — Tier 3 (AI diffing):** Percy — AI-powered diff that filters noise (font-smoothing won't fail; a 20px button shift will). Best for teams needing cross-browser coverage and a review dashboard. **Human review:** Even monthly: one human visually walks one complete user flow per feature area — "does this look like a coherent product?" — to catch what automated diffs miss (taste, hierarchy, consistency). [Chromatic visual testing guide](https://www.chromatic.com/blog/how-to-visual-test-ui-using-playwright/), [Playwright visual testing](https://codoid.com/automation-testing/playwright-visual-testing-a-comprehensive-guide-to-ui-regression/) |
| **Pipeline stage** | Automated check (Playwright screenshots + Storybook/Chromatic) → Human review (periodic visual walk) |

---

### FM-11 — Copy / i18n Issues (Romanian ERP context)

| Dimension | Detail |
|-----------|--------|
| **Why it happens** | Agents default to English placeholder text (`"No data found"`, `"An error occurred"`, `"Submit"`). Without explicit locale instructions they hardcode strings, skip i18n wrapping, and use non-idiomatic phrasing. [gendesigns.ai](https://gendesigns.ai/blog/ai-generated-ui-mistakes-how-to-fix) |
| **How to detect** | Grep for hardcoded UI strings outside translation files. i18n linter (e.g. `eslint-plugin-i18n-json`). Native speaker review of copy in each new screen. |
| **Concrete counters** | **Spec gate:** Mandate all user-facing strings in translation keys (e.g. `i18next` / `react-intl`); include a Romanian glossary of domain terms in `AGENTS.md`. **Automated check:** ESLint rule flagging hardcoded string literals in JSX. **Human review:** Romanian native speaker reviews copy for each screen at handoff. |
| **Pipeline stage** | Spec gate → Automated check (i18n linter) → Human review (copy review) |

---

## Pipeline Stage Summary

| Stage | What it catches |
|-------|----------------|
| **Spec gate** (before any code is written) | Generic palette, missing control specs, absent state requirements, no token dictionary, no NOT-goals |
| **Build** (agent invocation constraints) | Token usage (Zod/TS), component wrappers, headless-UI primitives, scoped single-task invocations |
| **Automated check** (CI, no human needed) | Token drift (ESLint), import boundaries, TypeScript errors (tsc), a11y (eslint-a11y + jest-axe), complexity, visual regression (Playwright/Chromatic), i18n |
| **Human review** (targeted, minimal) | Visual taste / consistency walk, keyboard nav, copy review, new abstraction decisions |

---

## Top Counters to Adopt First

These deliver the highest ROI for an autonomous pipeline with no human visual review today:

1. **`AGENTS.md` / `CLAUDE.md` design contract** — token file + component registry + control heuristics + NOT-goals. Single highest-leverage spec-gate investment. Covers FM-1 through FM-5.

2. **TypeScript strict mode + discriminated union props + `tsc --noEmit` in CI** — catches hallucinated props before they white-screen users. Covers FM-4. Zero ongoing maintenance cost.

3. **Playwright `toHaveScreenshot()` at 3 viewports (375/768/1280)** — first visual feedback loop; no service cost. Covers FM-8 and partially FM-10. Implement once, runs on every PR.

4. **`eslint-plugin-jsx-a11y` + `jest-axe`** — catches 70–85% of accessibility omissions at build time; 3–8 min prevention vs. 45–90 min remediation. Covers FM-7.

5. **Storybook stories per state (loading/error/empty) as CI merge gate** — forces agents to implement all states and gives the first component-level visual baseline. Covers FM-6 and FM-10.

6. **ESLint rule banning hardcoded hex values and default Tailwind palette** — makes token drift a build error, not a code review finding. Covers FM-1, FM-3. One-time setup.

7. **Import boundary linter** (`eslint-plugin-boundaries` or custom rule) — blocks direct vendor imports, enforcing the component wrapper layer. Covers FM-2, FM-3.

8. **Monthly human visual walk of one complete user flow** — the irreducible minimum human checkpoint that catches what every automated tool misses: taste, coherence, hierarchy. Covers FM-1, FM-2, FM-10. Cost: ~30 min/month.

---

## Sources

- [Addy Osmani — My LLM coding workflow going into 2026](https://addyosmani.com/blog/ai-coding-workflow/)
- [Sean Fay — Your AI Agent Can Code. It Can't Design.](https://medium.com/@ssbob98/your-ai-agent-can-code-it-cant-design-here-s-how-i-fixed-that-e1ced4c444ca)
- [Frontend Masters — AI-Generated UI Is Inaccessible by Default](https://frontendmasters.com/blog/ai-generated-ui-is-inaccessible-by-default/)
- [Ali Afsah-Noudeh — Your AI coding agent is only as good as your design system](https://medium.com/@aliafsah1988/your-ai-coding-agent-is-only-as-good-as-your-design-system-6055e4667fa9)
- [Yash Kapure — Frontend Architecture for the Age of AI Codegen](https://www.yashkapure.com/en/blog/frontend-architecture-for-ai-codegen/)
- [managed-code.com — AI in UI Design: Avoiding "AI Slop"](https://www.managed-code.com/blog-post/ai-slop-in-design)
- [Alan West / DEV — How to fix the 'AI-generated' look in your frontend](https://dev.to/alanwest/how-to-fix-the-ai-generated-look-in-your-frontend-1ahh)
- [vibecoder.me — Empty States, Loading States, Error States (statistics unverified)](https://blog.vibecoder.me/empty-states-loading-states-error-states)
- [gendesigns.ai — 15 AI-Generated UI Mistakes and How to Fix Every One](https://gendesigns.ai/blog/ai-generated-ui-mistakes-how-to-fix)
- [Ministry of Programming — Why AI-Generated UI Fails in Production](https://ministryofprogramming.com/blog/why-ai-generated-ui-fails-in-production)
- [Rajkumar Samra — Frontend Engineering with AI Agents](https://www.rajkumarsamra.me/blog/frontend-engineering-with-ai-agents)
- [Martin Fowler — Harness engineering for coding agent users](https://martinfowler.com/articles/harness-engineering.html)
- [Isaac Hagoel / DEV — Read This Before Building AI Agents](https://dev.to/isaachagoel/read-this-before-building-ai-agents-lessons-from-the-trenches-333i)
- [Francesca Tabor — UX Design Without Designers? How LLMs Are Rewriting UI](https://www.francescatabor.com/articles/2025/9/6/ux-design-without-designers-how-llms-are-rewriting-ui-in-real-time)
- [DEV.to / wisethewizard — Why asking LLMs to generate React/Nested code is a dead end](https://dev.to/wisethewizard/why-asking-llms-to-generate-reactnested-code-is-a-dead-end-for-agent-ui-2821)
- [Chromatic — Visual testing with Playwright](https://www.chromatic.com/blog/how-to-visual-test-ui-using-playwright/)
- [Sauce Labs — 20 Best Visual Regression Testing Tools of 2026](https://saucelabs.com/resources/blog/comparing-the-20-best-visual-testing-tools-of-2026)
- [Codoid — Playwright Visual Testing guide](https://codoid.com/automation-testing/playwright-visual-testing-a-comprehensive-guide-to-ui-regression/)
- [Design Systems Collective — Mapping your design system for AI agents](https://www.designsystemscollective.com/codebase-indexing-for-design-systems-agents-c0f6b563a39e)
- [Tympanus/Codrops — Supercharge Your Design System with LLMs and Storybook MCP](https://tympanus.net/codrops/2025/12/09/supercharge-your-design-system-with-llms-and-storybook-mcp/)
