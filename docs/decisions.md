# Decisions, assumptions and limitations

Recorded as the build goes (design doc section 20). Newest last within each section.

## Decisions

- **D1 Python 3.11+, uv for dev.** Runtime dependency: pydantic v2 (MIT). Dev: pytest (MIT).
  CLI uses `argparse` from the standard library, so no extra dependency.
- **D2 LLM provider not chosen yet.** `LLMClient` stays provider-agnostic; only a mock adapter
  exists until hackathon credits are known (M6).
- **D3 EnvConfig has extra fields** beyond section 6: `framework`, `report_format` and
  `report_path`. `test_command` is a shell template with `{report}` and `{select}` placeholders.
  Needed so one runner can drive pytest, node:test, Jest, Go, Maven and Cargo.
- **D4 Fixture tasks** (one good task and one bad task per reject code) are added with the
  validation runner (M4), because their on-disk format is defined there.
