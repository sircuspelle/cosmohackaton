# Project Structure Guidelines

All new code, configurations, data, and documentation must strictly follow this directory structure concept:

- `src/main/{module_name}/`: Application source code (Python modules like `core.py`, `app.py`, `service.py`, etc.).
- `src/test/{module_name}/`: Unit and integration tests (`test_service.py`, `test_extended.py`).
- `config/{module_name}/`: Configuration files (e.g. `config.example.json`).
- `data/{module_name}/`: Data assets, such as the SQLite database (`eva.sqlite3`) and examples (`examples/`).
- `docs/{module_name}/`: Documentation, READMEs, PDFs, and task descriptions.
- `/README.md`: description of the project, always in the root, common for all modules, don't touch automatically, if doesnt said to directly change readme.md
- `/PROJECT_STRUCTURE.md`: description of the project structure, always in the root, common for all modules


### Important Notes:
1. When running the application or tests, the root directory of the repository must be the current working directory.
2. Python imports must use absolute module paths from the root, e.g., `from src.main.events.core import ...`.
3. Do not place any files directly in the root directory.