"""Each function holds one case; the comment above it names what is measured."""


# p1: suppressed by a codeql comment on the line before.
def p1() -> None:
    # codeql[py/unused-local-variable] measured: the line before
    unused = 1


# p2: the same finding with no suppression, as a control.
def p2() -> None:
    unused = 1


# p3: the lgtm form on the same line.
def p3() -> None:
    unused = 1  # lgtm[py/unused-local-variable] measured: same line


# p4: a rule id that is not the one that fires.
def p4() -> None:
    # codeql[py/unused-import] measured: the wrong id
    unused = 1
