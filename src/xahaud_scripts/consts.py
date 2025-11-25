GDB_SCRIPT = """
# Set breakpoints for common crash conditions
breakpoint set --name malloc_error_break
breakpoint set --name abort
breakpoint set --name __assert_rtn
breakpoint set --name __stack_chk_fail

# Run the program
run

# When stopped, get backtrace for current/crashing thread and quit
thread backtrace
quit 1
"""

GDB_SCRIPT_ALL_THREADS = """
# Set breakpoints for common crash conditions
breakpoint set --name malloc_error_break
breakpoint set --name abort
breakpoint set --name __assert_rtn
breakpoint set --name __stack_chk_fail

# Run the program
run

# When stopped, get backtrace for all threads and quit
thread backtrace all
quit 1
"""
