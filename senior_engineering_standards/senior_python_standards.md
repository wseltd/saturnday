# Senior Python Backend Code Standards

This document is the reference standard for the Llama quality pass. Before improving any source file, read this document and apply these patterns.

## 1. Exception handling as architecture

- Define a base exception per service/module with rich subclasses carrying context attributes.
- Always use exception chaining with `raise X from Y` when mapping third-party exceptions.
- Never catch bare `Exception` unless creating an explicit isolation point that re-raises or logs.
- Minimize try/except block scope — the larger the try block, the more real errors you hide.
- Always log before re-raising with `logger.exception()` inside except blocks.
- Never use `assert` for validation — it can be disabled with Python's `-O` flag.
- Map exceptions at API boundaries to consistent HTTP status codes.

## 2. Production observability

- Use `logging.getLogger(__name__)` at module level. Use `%s` placeholder formatting, NOT f-strings.
- Log level discipline: DEBUG for variable values (dev only). INFO for entry/exit of key operations. WARNING for slow responses, deprecation usage. ERROR for failed connections, API timeouts. CRITICAL for cannot-start conditions.
- Never log PII, secrets, API keys, or large payloads at INFO level.
- Add `logger.info()` at entry of every public function with key parameters.
- Add `logger.error()` in every except block.

## 3. Beyond "it works"

- Every function doing I/O, database calls, HTTP requests, or parsing MUST have try/except around the operation.
- Catch SPECIFIC exceptions, never bare `except:`.
- Use `raise X from e` when converting exceptions to preserve traceback chain.
- A non-critical dependency failure should never return a 500.

## 4. Python-specific patterns

- `@contextmanager` for all resource management — DB sessions, file handles, locks, timing blocks.
- `@dataclass(frozen=True, slots=True)` for internal domain models. Pydantic `BaseModel` at external boundaries only.
- `typing.Protocol` for interfaces, not ABC.
- `@lru_cache(maxsize=128)` for expensive computations.
- `@wraps` on every decorator to preserve `__name__`, `__doc__`, and `__annotations__`.
- `StrEnum` with `auto()` for all string-based constants (Python 3.11+).
- Never use mutable default arguments. Use `def foo(items=None)` with `items = items or []`.

## 5. Code review standards

- Every public function has a Google-style docstring with Args/Returns/Raises sections.
- Summary line is one physical line, terminated by a period.
- Import ordering: (1) `__future__`, (2) standard library, (3) third-party, (4) local — sorted within each group.
- No wildcard imports. No relative imports.
- Type annotations on all public APIs. Use `X | None` instead of `Optional[X]`.
- Functions under ~40 lines. Single responsibility per function and class.
- `if foo is None:` not `if not foo:` for None checks. `if items:` not `if len(items) > 0:` for emptiness.

## 6. Production hardening

- Input validation at boundaries only. Validate inputs in the first few lines of every public function.
- Raise ValueError with descriptive messages for invalid inputs.
- Replace all magic numbers with named constants at module level with explanatory comments.
- Any user-provided string rendered in HTML MUST use `html.escape()`.
- Parameterized queries only — never f-strings in SQL.
- `SecretStr` for all secrets to prevent accidental serialization.

## 7. Docstring format (Google style)

```python
def process_invoice(doc_text: str, doc_type: str) -> dict:
    """Extract and validate fields from an invoice document.

    Args:
        doc_text: Raw text content of the document.
        doc_type: Document type identifier (e.g., "invoice", "claim").

    Returns:
        Dictionary mapping field names to extracted values with confidence scores.

    Raises:
        ValueError: If doc_type is not a supported document type.
    """
```

## 8. Logging format

```python
import logging

logger = logging.getLogger(__name__)

def process_document(doc_id: str, doc_type: str) -> dict:
    """Process a document and return extracted fields."""
    logger.info("Processing document doc_id=%s type=%s", doc_id, doc_type)
    try:
        result = _extract_fields(doc_id, doc_type)
        logger.info("Processed document doc_id=%s fields=%d", doc_id, len(result))
        return result
    except ExtractionError as e:
        logger.error("Extraction failed doc_id=%s: %s", doc_id, e)
        raise
```

## 9. Input validation format

```python
def score_confidence(value: str, field_type: str) -> float:
    """Score extraction confidence for a field value.

    Args:
        value: The extracted field value.
        field_type: Type of field (e.g., "date", "amount", "name").

    Returns:
        Confidence score between 0.0 and 1.0.

    Raises:
        ValueError: If value is empty or field_type is not supported.
    """
    if not value or not isinstance(value, str):
        raise ValueError("value must be a non-empty string")
    if field_type not in SUPPORTED_FIELD_TYPES:
        raise ValueError(f"Unsupported field_type: {field_type}")
```

## 10. Named constants format

```python
# Minimum confidence to auto-accept a field without human review
CONFIDENCE_THRESHOLD = 0.85

# Maximum number of retry attempts for external API calls
MAX_RETRIES = 3

# Fields that require human review regardless of confidence
MANDATORY_REVIEW_FIELDS = frozenset({"total_amount", "account_number"})
```
