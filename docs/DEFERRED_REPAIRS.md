# Deferred Repairs

The following behavioral repairs were identified during the repository audit but remain intentionally **out of scope**. Preserve this list for dedicated follow-up changes with focused tests and migration planning.

1. Restrict Brevo synchronization to verified selections.
2. Correct Mode B role filtering and preserve extracted person/role context.
3. Redesign tracker semantics, including criteria awareness and refresh behavior.
4. Add runtime checkpointing, GitHub workflow-level concurrency controls, and live external-service integration tests. (Safe crawler concurrency and deterministic mock integration coverage are now implemented; those do not change the deferred tracker/checkpoint semantics.)

Completed separately: Hunter now enforces valid-only selection with focused
tests.

Do not combine these changes with unrelated structural refactors. Each item can affect saved data, outreach behavior, provider-credit usage, or workflow semantics and should therefore be reviewed independently.
