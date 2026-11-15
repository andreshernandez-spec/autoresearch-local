"""The commands, one module per area. Every change of the ledger goes through one of them.

Three habits hold throughout:

- A command checks everything it can before it changes anything, so a refusal
  leaves the ledger, the worktrees and git as they were.
- Where a change takes several steps (a verdict moves a branch, a track and a
  record), the intent is written first and the steps can be finished later by
  any command, so a process that dies in the middle leaves nothing stuck.
- A run is made of parts. A part that is interrupted (a pause, a closed
  terminal, a killed process) is not a failure: the run continues later in the
  same folder, from the state the job last saved.
"""
