---
name: code_review
display_name: Code Review
description: Performs thorough code reviews with security and quality analysis
requires_binaries: [git]
user_invocable: true
auto_activate: false
---
## Instructions
When performing a code review:
1. Use `exec_command` to run `git diff` or `git diff --staged` to see changes
2. Read the modified files for full context
3. Analyze for: security vulnerabilities, bugs, performance issues, code style, best practices
4. Provide structured feedback with severity levels (critical, warning, suggestion)
5. Suggest specific fixes with code examples
