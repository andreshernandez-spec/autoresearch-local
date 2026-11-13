# Lessons: working with arl

What research loops learned about the way an agent works, in any project:
actions that were wrong, steps that wasted effort, habits that helped. Not
about any one problem. `arl lesson add --scope general` appends an entry,
usually at an introspection. `arl reload` prints this file, and every agent
reads it before it works. A lesson never overrides `PROTOCOL.md` or a project's
`PROGRAM.md`. This file is kept on this machine and is not in git. A person
prunes it and writes what has proved itself into the protocol.
