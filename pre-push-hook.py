#!/usr/bin/env python3

import os
import sys
import subprocess
import logging
from datetime import datetime

# Setup logging
log_dir = os.path.dirname(os.path.realpath(__file__))
log_file = os.path.join(log_dir, "git_push_attempts.log")

logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Define branches you're allowed to push to
ALLOWED_BRANCHES = [
    "jshooks"
]

def get_current_branch():
    """Get the name of the current branch."""
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], 
        universal_newlines=True
    ).strip()

def main():
    # Get the remote and branch being pushed to
    remote = sys.argv[1]
    
    # Get current branch
    current_branch = get_current_branch()
    
    # Get username for logging
    try:
        username = subprocess.check_output(
            ["git", "config", "user.name"],
            universal_newlines=True
        ).strip()
    except:
        username = "unknown"
    
    # Log the attempt
    logging.info(f"User {username} attempting to push {current_branch} to {remote}")
    
    # Only apply this check for pushes to 'origin'
    if remote != 'origin':
        logging.info(f"Push to non-origin remote {remote} allowed")
        sys.exit(0)  # Allow pushing to other remotes
    
    # Check if current branch is in allowed list
    if current_branch not in ALLOWED_BRANCHES:
        error_msg = f"ERROR: User {username} doesn't have permission to push '{current_branch}' to origin"
        print(error_msg)
        print(f"You can only push these branches: {', '.join(ALLOWED_BRANCHES)}")
        logging.warning(error_msg)
        sys.exit(1)  # Exit with error code to prevent the push
    
    # Branch is allowed, let the push proceed
    logging.info(f"Push of {current_branch} to origin allowed")
    print(f"Pushing {current_branch} to origin...")
    sys.exit(0)

if __name__ == "__main__":
    main()
