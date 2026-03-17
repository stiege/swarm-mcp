Structured code review output covering security, correctness, and style.

A `code-review` artifact is the output of a reviewer agent that has read source code and
evaluated it across multiple dimensions. It is not a summary of what the code does — it is
a structured evaluation of its quality, with specific, actionable findings.

## Structure

- **verdict**: The overall review decision. Must be exactly one of:
  - `approve` — code is acceptable as-is
  - `approve-with-comments` — code can merge but comments should be addressed
  - `request-changes` — code must not merge until issues are resolved

- **summary**: One paragraph summarising the overall quality and the reasoning behind the
  verdict. Must not simply restate the issue list.

- **security_issues**: A list of security findings. Each entry must include:
  - `severity`: one of `critical`, `high`, `medium`, `low`, or `info`
  - `file`: the file path (relative to repo root)
  - `line`: line number or range (e.g. `42` or `38–45`)
  - `description`: what the issue is, why it is a security risk, and how to fix it
  - `cwe`: the CWE identifier if applicable (e.g. `CWE-89`)

  If no security issues were found, this field must be an empty list with a note explaining
  why (e.g. "No security-sensitive code paths found in this changeset").

- **correctness_issues**: A list of bugs, logic errors, or broken edge-case handling.
  Each entry must include `file`, `line`, `description`, and `severity` (`critical`, `major`,
  `minor`).

- **style_issues**: A list of style, naming, or maintainability concerns. Each entry must
  include `file`, `line`, and `description`. Severity is implicitly `minor`.

- **positive_observations**: At least one thing done well in the code. This is not optional
  flattery — it helps authors learn what patterns to repeat.

## Example entry

```
verdict: request-changes

summary: |
  The new authentication endpoint has a critical SQL injection vulnerability and
  does not rate-limit login attempts, making it unsuitable for production. The
  overall structure is clean and the input parsing logic is well-factored.

security_issues:
  - severity: critical
    file: src/auth/login.py
    line: 47
    description: |
      User input is interpolated directly into the SQL query on line 47:
        `query = f"SELECT * FROM users WHERE email = '{email}'"`.
      Use parameterised queries: `cursor.execute("SELECT ... WHERE email = %s", (email,))`.
    cwe: CWE-89

  - severity: high
    file: src/auth/login.py
    line: 12
    description: |
      No rate limiting on the /login endpoint. An attacker can brute-force credentials
      without throttling. Add a per-IP rate limiter (e.g. Flask-Limiter) with a limit
      of 10 attempts per minute.
    cwe: CWE-307

correctness_issues:
  - severity: major
    file: src/auth/login.py
    line: 63
    description: |
      `check_password()` returns None when the user is not found, but the caller on
      line 63 treats None as falsy without distinguishing "user not found" from
      "wrong password". This leaks user existence via different error messages.

style_issues:
  - file: src/auth/login.py
    line: 8
    description: Unused import `datetime` should be removed.

positive_observations:
  - Input sanitisation in `parse_login_request()` (lines 20–35) is thorough and
    well-commented. The separation of parsing from business logic is a good pattern.
```

## Validity Criteria

- `verdict` must be exactly one of: `approve`, `approve-with-comments`, `request-changes`
- `summary` must be at least 2 sentences and must not simply list the issue titles
- Every `critical` security issue must include a specific, concrete fix in its `description`
  (not "validate your inputs" — the actual code change needed)
- `security_issues` must be present; an empty list is valid only if accompanied by a
  non-empty explanatory note
- Every issue entry (security, correctness, or style) must include `file` and `line`
- `positive_observations` must contain at least one entry — a review with no positive
  observations is not a complete review
- If `verdict` is `approve`, `security_issues` and `correctness_issues` must both be
  empty or contain only `low`/`info`/`minor` severity items
- If `verdict` is `request-changes`, at least one `critical` or `high` severity issue
  (security or correctness) must be present
