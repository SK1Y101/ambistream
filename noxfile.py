import nox

req_file = "requirements.txt"

code_directories = ["src"]
lint_directories = ["noxfile.py"] + code_directories
format_directories = lint_directories


@nox.session
def ui(session: nox.session) -> None:
    session.install("-r", req_file)
    session.install("uvicorn")
    session.run(
        "uvicorn",
        "src.backend:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        external=True,
    )


@nox.session
def orchestrator(session: nox.session) -> None:
    session.install("-r", req_file)
    session.run("python3", "-m", "src.orchestrator")


@nox.session
def player(session: nox.session) -> None:
    session.install("-r", req_file)
    session.run("python3", "-m", "src.player")


@nox.session
def dev(session: nox.session) -> None:
    session.install("-r", req_file)
    session.run("python3")


@nox.session(tags=["format", "lint"])
def black(session: nox.session) -> None:
    session.install("black")
    session.run("black", *format_directories)


@nox.session(tags=["format", "lint"])
def isort(session: nox.session) -> None:
    session.install("isort")
    session.run("isort", "--profile", "black", *format_directories)

    # sort requirements
    with open(req_file, "r") as f:
        reqs = f.readlines()
    sorted_reqs = [req for req in sorted(reqs) if req.strip("\n")]
    if reqs != sorted_reqs:
        with open(req_file, "w") as f:
            f.writelines(sorted_reqs)


@nox.session(tags=["lint"])
def lint(session: nox.session) -> None:
    """Lint all files."""
    session.install("flake8")
    session.run(
        "flake8",
        *lint_directories,
        "--max-line-length",
        "88",
        "--extend-ignore",
        "E203",
    )


@nox.session(tags=["lint"])
def mypy(session: nox.session) -> None:
    """Check python files for type violations."""
    mypy_directories = []
    for directory in code_directories:
        mypy_directories.extend(["-p", directory])

    session.install("mypy")
    session.install("-r", req_file)
    session.run("mypy", *mypy_directories, "--ignore-missing-imports")


@nox.session
def clean(session: nox.session) -> None:
    """Cleanup any created items."""
    import os
    import shutil

    def delete(directory):
        shutil.rmtree(directory, ignore_errors=True)

    def delete_file(file):
        try:
            os.remove(file)
        except FileNotFoundError:
            print(f"{file} doesn't seem to exist, skipping.")
        except Exception as e:
            print(f"Unknown error {e}")

    delete("src/__pycache__")
    delete("__pycache__")
    delete(".mypy_cache")
    delete(".pytest_cache")
    delete(".nox")
    delete_file("application.log")
