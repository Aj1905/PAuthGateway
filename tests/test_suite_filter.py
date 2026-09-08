

def test_tool_name_words_count_toward_suite_score():
    """A prompt that only shares words with a tool NAME must still select the suite."""
    from gateway.providers.suite_filter import SuiteFilter, _tokens
    from pauth.codegen import ToolDoc
    from pauth.suites.base import SuiteSpec, ToolSpec

    assert {"git", "diff", "unstaged"} <= _tokens("git_diff_unstaged")

    def suite(name, tools):
        specs = {
            t: ToolSpec(name=t, params=["repo_path"], signer=name,
                        doc=ToolDoc(name=t, description="", parameters=[
                            {"name": "repo_path", "type": "string", "desc": ""}], returns="string"))
            for t in tools
        }
        return SuiteSpec(name=name, tools=specs, make_env=lambda: None,
                         tool_executor_factory=lambda env: (lambda tool, kw: None), tasks=[])

    suites = {
        "fs": suite("fs", ["read_text_file", "edit_file"]),
        "git": suite("git", ["git_status", "git_diff_unstaged"]),
    }
    result = SuiteFilter().filter("Show the unstaged diff in /tmp/repo.", suites)
    assert "git" in result.selected
    assert result.scores[0].name == "git"
