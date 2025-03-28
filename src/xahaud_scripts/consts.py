GDB_SCRIPT = """
# Set breakpoints
breakpoint set --name malloc_error_break
breakpoint set --name abort

# Run the program
run

# Check process status after run completes or crashes
process status

# Use Python script to conditionally execute backtrace
script
import lldb
import sys
import os

lldb.debugger.SetOutputFileHandle(sys.stdout, True)

def log(s, *args):
    print('\n' + s, *args)

process = lldb.debugger.GetSelectedTarget().GetProcess()

exit_desc = process.GetExitDescription()
exit_status = process.GetExitStatus()
state = process.GetState()

if exit_desc:
    log(f"Process exited with status {exit_status}: {exit_desc}")
else:
    log(f"Process state: {state} stopped={lldb.eStateStopped}")

if state == lldb.eStateStopped:
    log("Getting backtrace:")
    lldb.debugger.HandleCommand('bt')
else:
    log("Process not stopped, can't get backtrace")
    log('Exiting')
    lldb.debugger.HandleCommand('quit')
    os._exit(0)

log('End of script')
"""
