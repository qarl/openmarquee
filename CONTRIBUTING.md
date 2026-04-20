# Contributing to OpenMarquee

OpenMarquee is GPLv3 and welcomes contributions. These rules apply equally to maintainer commits and contributor pull requests.

## Project values

OpenMarquee is a self-contained, offline-capable, no-subscription, no-database, phone-first display controller. Contributions that pull against those constraints (cloud sync, account systems, mandatory internet access, heavyweight runtime dependencies) will be rejected on principle. See [`../DESIGN_BRIEF.md`](../DESIGN_BRIEF.md) and [`../SYSTEM_SPEC.md`](../SYSTEM_SPEC.md) for the full picture — they live in the workspace repo, one directory up from the code.

## Before you start

For anything non-trivial, open an issue first and we'll talk through the design. Saves you from writing code we can't merge.

## Tests

- Every new function, endpoint, or piece of behavior ships with tests.
- Every bug fix ships with a regression test — one that fails before the fix and passes after it.
- Don't commit with failing tests. The full suite must pass locally before you commit or open a pull request.
- There is no hard line-coverage threshold. Cover the *behavior*, not the line count. A test that exercises a realistic code path is worth more than a test written to move a number.

## Code review

- All changes are reviewed before landing, including changes by maintainers.
- Review happens against the staged diff with fresh eyes — the reviewer reads the change without the author's framing.
- Rubber-stamping is not review.

## Commits

- Prefer small, focused commits over large mixed-purpose ones.
- A commit message should explain *why* the change was made, not just *what* it does. The diff already shows what.

## Dev setup

Dev setup instructions will land alongside the first backend and UI code.

## License

All contributions are licensed under GPLv3. See [`LICENSE`](LICENSE).
