import os


def get_xahaud_root() -> str:
    # walk up the directory tree until find CMakesList.txt
    # or if honour the XAHAUD_ROOT environment variable first
    env_xahaud_root = os.environ.get('XAHAUD_ROOT')
    if env_xahaud_root:
        return env_xahaud_root

    cwd = os.getcwd()
    while True:
        if os.path.exists(os.path.join(cwd, 'CMakeLists.txt')) and os.path.exists(os.path.join(cwd, '.git')):
            return cwd

        parent = os.path.dirname(cwd)
        if parent == cwd:
            raise Exception('Could not find CMakeLists.txt')
        cwd = parent
