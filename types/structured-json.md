A well-formed JSON object with all required fields populated and no null values.

`structured-json` is a meta-type for any output that must be valid, complete JSON. Use it
as a base type when you need a lightweight guarantee that an agent produced parseable JSON
with no missing or null required fields — before you apply a more specific type on top.

This type is intentionally general. Pair it with a domain-specific type (e.g.
`[analysis-report]`, `[code-review]`) by referencing `[structured-json]` in that type's
validity criteria, or use it standalone when the schema is too simple to warrant its own
type file.

## Structure

The output must be a JSON object (not an array, not a scalar) with the following properties:

- **Valid JSON syntax**: The output can be parsed by `json.loads()` without error. No
  trailing commas, no comments, no single-quoted strings, no unquoted keys.

- **Object at root level**: The top-level value is a JSON object `{}`, not an array `[]`
  or a primitive.

- **All required fields populated**: Every field that the producing agent was asked to
  populate is present in the output. "Required" is determined by the task prompt or the
  enclosing type definition.

- **No null values for required fields**: Required fields must have a meaningful value —
  not `null`, not an empty string `""` where a value was expected, not `0` where a count
  was expected.

- **Consistent types**: Field types are consistent with their declared purpose. A field
  described as "a list of items" must be an array, not a string. A field described as
  "a count" must be a number, not a string like `"three"`.

## What counts as invalid

- Output that is not JSON at all (plain prose, code blocks wrapping JSON, etc.)
- Output that starts with explanation text before the JSON object
- A JSON array `[...]` at root level when an object was required
- Any required field set to `null`
- Any required field set to an empty string or empty array when a value was clearly expected
- Truncated JSON (object not closed, array not closed)

## Example of VALID output

```json
{
  "status": "complete",
  "items_processed": 42,
  "errors": [],
  "summary": "All items processed successfully with no failures."
}
```

## Example of INVALID output

```json
{
  "status": "complete",
  "items_processed": null,
  "errors": null,
  "summary": ""
}
```

(Two required fields are null; one required field is an empty string where a value was expected.)

## Validity Criteria

- Output must parse as valid JSON without error
- Root value must be a JSON object `{}`
- No required field may be `null`
- No required field may be an empty string `""` where a substantive value was expected
- No required field may be missing from the output entirely
- Field types must match their declared purpose (arrays are arrays, numbers are numbers)
- Output must not contain any text before or after the JSON object (no preamble, no
  trailing explanation, no Markdown code fences wrapping the JSON)
