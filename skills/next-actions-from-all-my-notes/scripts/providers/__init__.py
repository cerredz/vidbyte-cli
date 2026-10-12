"""One adapter per note provider, each copied from that provider's own next-actions-from-* skill.

Every module has the same interface, which all_notes.py calls:

  NAME, LABEL, ITEM_WORDS, REFERENCE   the provider's key, display name, item words, and setup file
  detect(opts) -> dict                 free and local: is the provider set up? Never reads notes.
  check(opts, log) -> dict             one free, read-only API call that confirms access
  collect(opts, since, now, log)       reads the items created or edited since `since`
  from_items(opts, items, since, now)  filters items a host agent wrote with its own connector
  section_notes(kept) -> list          short lines that open the provider's digest section

Every failure is raised as notes_common.ProviderError, so one provider failing never stops the
others. Each module's docstring names the sibling skill it was copied from.
"""
