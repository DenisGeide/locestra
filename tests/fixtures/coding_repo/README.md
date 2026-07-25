# Local Agent Coding Fixture

This is a disposable, synthetic repository used only for coding-agent tests.

`CODING_FIXTURE_FACT=violet-otter-731`

Run the deterministic standard-library test suite with:

```text
python -m unittest discover -s tests -v
```

The baseline includes calculator, exact-byte, security, and UI fixtures. The UI
contains a stable `data-testid` selector for Playwright evidence.
