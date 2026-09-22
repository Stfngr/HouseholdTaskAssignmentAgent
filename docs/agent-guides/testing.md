# Testing

Use standard-library `unittest`; project has no pytest runner or static typecheck command.

```bash
python3 -m unittest discover -s tests -v
```

- Add or update tests in `tests/test_<module>.py` with changed behavior.
- Test pure planning and state transitions directly; mock network adapters and filesystem failure paths where needed.
- Inject clocks and `random.Random(seed)` for time-sensitive or allocation behavior. Do not depend on wall-clock time or unseeded randomness.
- Cover relevant calendar boundaries, retries, restart/idempotency, malformed persisted data, and delivery failure paths.
- Run `python3 main.py --check-config` after configuration-loading changes. It validates JSON without loading credentials, accessing state, or using network.
- `--preview YYYY-MM-DD --seed N` is advisory planning only. It must remain free of runtime service side effects.
