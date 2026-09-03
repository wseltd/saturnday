# Example OpenClaw Skill

This is a reference implementation showing the minimum viable structure for
a ClawHub-ready OpenClaw skill.

## Quick Start

```bash
# Install Saturnday
pip install saturnday

# Guard scan (canonical command)
saturnday scan --skill .

# Publish readiness check
saturnday publish-preflight --skill .

# Run tests
pytest test_summarize.py
```

## Command Reference

- `saturnday` is the unified CLI for Guard, Run, and Repair
- `saturnday scan` is the canonical scan command for builders

## Structure

```
SKILL.md                  # Required: skill description and metadata
summarize_skill.py        # Implementation
test_summarize.py         # Tests (verify_cmd: pytest test_summarize.py)
saturnday-policy.yaml     # Saturnday policy configuration
README.md                 # This file
```
