"""turn-corrections-into-skills: find where the user corrected their coding agent and propose one skill per scenario.

The modules here hold this skill's own logic. Session reading, redaction, and the usage-billed
`agents suggest run` plumbing come from the shared `sessionkit` package next to this folder, which
is a byte copy that must never be edited in this skill.
"""
