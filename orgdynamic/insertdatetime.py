import sublime
import datetime

def Execute(view, params):
	return [str(datetime.datetime.now())]


r"""
/* SDOC: Reference::Dynamic Blocks
** insertdatetime
    #+BEGIN_EXAMPLE
    #+BEGIN: insertdatetime
    #+END:
    #+END_EXAMPLE

    Writes the current date and time (python's =2026-10-11 14:03:00.123456=
    form) into the block. Takes no parameters; a simple example of a
    dynamic block writer.
EDOC */
"""
