"""find-unfinished-agent-work's own logic, on top of the shared sessionkit package.

signals     detects sessions whose work was not finished (end state, todos, final text, last turn)
repo_state  read-only git and gh checks of a session's folder, branches, and pull requests
followup    finds a later session in the same project that covers the same task
status      turns signals, later sessions, and repository facts into still open / probably done / unknown
resume      the command that resumes each session in its own harness
digest      writes digest.md (sent), sessions.json (local), inputs.json, and the focus copy
suggest     this skill's flag kinds, categories, and count for `agents suggest run`
report      maps the run's ideas back to sessions and writes one Markdown checklist
"""
