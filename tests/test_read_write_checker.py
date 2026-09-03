"""Fix 68 — narrow assign-then-add tracking in read_without_write_surface.

Positive proofs (PASS): genuine persistence patterns must be detected.
Negative proofs (FAIL): bare construction without persistence must NOT mask the finding.

Replaces the prior overly broad heuristic (every bare constructor call counted as
write evidence), which masked real read-without-write defects.
"""

from __future__ import annotations

from pathlib import Path

from saturnday.review import _check_read_without_write_surface


def _check(tmp_path: Path, code: str) -> dict:
    (tmp_path / "app.py").write_text(code)
    return _check_read_without_write_surface(tmp_path, ["app.py"])


# ---------------------------------------------------------------------------
# Positive proofs — should PASS (write path detected)
# ---------------------------------------------------------------------------


def test_db_add_direct_constructor(tmp_path: Path) -> None:
    """db.add(Model(...)) — direct constructor inside .add() call."""
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def create(db): db.add(User(name="x"))
""")
    assert result["status"] == "PASS", result["findings"]


def test_session_add_direct_constructor(tmp_path: Path) -> None:
    """session.add(Model(...)) — original SQLAlchemy pattern."""
    result = _check(tmp_path, """
from sqlalchemy.orm import Session
class User: pass
def get(s: Session): return s.query(User).first()
def create(s: Session): s.add(User(name="x"))
""")
    assert result["status"] == "PASS", result["findings"]


def test_assign_then_db_add(tmp_path: Path) -> None:
    """user = Model(...); db.add(user) — narrow per-scope assign-then-add."""
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def create(db):
    user = User(name="x")
    db.add(user)
""")
    assert result["status"] == "PASS", result["findings"]


def test_assign_then_session_add(tmp_path: Path) -> None:
    """user = Model(...); session.add(user) — same pattern with named session."""
    result = _check(tmp_path, """
from sqlalchemy.orm import Session
class User: pass
def get(s: Session): return s.query(User).first()
def create(s: Session):
    user = User(name="x")
    s.add(user)
""")
    assert result["status"] == "PASS", result["findings"]


def test_assign_then_create(tmp_path: Path) -> None:
    """obj = Model(...); repo.create(obj) — .create() is a write method."""
    result = _check(tmp_path, """
class Order: pass
def list_all(repo): return repo.query(Order).all()
def make(repo):
    order = Order(qty=1)
    repo.create(order)
""")
    assert result["status"] == "PASS", result["findings"]


def test_assign_then_save(tmp_path: Path) -> None:
    """obj = Model(...); repo.save(obj) — .save() is a write method."""
    result = _check(tmp_path, """
class Order: pass
def list_all(repo): return repo.query(Order).all()
def persist(repo):
    order = Order(qty=2)
    repo.save(order)
""")
    assert result["status"] == "PASS", result["findings"]


def test_django_objects_create(tmp_path: Path) -> None:
    """Model.objects.create(...) — Django write."""
    result = _check(tmp_path, """
class User:
    class objects:
        @staticmethod
        def filter(**kw): pass
        @staticmethod
        def create(**kw): pass
def get(): return User.objects.filter(active=True)
def make(): User.objects.create(name="x")
""")
    assert result["status"] == "PASS", result["findings"]


def test_annotated_assign_then_add(tmp_path: Path) -> None:
    """user: User = User(...); db.add(user) — annotated assignment is also tracked."""
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def create(db):
    user: User = User(name="x")
    db.add(user)
""")
    assert result["status"] == "PASS", result["findings"]


# ---------------------------------------------------------------------------
# Negative proofs — should FAIL (no genuine persistence)
# ---------------------------------------------------------------------------


def test_bare_constructor_no_persistence_fails(tmp_path: Path) -> None:
    """Bare User(...) call with no .add/.create/.save anywhere — must FAIL.

    This is the exact case the prior overly broad heuristic masked.
    """
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def seed(): User(name="seed")
""")
    assert result["status"] == "FAIL", "bare constructor must not satisfy write evidence"
    assert result["findings"][0]["kind"] == "read_without_write_path"
    assert "User" in result["findings"][0]["detail"]


def test_constructor_for_temp_transformation_fails(tmp_path: Path) -> None:
    """user = User(...) used only for transient transformation — must FAIL.

    Variable is constructed and read but never passed to a write method.
    """
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def transform(name):
    user = User(name=name)
    return user.name.upper()
""")
    assert result["status"] == "FAIL", "transient construction is not persistence"
    assert result["findings"][0]["kind"] == "read_without_write_path"


def test_constructor_in_helper_no_persistence_fails(tmp_path: Path) -> None:
    """Helper code that constructs but never persists — must FAIL.

    Mirrors a common mistake: factory function returns a Model object but
    no caller in the corpus persists it.
    """
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def make_user(name):
    return User(name=name)
""")
    assert result["status"] == "FAIL", "returning a constructed model is not persistence"
    assert result["findings"][0]["kind"] == "read_without_write_path"


def test_genuine_read_only_fails(tmp_path: Path) -> None:
    """Read with no constructor anywhere — must FAIL (control case)."""
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
""")
    assert result["status"] == "FAIL"
    assert result["findings"][0]["kind"] == "read_without_write_path"


def test_assign_in_one_function_add_in_another_does_not_match(tmp_path: Path) -> None:
    """Assignment in function A and unrelated .add(var) in function B with the
    same variable name should NOT be cross-matched — scoping is per-function.

    This proves the tracking is bounded to a single scope, not file-wide.
    Function A constructs a User but never persists it; function B calls
    db.add(user) where `user` is an unrelated parameter (no constructor in
    that scope), so write_entities for User must remain empty.
    """
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def transform():
    user = User(name="x")
    return user
def persist(db, user):
    db.add(user)
""")
    assert result["status"] == "FAIL", (
        "function-scope tracking must not credit User as written when the "
        "constructor and add() live in different scopes with no actual data flow"
    )
    assert result["findings"][0]["kind"] == "read_without_write_path"


def test_construct_then_add_different_var_does_not_match(tmp_path: Path) -> None:
    """user = User(...) but db.add(other) — different variable, must FAIL."""
    result = _check(tmp_path, """
class User: pass
def get(db): return db.query(User).first()
def create(db, other):
    user = User(name="x")
    db.add(other)
""")
    assert result["status"] == "FAIL", "add(other) does not persist user"
    assert result["findings"][0]["kind"] == "read_without_write_path"
