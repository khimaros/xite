# HACKING

whenever possible, prefer to delete and simplify code.
remove dead code and unnecessary or obvious comments.

maintain existing code style such as variable naming,
comment formatting, output formatting, whitespace, and quoting.

code comments and command line output should be all lowercase,
though CAPS can be used for acronyms or for emphasis.

in general, comments should focus on "why" instead of "what"
do not add comments explaining what each expression is doing and
do not add "changelog" style comments describing a code change.

keep dependencies to a bare minimum.

keep the code "DRY" and organized into small functions
use pure functions whenever possible.

prefer end-to-end integration tests over unit tests.
