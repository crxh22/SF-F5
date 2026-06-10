# Formatting & Language Conventions

Apply when producing new output: source files, entity names
(function/var/table/column/file/endpoint), or any user/founder-facing text.
Skip for read-only inspection or pure mechanical/format-only changes.

## Dates
- Founder-facing (filenames, messages, dialogs, dated artefact names): DD-MM-YYYY → `08-05-2026`. Dated filenames: `<topic>-DD-MM-YYYY.md`.
- Machine-parsed timestamps (frontmatter created_at/updated_at/closed_at/added_at/timestamp, any field a parser reads): ISO 8601 UTC `YYYY-MM-DDTHH:MM:SSZ`. Do not migrate these to DD-MM-YYYY.
- Rule: parser reads it → ISO UTC; only founder reads it → DD-MM-YYYY

## Language
- To founder (Texts that are addressed to the founder: escalations, default shell chat): Romanian, plain language. 
- Code, docstrings, tech docs, commits, logs, frontmatter keys, paths, identifiers: English.
- Romanian business terms (sinecost, ZN, cont de plata, aviz, factura fiscală, defectare, comandă furnizor, act predare vehicul, avans neutilizat, venit nelivrat, restant...) preserved as-is, never translated. If a translation shifts meaning, Romanian is canonical; keep it alongside any English gloss.