# fixture_project — seed material for ctxr-fsm E2E tests

This directory is shipped INSIDE the `ctxr-fsm` Python package. The
`ctxr.fsm.testing.materialise_fixture_project()` helper copies its
contents (resolving `gitignore.template` to `.gitignore`) into a
caller-supplied tmpdir, then runs `git init` and seeds a base + head
commit so the contents are diffable.

Do not treat this directory as a working consumer project. It is a
**template**. Edit the files here when you want every E2E test fixture
copy to change; never edit the materialised tmpdir copies.
