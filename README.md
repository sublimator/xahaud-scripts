 # Xahaud Scripts

 Scripts for working in xahaud repo

 ## Installation

 This project uses [Poetry](https://python-poetry.org/) for dependency management. Make sure you have Poetry installed before proceeding.

 1. Clone the repository:
    ```
    git clone https://github.com/your-username/xahaud_scripts.git
    cd xahaud_scripts
    ```

 2. Install dependencies:
    ```
    poetry install
    ```

 ## Usage

 Activate the virtual environment:

 ```
 poetry shell
 ```

 Then you can run the project:

 ```
 python -m xahaud_scripts
 ```

 ## Development

 This project uses several development tools managed by Poetry:

 - **pytest**: For running tests
 - **black**: For code formatting
 - **isort**: For import sorting
 - **flake8**: For linting
 - **mypy**: For static type checking

 To run tests:

 ```
 poetry run pytest
 ```

 To format code:

 ```
 poetry run black .
 poetry run isort .
 ```

 To run linters:

 ```
 poetry run flake8
 poetry run mypy .
 ```

 ## Contributing

 Contributions are welcome! Please feel free to submit a Pull Request.

 ## License

 This project is licensed under the MIT License.
