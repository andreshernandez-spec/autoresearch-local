The run of {target} ended without a result: the arl that ran it is gone (the session ended,
the machine restarted, or it was killed).

Continue it:  {arl} exp run {target}
arl first stops anything the dead run left behind, then goes on in the same folder, from
the state the job last saved. Nothing is counted as a failure.
To start it over instead of continuing:  {arl} exp fail {target} --reason "<why>"
