# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Style

- Comments are used to explain implementation when it's unclear. Do NOT add comments that are
  self-evident from the code, or that explain the code's history (that's what commit history is
  for). No comments like `# Removed db_context`.

## Development Guidelines

- NEVER assume that linter errors are false positives unless you have very clear evidence proving
  it. Linters in this project are generally set up to be correct. If there is a false positive you
  MUST document your evidence for that alongside the ignore comment or wherever you disable it.

\[Rest of the existing content remains unchanged\]
