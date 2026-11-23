import json
from pathlib import Path

from factory.cli import main
from factory.generation import author

TASK = Path(__file__).parent / "fixtures/tasks/python/good_bulk_discount"


def test_generate_validates_model_draft(py_repo, tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "workdir"
    options = ["--workdir", str(workdir), "--backend", "local"]
    assert main(["load", "--repo", str(py_repo), *options]) == 0
    assert main(["inspect", *options]) == 0

    solution = (TASK / "after/toyshop/pricing.py").read_text()
    hidden = (TASK / "tests/tests/test_bulk_discount.py").read_text()
    requirements = (TASK / "requirements.md").read_text()
    prompts = []
    def reply(client, messages):
        assert client.model == "test/model"
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return json.dumps({"title": "Bulk discount", "requirements": requirements,
                               "after": {"toyshop/pricing.py": solution}})
        return json.dumps({"tests": {"tests/test_bulk_discount.py": hidden}})

    monkeypatch.setattr(author.ModelClient, "reply", reply)
    assert main(["generate", "--prompt", "Add bulk discounts", "--model", "test/model",
                 "--file", "toyshop/pricing.py", *options]) == 0
    assert len(prompts) == 2
    assert "Add bulk discounts" in prompts[0]
    assert requirements in prompts[1]
    assert solution not in prompts[1]
    assert "1/1 valid" in capsys.readouterr().out
    draft_path, = (workdir / "runs").glob("*/draft.json")
    assert json.loads(draft_path.read_text())["generated_by"]["model"] == "test/model"


def test_test_files_accepts_a_bare_path_map():
    files = {"tests/test_x.py": "def test_x(): pass\n"}
    assert author._test_files({"tests": files}) == files
    assert author._test_files(files) == files  # models often drop the {"tests": ...} wrapper
    assert author._test_files({"title": "x"}) is None


def test_ask_strips_any_code_fence():
    for fence in ("```json\n{\"a\": 1}\n```", "```\n{\"a\": 1}\n```", "{\"a\": 1}"):
        assert author._ask(lambda _: fence, "p") == {"a": 1}


def test_ask_finds_the_object_inside_prose():
    reply = "Here is the task:\n```json\n{\"a\": {\"b\": 1}}\n```\nHope this helps!"
    assert author._ask(lambda _: reply, "p") == {"a": {"b": 1}}
    assert author._ask(lambda _: 'Sure! {"a": 2} done', "p") == {"a": 2}
    import pytest
    from factory.errors import PackageError
    with pytest.raises(PackageError, match="it began: 'I cannot"):
        author._ask(lambda _: "I cannot do that", "p")
