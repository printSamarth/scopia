from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scopia.vcs.git import Repo

from .fixtures import php_repo, py_repo


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)


def _make_repo(tmp_path_factory, name: str, base: dict[str, str], head: dict[str, str]) -> Repo:
    root = tmp_path_factory.mktemp(name)
    _git(root, "init", "-q")
    _commit(root, base, "base")
    for stale in set(base) - set(head):
        (root / stale).unlink()
    _commit(root, head, "head")
    return Repo(root)


@pytest.fixture(scope="session")
def py_fixture(tmp_path_factory) -> Repo:
    return _make_repo(tmp_path_factory, "pyrepo", py_repo.base_files(), py_repo.head_files())


@pytest.fixture(scope="session")
def php_fixture(tmp_path_factory) -> Repo:
    return _make_repo(tmp_path_factory, "phprepo", php_repo.base_files(), php_repo.head_files())


@pytest.fixture(scope="session")
def mixed_fixture(tmp_path_factory) -> Repo:
    """Python and PHP sharing symbol names — they must never be linked to each other."""
    base = {
        "tool.py": "def load(x):\n    return connect(x)\n\n\ndef connect(x):\n    return x\n",
        "Db.php": "<?php\n\nclass Db\n{\n    public function connect($dsn)\n    {\n"
        "        return $dsn;\n    }\n\n    public function load($k)\n    {\n"
        "        return $this->connect($k);\n    }\n}\n",
    }
    head = {
        "tool.py": "def load(x):\n    return connect(x * 2)\n\n\ndef connect(x):\n    return x\n",
        "Db.php": "<?php\n\nclass Db\n{\n    public function connect($dsn)\n    {\n"
        "        return $dsn;\n    }\n\n    public function load($k)\n    {\n"
        "        return $this->connect($k . '!');\n    }\n}\n",
    }
    return _make_repo(tmp_path_factory, "mixedrepo", base, head)


@pytest.fixture(scope="session")
def php_scope_fixture(tmp_path_factory) -> Repo:
    """Four services each defining `show`, called the four ways PHP names a class.

    Without using the named class, every one of these calls fans out across all four.
    """
    services = {
        f"src/{name}Service.php": (
            f"<?php\n\nclass {name}Service\n{{\n"
            "    public function show($id)\n    {\n"
            f"        return ['{name}', $id];\n    }}\n}}\n"
        )
        for name in ("Order", "User", "Product", "Outlet")
    }
    base = dict(services)
    base["src/BaseRepo.php"] = (
        "<?php\n\nclass BaseRepo\n{\n    public function find($id)\n    {\n"
        "        return $id;\n    }\n}\n"
    )
    base["src/OrderRepo.php"] = "<?php\n\nclass OrderRepo extends BaseRepo\n{\n}\n"
    base["src/Controller.php"] = (
        "<?php\n\nclass Controller\n{\n"
        "    private OrderService $svc;\n\n"
        "    public function show($id)\n    {\n"
        "        return $id;\n    }\n}\n"
    )

    head = dict(base)
    head["src/Controller.php"] = (
        "<?php\n\nclass Controller\n{\n"
        "    private OrderService $svc;\n\n"
        "    public function show($id)\n    {\n"
        "        app(UserService::class)->show($id);\n"       # ::class in a call
        "        (new ProductService())->show($id);\n"         # new X()
        "        OutletService::show($id);\n"                  # static call
        "        $this->svc->show($id);\n"                     # typed property
        "        app(OrderRepo::class)->find($id);\n"          # inherited method
        "        return $id;\n    }\n}\n"
    )
    return _make_repo(tmp_path_factory, "phpscope", base, head)


@pytest.fixture(scope="session")
def plumbing_fixture(tmp_path_factory) -> Repo:
    """One changed handler that calls a logger used by twenty other functions."""
    base = {
        "app/log.py": "def audit(event):\n    return event\n",
        "app/handler.py": "from app.log import audit\n\n\ndef handle(x):\n    return x\n",
    }
    # Twenty unrelated callers give `audit` a fan-in well past the utility threshold.
    for i in range(20):
        base[f"app/mod{i}.py"] = (
            f"from app.log import audit\n\n\ndef task_{i}(x):\n    audit('t{i}')\n    return x\n"
        )
    head = dict(base)
    head["app/handler.py"] = (
        "from app.log import audit\n\n\ndef handle(x):\n    audit('handled')\n    return x * 2\n"
    )
    return _make_repo(tmp_path_factory, "plumbing", base, head)


@pytest.fixture(scope="session")
def feedback_fixture(tmp_path_factory) -> Repo:
    """A scheduled job reaching a new pipeline through an aliased import.

    Mirrors the reported break: `process_configs -> pipeline_v3 -> get_attendance_data`
    appeared as three disconnected chains because the alias hid the real name. Also
    carries an edit to pre-existing behaviour and an unparseable file.
    """
    base = {
        "data.py": "def get_attendance_data():\n    return sql_for('all')\n",
        "sql.py": "def sql_for(kind):\n    return 'SELECT * FROM t'\n",
        "jobs.py": "def process_configs():\n    return None\n",
        "tools/config.json": '{\n  "a": 1\n}\n',
    }
    head = dict(base)
    head["pipeline.py"] = (
        "from data import get_attendance_data\n\n\n"
        "def run():\n    return get_attendance_data()\n"
    )
    # The alias is the whole point: the call says `pipeline_v3`, the def says `run`.
    head["jobs.py"] = (
        "from pipeline import run as pipeline_v3\n\n\n"
        "def process_configs():\n    return pipeline_v3()\n"
    )
    # An edit to behaviour that already existed.
    head["sql.py"] = "def sql_for(kind):\n    return 'SELECT * FROM t WHERE is_active = 1'\n"
    head["tools/config.json"] = '{\n  "a": 1,\n  "b": 2\n}\n'
    return _make_repo(tmp_path_factory, "feedback", base, head)


@pytest.fixture(scope="session")
def untracked_fixture(tmp_path_factory) -> Repo:
    """A modified tracked file plus a brand-new file never handed to git."""
    root = tmp_path_factory.mktemp("untracked")
    _git(root, "init", "-q")
    _commit(root, {"old.py": "def existing():\n    return 1\n"}, "base")
    (root / "old.py").write_text("def existing():\n    return 2\n", "utf-8")
    # Deliberately never `git add`-ed — invisible to `git diff HEAD`.
    (root / "feature.py").write_text(
        "from old import existing\n\n\ndef brand_new_feature():\n    return existing()\n",
        "utf-8",
    )
    (root / "ignored.log").write_text("noise\n", "utf-8")
    (root / ".gitignore").write_text("*.log\n", "utf-8")
    return Repo(root)


@pytest.fixture(scope="session")
def tiny_fixture(tmp_path_factory) -> Repo:
    """A two-line change in a single file — must fall below the gate."""
    base = {"only.py": "def solo():\n    return 1\n"}
    head = {"only.py": "def solo():\n    return 2\n"}
    return _make_repo(tmp_path_factory, "tinyrepo", base, head)
