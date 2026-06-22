# l0-shell — Navigation shell + module registry (L0)

Small FE-only layer. The living surface every later layer registers its screens into. Proving phase
for the new pipeline (smallest layer, fanned out first). Intra-layer DAG: `menu-registry` (contract) →
`mount-orphans-home` (leaf).

**Layer founder-verification gate (DRAFT L0):** the founder opens the app, sees a structured, modular
menu with the four groups (and empty Nomenclatoare/Date-de-bază sections), and can navigate to the
previously-unreachable issue/return screens. No data yet — the gate is purely "the frame and the menu
are real and modular".

---

## menu-registry — [REBUILD] typed sectioned menu registry (frontend · contract · structural)

**Scope:** Replace the flat `appRoutes: AppRouteDef[]` array + the array-mapping `Nav` in
`frontend/src/shell/` with a typed, sectioned menu registry. Screens self-register; one source feeds
both `AppShell.Nav` (grouped/sorted sections, Romanian headings) and `AppRoutes` (`<Route>` generation).
Four fixed groups — `operatiuni` (process-centric arc), `nomenclatoare`/`date_de_baza` (catalog-centric),
`config`. `AppLayout` + tokens + component layer untouched.

**Contract seam (frozen here — the root of the layer):** the registry-entry type
`{ path, label, icon?, group, order, element, rightsKey? }` + the `MenuGroup` union (four groups, fixed
order) + the `registerScreen` API. Every later FE screen (L0 leaf, all of L1+, and downstream layers)
registers against this exact shape. Kept minimal: no per-screen logic leaks into the type.

**Founder-gate contribution:** delivers the structured modular menu the L0 gate checks (the four groups
visible, empty sections degrade gracefully).

---

## mount-orphans-home — [REBUILD] mount orphans + launcher Home (frontend · leaf)

**Scope:** Register the orphaned `features/warehouse-issue/IssueCounter.tsx` + `ReturnCounter.tsx`
(components exist with tests but no route — gap G7) as `operatiuni` registry entries, making them
reachable. Replace the empty `HomePage` stub (`shell/routes.tsx`) with a minimal registry-driven
launcher (grouped navigable section cards/links derived entirely from the registry — adding a screen
later auto-surfaces it on Home). Operator-density/keyboard-first; tokens + approved components only.

**Contract seam (consumes):** depends on `menu-registry` — registers entries against the frozen
entry-type and renders Home from the registry. Adds no new seam. Standing edit/storno/history +
add-new-inline are explicitly N/A (navigation/launch surfaces, no operational form, no picker) — noted
so the omission is intentional, not a gap.

**Founder-gate contribution:** delivers the "previously-unreachable issue/return screens are reachable"
half of the L0 gate, plus a real landing surface instead of the placeholder Home.
