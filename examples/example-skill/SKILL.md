# Summarize Text

## Description

A simple OpenClaw skill that summarizes input text into key bullet points.
Demonstrates the minimum viable structure for a ClawHub-ready skill.

## Usage

```python
from summarize_skill import summarize

result = summarize("Your long text here...")
print(result)
```

## Requirements

- Python 3.10+

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_bullets` | `int` | `5` | Maximum number of bullet points |
| `min_length` | `int` | `10` | Minimum input length to summarize |

## Examples

### Example 1: Basic summarization

```python
text = "Python is a programming language. It is widely used for web development, data science, and automation. Python has a simple syntax that emphasizes readability."
result = summarize(text, max_bullets=3)
# Returns: ["Python is a programming language", "Used for web, data science, automation", "Simple readable syntax"]
```

## Limitations

- Input must be English text
- No support for structured data formats
- Maximum input length: 10,000 characters

## Author

Saturnday Example

## License

MIT
