"""The assignment's context reaches every target's background in the grading config."""

from app.assignment_context import HEADER
from app.feedback_assignment import read_assignment_config
from app.repository import InMemoryRepository


def configured():
    repo = InMemoryRepository()
    aid = repo.add_assignment("IMMEDIATE", platform_assignment_id="calc", name="Calculator")
    for order, identifier in enumerate(("calculator.py:add", "calculator.py:sub"), 1):
        repo.add_rubric(repo.add_grading_target(aid, identifier, order), criteria_text=f"Grade {identifier}.")
    repo.add_provider("GOOGLE", "gemini-2.5-flash", is_primary=True)
    return repo, aid


def test_config_without_context_keeps_the_rubric_text():
    repo, aid = configured()
    config = read_assignment_config(repo, str(aid))
    assert sorted(t.rubric.criteria_text for t in config.targets.values()) == [
        "Grade calculator.py:add.", "Grade calculator.py:sub.",
    ]


def test_config_appends_context_to_every_target():
    repo, aid = configured()
    repo.set_context_notes(aid, "Students have not learned exceptions yet.")
    repo.add_context_files(aid, [("handout.md", "Implement add and sub.")])

    config = read_assignment_config(repo, str(aid))

    for target in config.targets.values():
        text = target.rubric.criteria_text
        assert text.startswith(f"Grade {target.identifier}.\n\n--- {HEADER} ---")
        assert "Instructor notes:\nStudents have not learned exceptions yet." in text
        assert text.endswith("File: handout.md\nImplement add and sub.")


def test_config_accepts_context_at_the_limit():
    from app.assignment_context import MAX_FILES, MAX_TOTAL_CHARS

    repo, aid = configured()
    per_file = MAX_TOTAL_CHARS // MAX_FILES
    repo.add_context_files(aid, [(f"{'n' * 190}{i}.txt", "x" * per_file) for i in range(MAX_FILES)])

    config = read_assignment_config(repo, str(aid))

    assert all(len(t.rubric.criteria_text) > MAX_TOTAL_CHARS for t in config.targets.values())
