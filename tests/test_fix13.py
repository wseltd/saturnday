"""Fix 13: Generated-app operator truth surfaces.

Tests for four checks:
  13.1 _check_read_without_write_surface  — ORM read with no write path
  13.2 _check_frozen_status_fields        — status Column never dynamically updated
  13.3 _check_write_read_schema_asymmetry — within-function dict key mismatch
  13.4 _check_hollow_resumed_flow         — resume functions returning only ack keys

Coverage requirements:
  1.  ORM read surface with no write in corpus → FAIL
  2.  ORM read AND write present → PASS
  3.  Django-style read with no write → FAIL
  4.  Frozen status Column (no dynamic assign) → FAIL
  5.  Status Column with dynamic assign → PASS
  6.  Within-function dict key mismatch → FAIL
  7.  Within-function dict key access only from written keys → PASS
  8.  Hollow resume function (only ack keys) → FAIL
  9.  Resume function with substantive return keys → PASS
  10. Test files are excluded from all checks
  11. Empty input → PASS with correct shape
"""

from __future__ import annotations

from pathlib import Path

from saturnday.review import (
    _check_frozen_status_fields,
    _check_hollow_resumed_flow,
    _check_read_without_write_surface,
    _check_write_read_schema_asymmetry,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# Requirement 1 & 3: ORM read with no write → FAIL
# ---------------------------------------------------------------------------


def test_rww_sqlalchemy_read_no_write(tmp_path: Path) -> None:
    """session.query(Order) with no session.add(Order(...)) must FAIL."""
    code = (
        "from sqlalchemy.orm import Session\n"
        "\n"
        "def get_orders(session: Session):\n"
        "    return session.query(Order).all()\n"
    )
    f = _write(tmp_path, "src/orders.py", code)
    result = _check_read_without_write_surface(tmp_path, [f])
    assert result["name"] == "read_without_write_surface"
    assert result["status"] == "FAIL"
    assert result["severity"] == "warning"
    assert any(d["kind"] == "read_without_write_path" for d in result["findings"])
    assert any("Order" in d["detail"] for d in result["findings"])
    assert isinstance(result["files_checked"], list)


def test_rww_django_read_no_write(tmp_path: Path) -> None:
    """Django Model.objects.filter() with no Model.objects.create() must FAIL."""
    code = (
        "def get_active_users():\n"
        "    return User.objects.filter(active=True)\n"
    )
    f = _write(tmp_path, "src/users.py", code)
    result = _check_read_without_write_surface(tmp_path, [f])
    assert result["status"] == "FAIL"
    assert any("User" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Requirement 2: ORM read AND write present → PASS
# ---------------------------------------------------------------------------


def test_rww_read_and_write_present(tmp_path: Path) -> None:
    """Both session.query(Invoice) and session.add(Invoice(...)) present → PASS."""
    code = (
        "from sqlalchemy.orm import Session\n"
        "\n"
        "def save_invoice(session: Session, data):\n"
        "    session.add(Invoice(amount=data['amount']))\n"
        "\n"
        "def load_invoice(session: Session, inv_id):\n"
        "    return session.query(Invoice).filter_by(id=inv_id).first()\n"
    )
    f = _write(tmp_path, "src/invoices.py", code)
    result = _check_read_without_write_surface(tmp_path, [f])
    assert result["status"] == "PASS"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Requirement 4: Frozen status Column → FAIL
# ---------------------------------------------------------------------------


def test_fsf_frozen_status_column(tmp_path: Path) -> None:
    """status = Column(...) with no dynamic assign must FAIL."""
    code = (
        "from sqlalchemy import Column, String\n"
        "from sqlalchemy.orm import DeclarativeBase\n"
        "\n"
        "class Base(DeclarativeBase):\n"
        "    pass\n"
        "\n"
        "class Job(Base):\n"
        "    __tablename__ = 'jobs'\n"
        "    status = Column(String, default='pending')\n"
    )
    f = _write(tmp_path, "src/models.py", code)
    result = _check_frozen_status_fields(tmp_path, [f])
    assert result["name"] == "frozen_status_fields"
    assert result["status"] == "FAIL"
    assert result["severity"] == "warning"
    assert any(d["kind"] == "frozen_status_field" for d in result["findings"])
    assert any("status" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Requirement 5: Status Column with dynamic assign → PASS
# ---------------------------------------------------------------------------


def test_fsf_status_column_updated_dynamically(tmp_path: Path) -> None:
    """status Column with obj.status = new_status (variable RHS) must PASS."""
    code = (
        "from sqlalchemy import Column, String\n"
        "from sqlalchemy.orm import DeclarativeBase\n"
        "\n"
        "class Base(DeclarativeBase):\n"
        "    pass\n"
        "\n"
        "class Job(Base):\n"
        "    __tablename__ = 'jobs'\n"
        "    status = Column(String, default='pending')\n"
        "\n"
        "def advance_job(job, new_status):\n"
        "    job.status = new_status\n"
    )
    f = _write(tmp_path, "src/models.py", code)
    result = _check_frozen_status_fields(tmp_path, [f])
    assert result["status"] == "PASS"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Requirement 6: Within-function dict key mismatch → FAIL
# ---------------------------------------------------------------------------


def test_wrs_key_mismatch_in_function(tmp_path: Path) -> None:
    """Dict created with {a, b} but c is accessed → FAIL."""
    code = (
        "def build_response(data):\n"
        "    result = {'name': data['name'], 'score': data['score']}\n"
        "    return result['total']  # 'total' was never written\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_write_read_schema_asymmetry(tmp_path, [f])
    assert result["name"] == "write_read_schema_asymmetry"
    assert result["status"] == "FAIL"
    assert result["severity"] == "warning"
    assert any(d["kind"] == "write_read_key_mismatch" for d in result["findings"])
    assert any("total" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Requirement 7: Dict key access only from written keys → PASS
# ---------------------------------------------------------------------------


def test_wrs_all_keys_present(tmp_path: Path) -> None:
    """Dict created with {name, score} and only those keys accessed → PASS."""
    code = (
        "def build_response(data):\n"
        "    result = {'name': data['name'], 'score': data['score']}\n"
        "    label = result['name']\n"
        "    points = result['score']\n"
        "    return label, points\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_write_read_schema_asymmetry(tmp_path, [f])
    assert result["status"] == "PASS"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Requirement 8: Hollow resume function → FAIL
# ---------------------------------------------------------------------------


def test_hrf_hollow_resume_function(tmp_path: Path) -> None:
    """Resume function returning only {status, message} must FAIL."""
    code = (
        "def resume_job(job_id):\n"
        "    job = load_job(job_id)\n"
        "    start_worker(job)\n"
        "    return {'status': 'resumed', 'message': 'job restarted'}\n"
    )
    f = _write(tmp_path, "src/runner.py", code)
    result = _check_hollow_resumed_flow(tmp_path, [f])
    assert result["name"] == "hollow_resumed_flow"
    assert result["status"] == "FAIL"
    assert result["severity"] == "warning"
    assert any(d["kind"] == "hollow_resumed_response" for d in result["findings"])
    assert any("resume_job" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Requirement 9: Resume function with substantive keys → PASS
# ---------------------------------------------------------------------------


def test_hrf_resume_returns_substantive_data(tmp_path: Path) -> None:
    """Resume function returning {status, job_id, result, steps_completed} must PASS."""
    code = (
        "def resume_pipeline(pipeline_id):\n"
        "    state = load_state(pipeline_id)\n"
        "    output = continue_from(state)\n"
        "    return {\n"
        "        'status': 'resumed',\n"
        "        'pipeline_id': pipeline_id,\n"
        "        'result': output,\n"
        "        'steps_completed': state['steps'],\n"
        "    }\n"
    )
    f = _write(tmp_path, "src/pipeline.py", code)
    result = _check_hollow_resumed_flow(tmp_path, [f])
    assert result["status"] == "PASS"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Requirement 10: Test files excluded from all checks
# ---------------------------------------------------------------------------


def test_all_checks_exclude_test_files(tmp_path: Path) -> None:
    """Test files must not produce findings in any Fix 13 check."""
    code = (
        "def test_resume_job():\n"
        "    result = resume_job(1)\n"
        "    assert result['status'] == 'resumed'\n"
        "\n"
        "def resume_job(job_id):\n"
        "    return {'status': 'resumed', 'ok': True}\n"
        "\n"
        "def get_jobs(session):\n"
        "    return session.query(Job).all()\n"
        "\n"
        "class Job:\n"
        "    status = Column(String, default='pending')\n"
    )
    f = _write(tmp_path, "tests/test_jobs.py", code)
    assert _check_read_without_write_surface(tmp_path, [f])["status"] == "PASS"
    assert _check_frozen_status_fields(tmp_path, [f])["status"] == "PASS"
    assert _check_write_read_schema_asymmetry(tmp_path, [f])["status"] == "PASS"
    assert _check_hollow_resumed_flow(tmp_path, [f])["status"] == "PASS"


# ---------------------------------------------------------------------------
# Requirement 11: Empty input → PASS with correct shape
# ---------------------------------------------------------------------------


def test_all_checks_empty_input(tmp_path: Path) -> None:
    """All Fix 13 checks return correct shape with PASS when called with no files."""
    for fn, name in (
        (_check_read_without_write_surface, "read_without_write_surface"),
        (_check_frozen_status_fields, "frozen_status_fields"),
        (_check_write_read_schema_asymmetry, "write_read_schema_asymmetry"),
        (_check_hollow_resumed_flow, "hollow_resumed_flow"),
    ):
        result = fn(tmp_path, [])
        assert result["name"] == name
        assert result["status"] == "PASS"
        assert result["severity"] == "warning"
        assert result["findings"] == []
        assert result["files_checked"] == []
