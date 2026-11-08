Read the [readme](/README.md) for an overview of the repo.

# Development

## git branches

Do not commit to the `main` branch. Use branches, named as follows:

- Issue number followed by `-` (if addressing an Issue)
- a slug, ie a short lowercase dash-separated representation of the feature/bug name

Examples:
- `add-user-profile-page`
- `fix-widget-deletion`
- `improve-signup-experience`

## git commits

Do not commit to the `main` branch.

Make commits incremental and keep them small.

Use one commit for:
- a new widget is added and linked to from elsewhere
- a function is renamed across multiple files
- a function is added along with its unit tests

If in doubt, split changes into more smaller commits rather than fewer larger commits.

## git commit messages

Keep commit messages to one line only. No paragraphs. No emoji. No bullet point lists. No list of files changed.

Add a bracketed Issue number if addressing an Issue. e.g. "Add widget (#123)".

## git push

Before running `git push`, run local lint and test checks.
